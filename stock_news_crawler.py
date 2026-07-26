"""
종목뉴스 크롤러 (현대건설 기준 작성, 다른 카테고리에도 재사용 가능)

사전 준비:
    pip install playwright anthropic
    playwright install chromium
    (환경변수 ANTHROPIC_API_KEY 설정 필요 — LLM 태깅에 사용)

사용법:
    python stock_news_crawler.py
    (하단 __main__ 블록의 HYUNDAI_ENG_CONFIG 참고 — 카테고리별로
     이 dict만 바꿔서 crawl_category()를 다시 호출하면 됨)

주의:
    - 네이버 금융/검색 페이지의 HTML 구조는 예고 없이 바뀔 수 있습니다.
      셀렉터가 깨지면 fetch_main_source / fetch_supplementary_source 의
      CSS 셀렉터를 실제 페이지 구조에 맞춰 조정해야 합니다.
    - "최신순" 정렬은 별도 날짜 파싱 없이, 크롤링 시 이미 최신순으로
      내려오는 네이버의 기본 정렬 순서를 그대로 신뢰합니다.
"""

import json
import os
import re
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import quote

import anthropic
from playwright.sync_api import sync_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 1. 메인 소스 — 네이버 금융 종목뉴스 (Playwright)
# ---------------------------------------------------------------------------

def fetch_main_source(stock_code: str, pages: int = 2) -> list[dict]:
    """네이버 금융 종목뉴스 페이지를 크롤링해 title/press/time/url을 수집한다."""
    if not stock_code:
        return []

    articles = []
    referer = f"https://finance.naver.com/item/main.naver?code={stock_code}"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT, extra_http_headers={"Referer": referer})
        # news_news.naver는 종목 메인 페이지를 거쳐 들어오지 않으면 빈 결과("검색된 '' 뉴스가
        # 없습니다")를 반환하므로, 먼저 메인 페이지를 방문해 Referer 흐름을 만들어준다.
        page.goto(referer, wait_until="domcontentloaded")
        for page_no in range(1, pages + 1):
            url = (
                f"https://finance.naver.com/item/news_news.naver"
                f"?code={stock_code}&page={page_no}"
            )
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_selector("table.type5", timeout=10000)

            rows = page.query_selector_all("table.type5 tr")
            for row in rows:
                title_el = row.query_selector("td.title a")
                press_el = row.query_selector("td.info")
                date_el = row.query_selector("td.date")
                if not title_el or not press_el or not date_el:
                    continue

                title = title_el.inner_text().strip()
                href = title_el.get_attribute("href")
                if not title or not href:
                    continue

                link = href if href.startswith("http") else f"https://finance.naver.com{href}"
                articles.append({
                    "title": title,
                    "press": press_el.inner_text().strip(),
                    "time": date_el.inner_text().strip(),
                    "url": link,
                    "source": "main",
                    "source_tier": None,
                })
        browser.close()
    return articles


# ---------------------------------------------------------------------------
# 2. 보조 소스 — 네이버뉴스 검색 결과 (Playwright)
# ---------------------------------------------------------------------------

