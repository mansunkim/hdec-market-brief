"""
일일 아카이브 생성 스크립트

fetch_market_indicators() + crawl_category() 5개 카테고리를 실행해
archive/{YYYY-MM-DD}.json 으로 저장한다. 30일 지난 파일은 자동 삭제한다.

archive/index.json 에는 날짜별 요약(총 기사수, High 건수)을 유지해서
HTML의 날짜 드롭다운이 파일 목록을 매번 추측/탐색하지 않도록 한다.

cidx2(건설공사비지수)는 월 1회만 갱신되는 지표라 매일 새로 조회하지 않는다.
archive/cidx2_cache.json 에 "이번 달에 이미 조회했는지"를 기록해두고,
같은 달이면 캐시를 재사용하고 달이 바뀌었거나 캐시가 없을 때만 KOSIS API를 호출한다.

환경변수:
    ANTHROPIC_API_KEY  — 뉴스 LLM 태깅
    KRX_ID, KRX_PW     — cidx1(pykrx) 로그인
    KOSIS_API_KEY      — cidx2(건설공사비지수)

사전 준비:
    pip install playwright anthropic finance-datareader pykrx requests
    playwright install chromium
"""

import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from market_indicators import fetch_construction_cost_index, fetch_market_indicators
from stock_news_crawler import CATEGORY_CONFIGS, crawl_category, dedup_cross_source, sort_articles

KST = ZoneInfo("Asia/Seoul")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")
INDEX_PATH = os.path.join(ARCHIVE_DIR, "index.json")
CIDX2_CACHE_PATH = os.path.join(ARCHIVE_DIR, "cidx2_cache.json")
RETENTION_DAYS = 30

# 카테고리1(현대건설)의 최종 화면 노출 건수. crawl_category()의 max_articles(15)는
# subject 재분류 후에도 후보가 충분히 남도록 넉넉히 잡은 수집 단계 값이고,
# 이 값은 재분류 이후 실제로 화면에 보여줄 건수다.
HDEC_DISPLAY_MAX = 7

CATEGORY_CONFIGS_BY_NAME = {c["category_name"]: c for c in CATEGORY_CONFIGS}


def _ensure_archive_dir():
    os.makedirs(ARCHIVE_DIR, exist_ok=True)


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_cidx2_monthly(now: datetime) -> list:
    """이번 달에 이미 조회한 값이 있으면 캐시를 재사용하고, 없으면 KOSIS API로 새로 조회한다."""
    current_month = now.strftime("%Y-%m")
    cache = _load_json(CIDX2_CACHE_PATH, None)
    if cache and cache.get("fetched_month") == current_month:
        return cache["data"]

    kosis_key = os.environ.get("KOSIS_API_KEY")
    if not kosis_key:
        raise RuntimeError("KOSIS_API_KEY 환경변수가 설정되지 않았습니다 (cidx2 조회 불가)")

    data = fetch_construction_cost_index(kosis_key)
    _save_json(CIDX2_CACHE_PATH, {"fetched_month": current_month, "data": data})
    return data


def crawl_all_categories() -> dict:
    """5개 카테고리를 크롤링한다.

    카테고리1(현대건설)은 crawl_category()가 반환한 기사 중 subject=="자사"인
    것만 남기고, 나머지(정책/시장전반/경쟁사 — 현대건설이 언급됐지만 주어가
    아닌 기사)는 카테고리2(건설업)로 이관해 병합한다. 즉 카테고리1의 최종
    결과는 "현대건설이 무엇을 했다"가 주어인 기사만 남는다.
    """
    news = {}

    print("[뉴스] 현대건설 크롤링 중...")
    hdec_articles = crawl_category(**CATEGORY_CONFIGS_BY_NAME["현대건설"])
    hdec_own = [a for a in hdec_articles if a.get("subject") == "자사"]
    hdec_migrate = [a for a in hdec_articles if a.get("subject") != "자사"]
    news["현대건설"] = sort_articles(hdec_own)[:HDEC_DISPLAY_MAX]

    print("[뉴스] 건설업 크롤링 중...")
    gs_config = CATEGORY_CONFIGS_BY_NAME["건설업"]
    gs_articles = crawl_category(**gs_config)
    if hdec_migrate:
        print(f"[뉴스] 현대건설에서 건설업으로 이관: {len(hdec_migrate)}건 (중복 제외 전)")
        # 건설업 자체 크롤링 결과와 제목이 유사한 이관 기사는 중복이므로 제외
        deduped_migrate = dedup_cross_source(gs_articles, hdec_migrate, threshold=0.7)
        gs_articles = sort_articles(gs_articles + deduped_migrate)
    news["건설업"] = gs_articles[: gs_config["max_articles"]]

    for name in ["원자력", "도시정비", "자본시장"]:
        print(f"[뉴스] {name} 크롤링 중...")
        news[name] = crawl_category(**CATEGORY_CONFIGS_BY_NAME[name])

    return news


def build_day_payload(now: datetime) -> dict:
    print("[시황] 일간 지표 수집 중...")
    indicators = fetch_market_indicators()

    print("[시황] 건설공사비지수(월간) 확인 중...")
    # cidx2도 다른 지표와 동일한 [값, 등락률, 방향] 형태이므로 그대로 indicators에 합쳐서
    # HTML의 "시장지표" 그리드에 카드로 함께 렌더링되게 한다.
    indicators["cidx2"] = get_cidx2_monthly(now)

    news = crawl_all_categories()

    return {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now.isoformat(),
        "indicators": indicators,
        "news": news,
    }


def update_index(day_payload: dict):
    index = _load_json(INDEX_PATH, [])
    index = [entry for entry in index if entry["date"] != day_payload["date"]]

    all_articles = [a for articles in day_payload["news"].values() for a in articles]
    total = len(all_articles)
    high_count = sum(1 for a in all_articles if a.get("importance") == "High")
    top_titles = [
        articles[0]["title"]
        for articles in day_payload["news"].values()
        if articles
    ]

    index.append({
        "date": day_payload["date"],
        "total": total,
        "high": high_count,
        "top_titles": top_titles[:2],
    })
    index.sort(key=lambda e: e["date"], reverse=True)
    _save_json(INDEX_PATH, index)


def prune_old_archives(now: datetime):
    cutoff = (now - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    for filename in os.listdir(ARCHIVE_DIR):
        if not filename.endswith(".json"):
            continue
        date_part = filename[:-5]
        # index.json, cidx2_cache.json 등 날짜 형식이 아닌 파일은 건너뜀
        if len(date_part) != 10 or date_part.count("-") != 2:
            continue
        if date_part < cutoff:
            path = os.path.join(ARCHIVE_DIR, filename)
            os.remove(path)
            print(f"[정리] 30일 지난 아카이브 삭제: {filename}")

    # index.json에서도 30일 지난 항목 제거
    index = _load_json(INDEX_PATH, [])
    pruned_index = [entry for entry in index if entry["date"] >= cutoff]
    if len(pruned_index) != len(index):
        _save_json(INDEX_PATH, pruned_index)


def main():
    _ensure_archive_dir()
    now = datetime.now(KST)

    day_payload = build_day_payload(now)

    archive_path = os.path.join(ARCHIVE_DIR, f"{day_payload['date']}.json")
    _save_json(archive_path, day_payload)
    print(f"저장 완료: {archive_path}")

    update_index(day_payload)
    print(f"인덱스 갱신 완료: {INDEX_PATH}")

    prune_old_archives(now)


if __name__ == "__main__":
    main()
