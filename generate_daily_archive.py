"""
일일 아카이브 생성 스크립트

fetch_market_indicators() + crawl_category() 5개 카테고리를 실행해
archive/{YYYY-MM-DD}_AM.json 으로 저장한다. 매일 06:40 KST에 1회만 실행되며,
같은 세션이 재실행(재시도/수동 재실행)되면 기존 세션 파일에 새로 발견된 기사만
누적 병합한다. 30일 지난 파일은 자동 삭제한다.

(과거에는 07:30/15:00 KST 하루 2회 실행해 AM/PM 세션을 나눴다. 그 시절 생성된
PM 세션 파일이 archive/에 남아 있을 수 있어 _session_label()/_load_day_sessions()가
여전히 AM/PM을 모두 인식하지만, 새로 생성되는 세션은 항상 AM이다.)

기사 자체의 중복은 stock_news_crawler.py의 data/seen_urls.json이 실행
경계를 넘어 전역으로 걸러낸다(직전 실행에서 이미 결과에 포함됐던 기사는
다음 실행에서 재태깅/재노출되지 않음).

archive/index.json 에는 날짜별 요약(총 기사수, High 건수)을 유지해서 HTML의
날짜 드롭다운이 파일 목록을 매번 추측/탐색하지 않도록 한다.

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

from market_indicators import (
    DEFAULT_DOMESTIC_PEERS,
    DEFAULT_NUCLEAR_RELATED,
    fetch_construction_cost_index_full,
    fetch_dynamic_stock_group_full,
    fetch_market_indicators_full,
)
from stock_news_crawler import CATEGORY_CONFIGS, crawl_category, dedup_cross_source, sort_articles

KST = ZoneInfo("Asia/Seoul")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")
INDEX_PATH = os.path.join(ARCHIVE_DIR, "index.json")
CIDX2_CACHE_PATH = os.path.join(ARCHIVE_DIR, "cidx2_cache.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
RETENTION_DAYS = 30

# 카테고리1(현대건설)의 최종 화면 노출 건수. crawl_category()의 max_articles(15)는
# subject 재분류 후에도 후보가 충분히 남도록 넉넉히 잡은 수집 단계 값이고,
# 이 값은 재분류 이후 실제로 화면에 보여줄 건수다.
HDEC_DISPLAY_MAX = 7

CATEGORY_CONFIGS_BY_NAME = {c["category_name"]: c for c in CATEGORY_CONFIGS}

# 건설업 카테고리 검색 결과 중 원전/원자력 관련 기사를 원자력 카테고리로 재분류할 때
# 쓰는 키워드. 원자력 카테고리 자체의 include_keywords(검색 결과 1차 필터링용, 일부러
# 좁게 잡음)와 달리, 이미 건설업 필터를 통과한 기사를 재분류하는 용도라 넓게 잡는다.
NUCLEAR_MIGRATE_KEYWORDS = [
    "원전", "원자력", "SMR", "우라늄", "한수원", "웨스팅하우스", "AP1000",
    "홀텍", "테라파워", "페르미 아메리카",
    "nuclear", "holtec", "westinghouse", "fermi", "terrapower", "natrium",
]


def _ensure_archive_dir():
    os.makedirs(ARCHIVE_DIR, exist_ok=True)


def _session_label(now: datetime) -> str:
    """실행 시각을 정오 기준으로 나눠 세션명을 정한다.

    현재 스케줄(06:40 KST 1회)은 항상 "AM"이 된다. 과거 하루 2회(07:30/15:00)
    실행 시절의 PM 세션 파일과 호환되도록 기준은 그대로 남겨둔다.
    """
    return "AM" if now.hour < 12 else "PM"


def _session_archive_path(date_str: str, session: str) -> str:
    return os.path.join(ARCHIVE_DIR, f"{date_str}_{session}.json")


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_admin_config() -> dict:
    """관리자 패널에서 내보낸 config.json을 읽는다.

    저장소 루트의 config.json이 없거나 형식이 잘못됐으면 빈 dict를 반환해,
    카테고리 수집 기준/지표 종목 구성이 모두 코드에 하드코딩된 기본값으로
    폴백되게 한다(기존 동작과 100% 동일하게 유지됨).
    """
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[경고] config.json 파싱 실패, 하드코딩 기본값으로 폴백: {e}")
        return {}


def _effective_category_config(name: str, admin_config: dict) -> dict:
    """카테고리 기본 설정(CATEGORY_CONFIGS)에 config.json의 관리자 설정을 덮어씌운다.

    config.json에 해당 카테고리의 include/exclude/tier1/tier2가 있고 비어있지
    않으면 그 값을 쓰고, 없으면 코드에 하드코딩된 기본값을 그대로 쓴다.
    main_source_url/supplementary_query/max_articles 등 구조적인 값은
    관리자 패널에서 건드리지 않는 값이라 항상 기본값을 쓴다.
    """
    base = dict(CATEGORY_CONFIGS_BY_NAME[name])
    override = admin_config.get("categories", {}).get(name, {})
    if override.get("include"):
        base["include_keywords"] = override["include"]
    if override.get("exclude"):
        base["exclude_keywords"] = override["exclude"]
    if override.get("tier1"):
        base["tier1_whitelist"] = override["tier1"]
    if override.get("tier2"):
        base["tier2_whitelist"] = override["tier2"]
    return base


def get_cidx2_monthly_full(now: datetime) -> tuple:
    """이번 달에 이미 조회한 값이 있으면 캐시를 재사용하고, 없으면 KOSIS API로 새로 조회한다.

    (스냅샷, 1년치 히스토리) 튜플을 반환한다.
    """
    current_month = now.strftime("%Y-%m")
    cache = _load_json(CIDX2_CACHE_PATH, None)
    if cache and cache.get("fetched_month") == current_month and "history" in cache:
        return cache["data"], cache["history"]

    kosis_key = os.environ.get("KOSIS_API_KEY")
    if not kosis_key:
        raise RuntimeError("KOSIS_API_KEY 환경변수가 설정되지 않았습니다 (cidx2 조회 불가)")

    data, history = fetch_construction_cost_index_full(kosis_key)
    _save_json(CIDX2_CACHE_PATH, {"fetched_month": current_month, "data": data, "history": history})
    return data, history


def crawl_all_categories(admin_config: dict) -> dict:
    """5개 카테고리를 크롤링한다.

    카테고리1(현대건설)은 crawl_category()가 반환한 기사 중 subject=="자사"인
    것만 남기고, 나머지(정책/시장전반/경쟁사 — 현대건설이 언급됐지만 주어가
    아닌 기사)는 카테고리2(건설업)로 이관해 병합한다. 반대로 카테고리5(자본시장)
    결과 중 subject=="자사"(현대건설 목표주가/투자의견/신용등급/회사채 등)는
    카테고리1로 이관한다. 즉 카테고리1의 최종 결과는 "현대건설이 무엇을 했다"가
    주어인 기사만 남고, 카테고리5는 개별 종목이 아닌 시장 전반 시황만 남는다.

    각 카테고리의 include/exclude 키워드·화이트리스트는 _effective_category_config()를
    통해 admin_config(관리자 패널 config.json)의 값으로 덮어써질 수 있다.
    """
    news = {}

    print("[뉴스] 현대건설 크롤링 중...")
    hdec_articles = crawl_category(**_effective_category_config("현대건설", admin_config))
    hdec_own = [a for a in hdec_articles if a.get("subject") == "자사"]
    hdec_migrate = [a for a in hdec_articles if a.get("subject") != "자사"]
    # news["현대건설"]은 아래에서 자본시장 이관분까지 합친 뒤 마지막에 확정한다.

    print("[뉴스] 건설업 크롤링 중...")
    gs_config = _effective_category_config("건설업", admin_config)
    gs_articles = crawl_category(**gs_config)
    if hdec_migrate:
        print(f"[뉴스] 현대건설에서 건설업으로 이관: {len(hdec_migrate)}건 (중복 제외 전)")
        # 건설업 자체 크롤링 결과와 제목이 유사한 이관 기사는 중복이므로 제외
        deduped_migrate = dedup_cross_source(gs_articles, hdec_migrate, threshold=0.7)
        gs_articles = sort_articles(gs_articles + deduped_migrate)

    # 건설업 결과 중 원전/원자력 관련 기사(예: "대미 원전 투자...")는 원자력
    # 카테고리로 재분류한다. 건설업 검색어(GS건설/DL이앤씨 등)에는 걸리지만
    # 내용상으로는 원자력 카테고리가 더 적합한 기사들이다.
    gs_keep, gs_nuke_migrate = [], []
    for a in gs_articles:
        title_lower = a["title"].lower()
        if any(kw.lower() in title_lower for kw in NUCLEAR_MIGRATE_KEYWORDS):
            gs_nuke_migrate.append(a)
        else:
            gs_keep.append(a)
    news["건설업"] = gs_keep[: gs_config["max_articles"]]

    print("[뉴스] 원자력 크롤링 중...")
    nuke_config = _effective_category_config("원자력", admin_config)
    nuke_articles = crawl_category(**nuke_config)
    if gs_nuke_migrate:
        print(f"[뉴스] 건설업에서 원자력으로 이관: {len(gs_nuke_migrate)}건 (중복 제외 전)")
        deduped_nuke_migrate = dedup_cross_source(nuke_articles, gs_nuke_migrate, threshold=0.7)
        nuke_articles = sort_articles(nuke_articles + deduped_nuke_migrate)
    news["원자력"] = nuke_articles[: nuke_config["max_articles"]]

    print("[뉴스] 도시정비 크롤링 중...")
    news["도시정비"] = crawl_category(**_effective_category_config("도시정비", admin_config))

    print("[뉴스] 자본시장 크롤링 중...")
    capital_config = _effective_category_config("자본시장", admin_config)
    capital_articles = crawl_category(**capital_config)
    capital_own = [a for a in capital_articles if a.get("subject") == "자사"]
    capital_keep = [a for a in capital_articles if a.get("subject") != "자사"]
    news["자본시장"] = capital_keep[: capital_config["max_articles"]]

    # 자본시장 결과 중 현대건설 자사 기사(목표주가/신용등급 등)는 카테고리1로 이관
    if capital_own:
        print(f"[뉴스] 자본시장에서 현대건설로 이관: {len(capital_own)}건 (중복 제외 전)")
        deduped_capital_migrate = dedup_cross_source(hdec_own, capital_own, threshold=0.7)
        hdec_own = sort_articles(hdec_own + deduped_capital_migrate)
    news["현대건설"] = hdec_own[:HDEC_DISPLAY_MAX]

    return news


def _resolve_dynamic_indicator_groups(admin_config: dict) -> dict:
    """config.json의 indicators.domestic_peers/nuclear_related를 쓰고,
    없거나 비어 있으면 코드 기본값으로 폴백한다."""
    admin_indicators = admin_config.get("indicators", {})
    return {
        "domestic_peers": {
            "label": "동종사",
            "entries": admin_indicators.get("domestic_peers") or DEFAULT_DOMESTIC_PEERS,
        },
        "nuclear_related": {
            "label": "원자력 관련주",
            "entries": admin_indicators.get("nuclear_related") or DEFAULT_NUCLEAR_RELATED,
        },
    }


def build_day_payload(now: datetime, admin_config: dict) -> dict:
    print("[시황] 일간 지표 수집 중...")
    indicators, indicator_history = fetch_market_indicators_full()

    print("[시황] 건설공사비지수(월간) 확인 중...")
    # cidx2도 다른 지표와 동일한 [값, 등락률, 방향] 형태이므로 그대로 indicators에 합쳐서
    # HTML의 "시장지표" 그리드에 카드로 함께 렌더링되게 한다. 히스토리는 다른 지표(최근
    # 1개월)와 달리 월간 지표 특성상 최근 1년치로 별도 관리한다.
    cidx2_snapshot, cidx2_history = get_cidx2_monthly_full(now)
    indicators["cidx2"] = cidx2_snapshot
    indicator_history["cidx2"] = cidx2_history

    print("[시황] 동종사/원자력 관련주(관리자 설정) 수집 중...")
    # 관리자 패널에서 종목 구성을 바꿀 수 있는 그룹. key는 종목코드/티커 그대로 쓰고,
    # HTML이 라벨/그룹을 알 수 있도록 indicator_meta에 같이 기록한다.
    dynamic_groups = _resolve_dynamic_indicator_groups(admin_config)
    indicator_meta = {}
    for group_key, group in dynamic_groups.items():
        snap, hist = fetch_dynamic_stock_group_full(group["entries"])
        indicators.update(snap)
        indicator_history.update(hist)
        for entry in group["entries"]:
            key = entry.get("code") or entry.get("ticker")
            if key in snap:
                indicator_meta[key] = {"label": entry["label"], "group": group_key}

    news = crawl_all_categories(admin_config)

    return {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now.isoformat(),
        "indicators": indicators,
        "indicator_history": indicator_history,
        "indicator_meta": indicator_meta,
        "daily_comment": admin_config.get("daily_comment") or "",
        "news": news,
    }


def _merge_category_articles(existing: list, new: list) -> list:
    """같은 날짜의 기존 기사 목록에 새로 크롤링한 기사를 누적 병합한다.

    URL이 같으면 동일 기사로 보고 제외하고, URL이 다르지만 제목이 비슷한
    경우(같은 소식을 다른 매체가 보도)도 dedup_cross_source로 제외한다.
    max_articles로 다시 자르지 않는다 — 하루 두 번의 실행이 누적되면서
    실제로 늘어난 만큼 그대로 보여주기 위함이다.
    """
    existing_urls = {a.get("url") for a in existing}
    new_unique = [a for a in new if a.get("url") not in existing_urls]
    new_unique = dedup_cross_source(existing, new_unique, threshold=0.7)
    return sort_articles(existing + new_unique)


def merge_with_existing_archive(archive_path: str, day_payload: dict) -> dict:
    """같은 세션(AM 또는 PM) 파일이 이미 존재하면(재시도/수동 재실행 등으로
    같은 세션이 두 번 이상 돌 때) 뉴스는 기존 목록에 새 기사를 누적 병합하고,
    시황 지표는 최신 값으로 덮어쓴다(지표는 스냅샷이라 누적할 대상이 아니다).

    data/seen_urls.json이 이미 실행 경계를 넘어 기사 중복 자체를 막아주므로,
    이 병합은 어디까지나 "같은 세션이 재실행됐을 때 이전 결과를 통째로
    덮어쓰지 않기 위한" 안전장치다.
    """
    existing = _load_json(archive_path, None)
    if not existing or not existing.get("news"):
        return day_payload

    print("[뉴스] 같은 세션의 기존 아카이브 발견 — 누적 병합")
    merged_news = {}
    for name, new_articles in day_payload["news"].items():
        existing_articles = existing["news"].get(name, [])
        merged = _merge_category_articles(existing_articles, new_articles)
        if len(merged) != len(existing_articles):
            print(f"  {name}: {len(existing_articles)}건 → {len(merged)}건")
        merged_news[name] = merged

    day_payload["news"] = merged_news
    return day_payload


def _load_day_sessions(date_str: str) -> list:
    """해당 날짜의 AM/PM 세션 파일 중 실제로 존재하는 것만 로드해서 반환한다."""
    sessions = []
    for session in ["AM", "PM"]:
        data = _load_json(_session_archive_path(date_str, session), None)
        if data:
            sessions.append(data)
    return sessions


def update_index(date_str: str):
    """해당 날짜의 AM+PM 세션을 합산해 index.json에 날짜당 한 항목만 유지한다.

    세션 파일을 나눠 저장해도 사용자에게는 "오늘 브리핑" 하나로 보여주는 게
    자연스러우므로(HTML도 AM+PM을 합쳐서 렌더링한다), 인덱스도 세션이 아니라
    날짜 단위로 집계한다.
    """
    sessions = _load_day_sessions(date_str)
    if not sessions:
        return

    combined_news: dict = {}
    for day in sessions:
        for name, articles in day.get("news", {}).items():
            combined_news.setdefault(name, []).extend(articles)
    for name, articles in combined_news.items():
        # AM/PM 세션을 합칠 때 같은 기사(URL 동일)가 겹칠 수 있어 총 건수/High
        # 건수가 부풀려지지 않도록 URL 기준으로 중복을 제거한 뒤 정렬한다.
        seen_urls = set()
        deduped = []
        for a in articles:
            url = a.get("url")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            deduped.append(a)
        combined_news[name] = sort_articles(deduped)

    all_articles = [a for articles in combined_news.values() for a in articles]
    total = len(all_articles)
    high_count = sum(1 for a in all_articles if a.get("importance") == "High")
    top_titles = [
        articles[0]["title"]
        for articles in combined_news.values()
        if articles
    ]

    index = _load_json(INDEX_PATH, [])
    index = [entry for entry in index if entry["date"] != date_str]
    index.append({
        "date": date_str,
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
        stem = filename[:-5]
        # "YYYY-MM-DD_AM"/"YYYY-MM-DD_PM" 형식에서 날짜 부분만 뗀다. 예전
        # "YYYY-MM-DD.json"(세션 구분 없던 파일)도 그대로 지원한다.
        date_part = stem[:-3] if stem.endswith(("_AM", "_PM")) else stem
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

    admin_config = load_admin_config()
    if admin_config:
        print("[설정] config.json 로드됨 — 관리자 설정 적용")
    else:
        print("[설정] config.json 없음 — 하드코딩 기본값 사용")

    session = _session_label(now)
    print(f"[세션] {session} ({now.strftime('%H:%M')} KST)")

    day_payload = build_day_payload(now, admin_config)
    day_payload["session"] = session

    date_str = day_payload["date"]
    archive_path = _session_archive_path(date_str, session)
    day_payload = merge_with_existing_archive(archive_path, day_payload)
    _save_json(archive_path, day_payload)
    print(f"저장 완료: {archive_path}")

    update_index(date_str)
    print(f"인덱스 갱신 완료: {INDEX_PATH}")

    prune_old_archives(now)


if __name__ == "__main__":
    main()