# 네이버뉴스 검색 결과 카드에서 제목/언론사/시각을 뽑아내는 브라우저 측 스크립트.
# 네이버 검색 UI는 클래스명이 빌드마다 해시로 바뀌는 디자인 시스템(sds-comps-*)을 쓰므로
# 비교적 안정적인 data-heatmap-target 속성과 텍스트 구조를 기준으로 파싱한다.
# 카드는 두 가지 템플릿이 섞여 있다:
#   (B) 압축형 "관련기사" 목록 — 제목 링크의 바로 위 부모에 언론사/시각이 형제 텍스트로 존재
#   (A) 대표기사 카드(클러스터 헤드) — 언론사 정보가 .prof 텍스트로 별도 위치에 존재
_SUPP_EXTRACT_JS = r"""
(el) => {
    const stripNoise = (s) => s
        .replace(/새 창 열림/g, "")
        .replace(/네이버뉴스/g, "")
        .replace(/\|/g, "")
        .trim();

    const titleText = stripNoise(el.innerText);

    // (B) 압축형 목록: 부모의 텍스트에서 제목을 제거하면 언론사/시각만 남는다.
    const parent = el.parentElement;
    if (parent) {
        const cleaned = parent.innerText.replace(titleText, "");
        const lines = cleaned.split("\n").map(stripNoise).filter(Boolean);
        if (lines.length >= 2) {
            return { press: lines[0], time: lines[1] };
        }
    }

    // (A) 대표기사 카드: 가까운 조상에서 .prof 텍스트(언론사명)와 날짜 패턴 텍스트를 탐색.
    const datePattern = /(\d+\s*(분|시간|일)\s*전|어제|\d{4}\.\s*\d{1,2}\.\s*\d{1,2})/;
    let ancestor = el;
    for (let depth = 0; depth < 6 && ancestor; depth++) {
        ancestor = ancestor.parentElement;
        if (!ancestor) break;
        const profEl = ancestor.querySelector(".sds-comps-profile-info-title-text");
        if (!profEl) continue;

        const press = stripNoise(profEl.innerText);
        let time = null;
        const walker = document.createTreeWalker(ancestor, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
            const m = walker.currentNode.textContent.match(datePattern);
            if (m) { time = m[0]; break; }
        }
        return { press, time };
    }

    return { press: null, time: null };
}
"""


def fetch_supplementary_source(query: str, max_results: int = 30) -> list[dict]:
    """네이버뉴스 검색 결과를 크롤링해 title/press/time/url을 수집한다.

    네이버뉴스 검색은 불리언 "OR" 문법을 지원하지 않고 문자 그대로 취급하므로,
    query에 " OR "가 포함되어 있으면 각 검색어를 개별적으로 검색한 뒤
    URL 기준으로 중복을 제거해 합친다. (예: "현대건설 OR 현대엔지니어링"
    → "현대건설", "현대엔지니어링" 두 번 검색 후 병합)
    """
    terms = [t.strip() for t in re.split(r"\s+OR\s+", query, flags=re.IGNORECASE) if t.strip()]
    if not terms:
        return []

    seen_urls: set[str] = set()
    articles = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)
        for term in terms:
            encoded = quote(term)
            url = f"https://search.naver.com/search.naver?where=news&query={encoded}"
            page.goto(url, wait_until="networkidle")

            try:
                page.wait_for_selector('a[data-heatmap-target=".tit"]', timeout=10000)
            except Exception:
                continue

            title_els = page.query_selector_all('a[data-heatmap-target=".tit"]')
            for title_el in title_els[:max_results]:
                href = title_el.get_attribute("href")
                title = re.sub(r"새 창 열림\s*$", "", title_el.inner_text()).strip()
                if not title or not href or href in seen_urls:
                    continue
                seen_urls.add(href)

                info = title_el.evaluate(_SUPP_EXTRACT_JS)
                press = (info.get("press") or "").strip()
                press = re.sub(r"^언론사\s*선정?\s*", "", press).strip()
                time_text = (info.get("time") or "").strip()

                articles.append({
                    "title": title,
                    "press": press,
                    "time": time_text,
                    "url": href,
                    "source": "supplementary",
                    "source_tier": None,
                })
        browser.close()
    return articles


def is_published_today(time_text: str) -> bool:
    """상대시각("3시간 전")과 절대시각("2026.07.26 09:30") 표기를 모두
    '오늘 발행 여부' boolean으로 정규화한다. 형식을 알 수 없으면 False."""
    if not time_text:
        return False

    text = time_text.strip()

    # 상대시각: "N분 전", "N시간 전" → 오늘
    if re.search(r"(분|시간)\s*전$", text):
        return True

    # 상대시각: "N일 전", "어제" 등 → 오늘 아님
    if re.search(r"일\s*전$", text) or text.startswith("어제"):
        return False

    # 절대시각: "2026.07.26" 또는 "2026.07.26 09:30" 형태
    m = re.match(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", text)
    if m:
        try:
            article_date = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
            return article_date == datetime.now().date()
        except ValueError:
            return False

    return False


# ---------------------------------------------------------------------------
# 3. 1차 필터 (포함/제외 키워드)
# ---------------------------------------------------------------------------

def apply_keyword_filter(
    articles: list[dict],
    include_keywords: list[str],
    exclude_keywords: list[str],
) -> list[dict]:
    result = []
    for a in articles:
        title = a["title"]
        if any(kw in title for kw in exclude_keywords):
            continue
        if include_keywords and not any(kw in title for kw in include_keywords):
            continue
        result.append(a)
    return result


# ---------------------------------------------------------------------------
# 4. 보조소스 언론사 화이트리스트 필터
# ---------------------------------------------------------------------------

def apply_whitelist_filter(
    articles: list[dict],
    tier1_whitelist: list[str],
    tier2_whitelist: list[str],
) -> list[dict]:
    result = []
    for a in articles:
        press = a["press"]
        if press in tier1_whitelist:
            a["source_tier"] = "tier1"
            result.append(a)
        elif press in tier2_whitelist:
            a["source_tier"] = "tier2"
            result.append(a)
        # 화이트리스트 외 언론사는 자동 제외
    return result


# ---------------------------------------------------------------------------
# 5. 제목 유사도 기반 중복 제거
# ---------------------------------------------------------------------------

def _title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def dedup_cross_source(
    main_articles: list[dict],
    supplementary_articles: list[dict],
    threshold: float = 0.7,
) -> list[dict]:
    """메인소스와 제목 유사도 threshold 이상인 보조소스 기사를 제외한다."""
    result = []
    for s in supplementary_articles:
        is_dup = any(
            _title_similarity(s["title"], m["title"]) >= threshold
            for m in main_articles
        )
        if not is_dup:
            result.append(s)
    return result


def dedup_within_source(articles: list[dict], threshold: float = 0.8) -> list[dict]:
    """동일 소스 내 제목 유사도 threshold 이상 기사는 최신 1건만 남기고
    '외 N개 언론사 보도'로 병합한다. articles는 최신순으로 정렬되어 있다고 가정."""
    groups: list[list[dict]] = []
    for a in articles:
        placed = False
        for g in groups:
            if _title_similarity(a["title"], g[0]["title"]) >= threshold:
                g.append(a)
                placed = True
                break
        if not placed:
            groups.append([a])

    merged = []
    for g in groups:
        kept = dict(g[0])  # 그룹의 첫 기사 = 최신 기사 (입력이 최신순 정렬 가정)
        if len(g) > 1:
            kept["title"] = f"{kept['title']} 외 {len(g) - 1}개 언론사 보도"
        merged.append(kept)
    return merged


# ---------------------------------------------------------------------------
# 6. LLM 태깅 (Claude API, Structured Outputs로 일괄 처리)
# ---------------------------------------------------------------------------

_TAG_SCHEMA = {
    "type": "object",
    "properties": {
        "articles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "scope": {"type": "string", "enum": ["국내", "해외"]},
                    "subject": {
                        "type": "string",
                        "enum": ["자사", "경쟁사", "정책", "시장전반"],
                    },
                    "entity": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "importance": {"type": "string", "enum": ["High", "Normal"]},
                },
                "required": ["index", "scope", "subject", "entity", "importance"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["articles"],
    "additionalProperties": False,
}


def tag_articles_with_llm(
    articles: list[dict],
    domain: str,
    client: anthropic.Anthropic,
) -> list[dict]:
    """각 기사에 scope/subject/entity/importance를 LLM으로 채운다 (1회 배치 호출)."""
    if not articles:
        return articles

    numbered = "\n".join(
        f"{i}. [{a['press']}] {a['title']}" for i, a in enumerate(articles)
    )
    prompt = f"""다음은 "{domain}" 관련 뉴스 기사 제목 목록이다. 각 기사를 아래 기준으로 분류하라.

- scope: 기사 내용이 국내 사업/이슈면 "국내", 해외 사업/이슈면 "해외"
- subject: 기사의 주체가 {domain} 자사 관련이면 "자사", 경쟁사 관련이면 "경쟁사",
  정부/제도/정책 관련이면 "정책", 특정 기업이 아닌 시장 전반 동향이면 "시장전반"
- entity: 기사에서 언급된 구체적 프로젝트명/기관명/기업명 (특정할 수 없으면 null)
- importance: 수주, 계약, 공시 관련 내용을 포함하면 "High", 그 외는 "Normal"

기사 목록:
{numbered}
"""

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4096,
        thinking={"type": "disabled"},
        output_config={
            "effort": "low",
            "format": {"type": "json_schema", "schema": _TAG_SCHEMA},
        },
        messages=[{"role": "user", "content": prompt}],
    )

    text = next(b.text for b in response.content if b.type == "text")
    parsed = json.loads(text)
    tag_map = {item["index"]: item for item in parsed["articles"]}

    for i, a in enumerate(articles):
        tag = tag_map.get(i, {})
        a["domain"] = domain
        a["scope"] = tag.get("scope", "국내")
        a["subject"] = tag.get("subject", "자사")
        a["entity"] = tag.get("entity")
        a["importance"] = tag.get("importance", "Normal")
    return articles


# ---------------------------------------------------------------------------
# 7. 메인 오케스트레이터
# ---------------------------------------------------------------------------

def crawl_category(
    category_name: str,
    main_source_url: str,
    supplementary_query: str,
    include_keywords: list[str],
    exclude_keywords: list[str],
    tier1_whitelist: list[str],
    tier2_whitelist: list[str],
    max_articles: int = 7,
) -> list[dict]:
    """카테고리별 종목뉴스를 크롤링/필터링/태깅해 최종 기사 리스트를 반환한다."""

    # main_source_url 예: "https://finance.naver.com/item/news_news.naver?code=000720"
    code_match = re.search(r"code=(\d+)", main_source_url or "")
    stock_code = code_match.group(1) if code_match else None

    main_raw = fetch_main_source(stock_code, pages=2)
    supp_raw = fetch_supplementary_source(supplementary_query)

    # 1차 필터 (포함/제외 키워드)
    main_filtered = apply_keyword_filter(main_raw, include_keywords, exclude_keywords)
    supp_filtered = apply_keyword_filter(supp_raw, include_keywords, exclude_keywords)

    # 보조소스 언론사 화이트리스트 필터 (Tier1/Tier2 외 자동 제외)
    supp_whitelisted = apply_whitelist_filter(supp_filtered, tier1_whitelist, tier2_whitelist)

    # 메인소스와 제목 유사도 70% 이상 → 중복으로 간주해 보조소스에서 제외
    supp_deduped = dedup_cross_source(main_filtered, supp_whitelisted, threshold=0.7)

    # 동일 소스 내 제목 유사도 80% 이상 → 최신 1건만 남기고 병합
    main_merged = dedup_within_source(main_filtered, threshold=0.8)
    supp_merged = dedup_within_source(supp_deduped, threshold=0.8)

    all_articles = main_merged + supp_merged

    # LLM 태깅 (domain/scope/subject/entity/importance)
    client = anthropic.Anthropic()
    all_articles = tag_articles_with_llm(all_articles, category_name, client)

    # Tier2 매체 + 제목에 "단독" 포함 → importance Normal 강등 + verify_needed 플래그
    for a in all_articles:
        if a.get("source_tier") == "tier2" and "단독" in a["title"]:
            a["importance"] = "Normal"
            a["verify_needed"] = True
        else:
            a.setdefault("verify_needed", False)

    # importance High 우선 → 그 다음 오늘 발행 기사 우선 → 그 안에서는 소스 내 정렬
    # 순서(=최신순) 유지 (stable sort이므로 동일 키 그룹 내에서는 원래 순서가 보존됨)
    all_articles.sort(
        key=lambda a: (
            0 if a.get("importance") == "High" else 1,
            0 if is_published_today(a.get("time", "")) else 1,
        )
    )

    final_articles = all_articles[:max_articles]

    return [
        {
            "title": a["title"],
            "press": a["press"],
            "time": a.get("time", ""),
            "url": a.get("url", ""),
            "domain": a.get("domain", category_name),
            "scope": a.get("scope"),
            "subject": a.get("subject"),
            "entity": a.get("entity"),
            "importance": a.get("importance"),
            "source": a.get("source"),
            # 메인소스(공식 종목뉴스 피드)는 항상 Tier 1로 간주.
            # 보조소스는 화이트리스트 등급(tier1/tier2)을 그대로 반영.
            "tier": "t1" if a.get("source") == "main" else a.get("source_tier", "t2"),
            "verify_needed": a.get("verify_needed", False),
        }
        for a in final_articles
    ]


# ---------------------------------------------------------------------------
# 카테고리 1~5 스펙 (generate_daily_archive.py에서 import해서 사용)
# ---------------------------------------------------------------------------

_TIER1_WHITELIST = ["대한경제", "건설경제", "매일경제", "한국경제", "서울경제"]
_TIER2_WHITELIST = ["더그루", "이데일리", "뉴스핌", "파이낸셜뉴스"]
_EXCLUDE_KEYWORDS = ["구단", "야구단", "축구단", "테마주", "급등주"]

CATEGORY_CONFIGS = [
    dict(
        category_name="현대건설",
        main_source_url="https://finance.naver.com/item/news_news.naver?code=000720",
        supplementary_query="현대건설 OR 현대엔지니어링",
        include_keywords=[
            "수주", "계약", "실적", "공시", "배당", "인수", "지분", "소송", "MOU", "착공", "준공",
        ],
        exclude_keywords=_EXCLUDE_KEYWORDS,
        tier1_whitelist=_TIER1_WHITELIST,
        tier2_whitelist=_TIER2_WHITELIST,
        max_articles=7,
    ),
    dict(
        category_name="건설업",
        main_source_url="",  # 특정 종목이 없어 보조소스만 사용
        supplementary_query="건설업 OR 건설경기",
        include_keywords=["수주", "분양", "정비사업", "PF", "착공", "준공"],
        exclude_keywords=_EXCLUDE_KEYWORDS,
        tier1_whitelist=_TIER1_WHITELIST,
        tier2_whitelist=_TIER2_WHITELIST,
        max_articles=7,
    ),
    dict(
        category_name="원자력",
        main_source_url="",
        supplementary_query="원자력 OR SMR OR 원전",
        include_keywords=["원전", "원자력", "SMR", "수주", "수출"],
        exclude_keywords=_EXCLUDE_KEYWORDS,
        tier1_whitelist=_TIER1_WHITELIST,
        tier2_whitelist=_TIER2_WHITELIST,
        max_articles=7,
    ),
    dict(
        category_name="도시정비",
        main_source_url="",
        supplementary_query="도시정비 OR 재건축 OR 재개발",
        include_keywords=["재개발", "재건축", "정비사업", "시공사", "입찰", "조합"],
        exclude_keywords=_EXCLUDE_KEYWORDS,
        tier1_whitelist=_TIER1_WHITELIST,
        tier2_whitelist=_TIER2_WHITELIST,
        max_articles=7,
    ),
    dict(
        category_name="자본시장",
        main_source_url="",
        supplementary_query="건설사 회사채 OR 건설업 자본시장",
        include_keywords=["회사채", "신용등급", "유상증자", "목표주가", "투자의견", "실적"],
        exclude_keywords=_EXCLUDE_KEYWORDS,
        tier1_whitelist=_TIER1_WHITELIST,
        tier2_whitelist=_TIER2_WHITELIST,
        max_articles=7,
    ),
]


# ---------------------------------------------------------------------------
# 실행 예시 — 카테고리 5개 전체
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    all_results = {}
    for config in CATEGORY_CONFIGS:
        name = config["category_name"]
        print(f"=== {name} 크롤링 중 ===")
        all_results[name] = crawl_category(**config)

    print(json.dumps(all_results, ensure_ascii=False, indent=2))

    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output_test.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"저장 완료: {output_path}")
