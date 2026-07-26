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

import html
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
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


def _expand_query(query: str) -> list[str]:
    """검색어 문자열을 네이버뉴스에 실제로 넣을 리터럴 검색어 목록으로 변환한다.

    네이버뉴스 검색은 불리언 연산자를 지원하지 않고 문자 그대로 취급하므로,
    아래 패턴만 해석해서 여러 번의 리터럴 검색으로 확장한다:
      - "A AND (B OR C OR D)" → ["A B", "A C", "A D"]
        (공백으로 결합한 문자열은 네이버 검색창에서 암묵적 AND로 동작)
      - "A OR B OR C" → ["A", "B", "C"]
      - 그 외 → [query] (있는 그대로 한 번 검색)
    """
    query = (query or "").strip()
    if not query:
        return []

    and_match = re.match(r"^(.+?)\s+AND\s+\((.+)\)$", query, flags=re.IGNORECASE)
    if and_match:
        prefix = and_match.group(1).strip()
        or_terms = [
            t.strip() for t in re.split(r"\s+OR\s+", and_match.group(2), flags=re.IGNORECASE) if t.strip()
        ]
        return [f"{prefix} {t}" for t in or_terms]

    or_terms = [t.strip() for t in re.split(r"\s+OR\s+", query, flags=re.IGNORECASE) if t.strip()]
    return or_terms if or_terms else [query]


def fetch_supplementary_source(query: str, max_results: int = 30) -> list[dict]:
    """네이버뉴스 검색 결과를 크롤링해 title/press/time/url을 수집한다.

    query는 " OR "와 "A AND (B OR C)" 패턴을 해석해 여러 번의 리터럴 검색으로
    확장한 뒤 URL 기준으로 중복을 제거해 합친다. (_expand_query 참고)
    """
    terms = _expand_query(query)
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
# 2b. 해외 소스 — 원자력 카테고리 전용 (RSS 우선, 일부는 HTML 스크래핑)
# ---------------------------------------------------------------------------

# press 표시명 -> RSS 피드 URL. Reuters는 공개 RSS를 찾지 못해 목록에서 제외했다
# (네이버뉴스 검색 결과로 노출되는 Reuters 기사만 화이트리스트를 통해 포함된다).
_FOREIGN_RSS_SOURCES = {
    "World Nuclear News": "https://www.world-nuclear-news.org/rss",
    "DOE": "https://www.energy.gov/rss.xml",
    "NRC": "https://www.nrc.gov/public-involve/rss?feed=news",
    "Utility Dive": "https://www.utilitydive.com/feeds/news/",
    "POWER Magazine": "https://www.powermag.com/feed/",
    "Holtec": "https://holtecinternational.com/feed/",
    "Westinghouse": "https://info.westinghousenuclear.com/news/rss.xml",
    "Fermi America": "https://fermiamerica.com/feed/",
}

# 기업이 직접 발행하는 보도자료 소스. 홍보성일 수 있어 항상 Tier2 + verify_needed로
# 처리한다 (tag_articles_with_llm 이후 _crawl_category_impl에서 무조건 적용하는 규칙).
_COMPANY_PR_PRESS_NAMES = {"Holtec", "Westinghouse", "TerraPower", "Fermi America"}


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _format_rss_time(pub_date: str) -> str:
    """RSS pubDate(RFC 822)를 메인소스와 동일한 "YYYY.MM.DD HH:MM" 형식으로 변환한다.
    (is_published_today/formatDisplayTime이 별도 처리 없이 그대로 인식하도록 하기 위함)
    파싱에 실패하면 빈 문자열을 반환한다."""
    if not pub_date:
        return ""
    try:
        dt = parsedate_to_datetime(pub_date)
        return dt.strftime("%Y.%m.%d %H:%M")
    except (TypeError, ValueError):
        return ""


def _parse_rss(xml_text: str, press_name: str, max_items: int = 15) -> list[dict]:
    articles = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return articles
    for item in root.findall(".//item")[:max_items]:
        title_el = item.find("title")
        link_el = item.find("link")
        date_el = item.find("pubDate")
        desc_el = item.find("description")
        title = html.unescape((title_el.text or "").strip()) if title_el is not None else ""
        link = (link_el.text or "").strip() if link_el is not None else ""
        pub_date = (date_el.text or "").strip() if date_el is not None else ""
        description = _strip_html(desc_el.text or "")[:400] if desc_el is not None else ""
        if not title or not link:
            continue
        articles.append({
            "title": title,
            "press": press_name,
            "time": _format_rss_time(pub_date),
            "url": link,
            "source": "supplementary",
            "source_tier": None,
            "description": description,
        })
    return articles


def fetch_foreign_sources() -> list[dict]:
    """원자력 카테고리 전용 해외 소스를 수집한다.

    - RSS는 Playwright로 페이지를 열어 response.text()로 받는다. NRC처럼 urllib
      요청은 403으로 차단하지만 실제 브라우저 요청은 통과시키는 사이트가 있어,
      전 소스에 동일한 방식(실제 브라우저)을 써서 일관되게 처리한다.
    - TerraPower는 공개 RSS가 없어 뉴스 목록 페이지를 직접 스크래핑한다.
    """
    articles = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)

        for press_name, url in _FOREIGN_RSS_SOURCES.items():
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=20000)
                xml_text = resp.text() if resp else ""
                articles.extend(_parse_rss(xml_text, press_name))
            except Exception:
                continue  # 개별 소스 실패는 건너뛰고 계속 진행

        try:
            page.goto("https://www.terrapower.com/news", wait_until="networkidle", timeout=20000)
            seen_urls: set[str] = set()
            for link_el in page.query_selector_all("a"):
                title = (link_el.inner_text() or "").strip()
                if len(title) < 15:
                    continue
                href = link_el.evaluate("e => e.href")
                if not href or href in seen_urls:
                    continue
                seen_urls.add(href)
                articles.append({
                    "title": title,
                    "press": "TerraPower",
                    "time": "",
                    "url": href,
                    "source": "supplementary",
                    "source_tier": None,
                    "description": "",
                })
                if len(seen_urls) >= 20:
                    break
        except Exception:
            pass

        browser.close()
    return articles


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
        title = a["title"].lower()
        if any(kw.lower() in title for kw in exclude_keywords):
            continue
        if include_keywords and not any(kw.lower() in title for kw in include_keywords):
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
                    "relevance": {"type": "string", "enum": ["high", "low"]},
                    "summary_kr": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": [
                    "index", "scope", "subject", "entity", "importance", "relevance", "summary_kr",
                ],
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
    """각 기사에 scope/subject/entity/importance/relevance를 LLM으로 채운다 (1회 배치 호출)."""
    if not articles:
        return articles

    def _prompt_line(i: int, a: dict) -> str:
        line = f"{i}. [{a['press']}] {a['title']}"
        desc = a.get("description")
        if desc:
            line += f"\n   설명: {desc}"
        return line

    numbered = "\n".join(_prompt_line(i, a) for i, a in enumerate(articles))
    prompt = f"""다음은 "{domain}" 관련 뉴스 기사 목록이다. 일부 기사는 제목 아래에 원문 설명(설명:)이
함께 제공된다. 각 기사를 아래 기준으로 분류하라.

- scope: 기사 내용이 국내 사업/이슈면 "국내", 해외 사업/이슈면 "해외"
- subject: 기사의 주체가 {domain} 자사 관련이면 "자사", 경쟁사 관련이면 "경쟁사",
  정부/제도/정책 관련이면 "정책", 특정 기업이 아닌 시장 전반 동향이면 "시장전반"
- entity: 기사에서 언급된 구체적 프로젝트명/기관명/기업명 (특정할 수 없으면 null)
- importance: 수주, 계약, 공시 관련 내용을 포함하면 "High", 그 외는 "Normal"
- relevance: 기사의 실질적 가치를 "high" 또는 "low"로 판단
  - "low": 단순 홍보/이벤트/사내소식/동정성 기사, 보도자료를 사실상 그대로 옮긴 기사,
    회사명이 스치듯 한 번만 언급되고 핵심 내용이 아닌 경우, Holtec/Westinghouse/Fermi/
    TerraPower 등 기업의 단순 채용공고·컨퍼런스 참가 소식처럼 현대건설 프로젝트와
    직접적 연관이 없는 기사
  - "high": 사업/실적/계약/정책/시장에 실질적 영향이 있는 내용
- summary_kr: 기사 제목이 영어로 작성된 경우에만 작성한다. 제목과 설명(있는 경우)에 담긴
  핵심 사실(주체/행위/시점/숫자)을 바탕으로 한국어 문장을 새로 구성해 2~3문장으로 요약한다.
  원문을 그대로 번역하거나 원문 문장 구조를 그대로 따라가지 않는다. 제목이 한글이면 null로 둔다.

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
        a["relevance"] = tag.get("relevance", "high")
        a["summary_kr"] = tag.get("summary_kr")
    return articles


# ---------------------------------------------------------------------------
# 7. 메인 오케스트레이터
# ---------------------------------------------------------------------------

def sort_articles(articles: list[dict]) -> list[dict]:
    """importance High 우선 → 오늘 발행 기사 우선 순으로 정렬한다.

    stable sort이므로 동일 키 그룹 내에서는 입력 순서(대개 최신순)가 보존된다.
    generate_daily_archive.py의 오케스트레이션 단계(카테고리 간 기사 이관·병합)에서도
    재사용한다.
    """
    return sorted(
        articles,
        key=lambda a: (
            0 if a.get("importance") == "High" else 1,
            0 if is_published_today(a.get("time", "")) else 1,
        ),
    )


def _to_output_article(a: dict, category_name: str) -> dict:
    return {
        "title": a["title"],
        "press": a["press"],
        "time": a.get("time", ""),
        "url": a.get("url", ""),
        "domain": a.get("domain", category_name),
        "scope": a.get("scope"),
        "subject": a.get("subject"),
        "entity": a.get("entity"),
        "importance": a.get("importance"),
        "relevance": a.get("relevance"),
        "summary_kr": a.get("summary_kr"),
        "source": a.get("source"),
        # 메인소스(공식 종목뉴스 피드)는 항상 Tier 1로 간주.
        # 보조소스는 화이트리스트 등급(tier1/tier2)을 그대로 반영.
        "tier": "t1" if a.get("source") == "main" else a.get("source_tier", "t2"),
        "verify_needed": a.get("verify_needed", False),
    }


def _crawl_category_impl(
    category_name: str,
    main_source_url: str,
    supplementary_query: str,
    include_keywords: list[str],
    exclude_keywords: list[str],
    tier1_whitelist: list[str],
    tier2_whitelist: list[str],
    max_articles: int = 7,
    foreign_sources: bool = False,
) -> tuple[list[dict], list[dict]]:
    """crawl_category() / crawl_category_debug()의 공통 구현.

    (최종 포함된 기사, relevance="low"로 제외된 기사) 튜플을 반환한다.
    foreign_sources=True면 fetch_foreign_sources()(원자력 전용 해외 RSS/스크래핑)
    결과를 보조소스에 합쳐 동일한 필터링/태깅 파이프라인을 태운다.
    """

    # main_source_url 예: "https://finance.naver.com/item/news_news.naver?code=000720"
    code_match = re.search(r"code=(\d+)", main_source_url or "")
    stock_code = code_match.group(1) if code_match else None

    main_raw = fetch_main_source(stock_code, pages=2)
    supp_raw = fetch_supplementary_source(supplementary_query)
    if foreign_sources:
        supp_raw = supp_raw + fetch_foreign_sources()

    # 1차 필터 (포함/제외 키워드). relevance가 2차 필터 역할을 하므로
    # exclude_keywords는 명백한 노이즈(스포츠단, 광고성 문구 등) 정도만 최소로 둔다.
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

    # LLM 태깅 (domain/scope/subject/entity/importance/relevance)
    client = anthropic.Anthropic()
    all_articles = tag_articles_with_llm(all_articles, category_name, client)

    # relevance 2차 필터: 홍보성/사소한 기사는 exclude_keywords 없이도 걸러낸다
    included = [a for a in all_articles if a.get("relevance", "high") != "low"]
    excluded_low_relevance = [a for a in all_articles if a.get("relevance", "high") == "low"]

    # verify_needed 플래그: ① Tier2 매체 + 제목에 "단독" 포함 → importance Normal 강등,
    # ② 기업이 직접 발행한 보도자료(Holtec/Westinghouse/TerraPower/Fermi America)는
    #    홍보성 가능성이 있어 무조건 verify_needed (importance는 그대로 둔다)
    for a in included:
        verify_needed = False
        if a.get("source_tier") == "tier2" and "단독" in a["title"]:
            a["importance"] = "Normal"
            verify_needed = True
        if a.get("press") in _COMPANY_PR_PRESS_NAMES:
            verify_needed = True
        a["verify_needed"] = verify_needed

    included = sort_articles(included)
    final_articles = included[:max_articles]

    return (
        [_to_output_article(a, category_name) for a in final_articles],
        [_to_output_article(a, category_name) for a in excluded_low_relevance],
    )


def crawl_category(
    category_name: str,
    main_source_url: str,
    supplementary_query: str,
    include_keywords: list[str],
    exclude_keywords: list[str],
    tier1_whitelist: list[str],
    tier2_whitelist: list[str],
    max_articles: int = 7,
    foreign_sources: bool = False,
) -> list[dict]:
    """카테고리별 종목뉴스를 크롤링/필터링/태깅해 최종 기사 리스트를 반환한다."""
    included, _ = _crawl_category_impl(
        category_name, main_source_url, supplementary_query,
        include_keywords, exclude_keywords, tier1_whitelist, tier2_whitelist,
        max_articles, foreign_sources,
    )
    return included


def crawl_category_debug(
    category_name: str,
    main_source_url: str,
    supplementary_query: str,
    include_keywords: list[str],
    exclude_keywords: list[str],
    tier1_whitelist: list[str],
    tier2_whitelist: list[str],
    max_articles: int = 7,
    foreign_sources: bool = False,
) -> tuple[list[dict], list[dict]]:
    """crawl_category()와 동일하지만 relevance="low"로 제외된 기사도 함께 반환한다.

    (최종 포함된 기사, 제외된 기사) — 필터링이 실제로 타당한지 육안 검증할 때 사용.
    """
    return _crawl_category_impl(
        category_name, main_source_url, supplementary_query,
        include_keywords, exclude_keywords, tier1_whitelist, tier2_whitelist,
        max_articles, foreign_sources,
    )


# ---------------------------------------------------------------------------
# 카테고리 1~5 스펙 (generate_daily_archive.py에서 import해서 사용)
# ---------------------------------------------------------------------------
# 각 카테고리마다 화이트리스트/키워드가 서로 다른 전문 매체·용어를 쓰므로 공용
# 상수로 묶지 않고 카테고리별로 그대로 명시한다. relevance가 2차 필터 역할을
# 하므로 exclude_keywords는 명백한 노이즈만 최소로 유지한다.

CATEGORY_CONFIGS = [
    dict(
        category_name="현대건설",
        main_source_url="https://finance.naver.com/item/news_news.naver?code=000720",
        supplementary_query="현대건설 OR 현대엔지니어링",
        include_keywords=[
            "수주", "계약", "실적", "공시", "배당", "인수", "지분", "소송", "MOU", "착공", "준공",
        ],
        exclude_keywords=["구단", "야구단", "축구단", "테마주", "급등주"],
        tier1_whitelist=["대한경제", "건설경제", "매일경제", "한국경제", "서울경제"],
        tier2_whitelist=["더그루", "이데일리", "뉴스핌", "파이낸셜뉴스"],
        # generate_daily_archive.py에서 subject=="자사"만 남기고 재분류하므로,
        # 재분류 후에도 상위 7건이 충분히 남도록 넉넉하게 수집한다.
        max_articles=15,
    ),
    dict(
        category_name="건설업",
        main_source_url="",  # 특정 종목이 없어 보조소스만 사용
        supplementary_query="건설업 OR GS건설 OR DL이앤씨 OR 삼성E&A OR 삼성물산 OR HDC현대산업개발",
        include_keywords=[
            "수주", "인허가", "PF", "미분양", "시공능력평가", "금리", "국토부",
            "GS건설", "DL이앤씨", "삼성E&A", "삼성물산", "HDC현대산업개발", "현대산업개발",
        ],
        exclude_keywords=["스포츠단", "야구단", "축구단"],
        tier1_whitelist=["대한경제", "건설경제", "매일경제", "한국경제", "서울경제"],
        tier2_whitelist=["이데일리", "더구루", "뉴스핌"],
        max_articles=7,
    ),
    dict(
        category_name="원자력",
        main_source_url="",
        supplementary_query="원전 OR SMR OR 원자력",
        include_keywords=[
            "SMR", "원전 수주", "원전 수출", "AP1000", "우라늄", "한수원", "웨스팅하우스",
            "FRMI", "CCJ", "CEG", "OKLO", "두산에너빌리티", "한전기술",
            # 해외 소스(RSS) 영문 기사용 키워드
            "Holtec", "Palisades", "Westinghouse", "Fermi", "Fermi America", "Matador",
            "TerraPower", "Natrium", "Kozloduy", "Bulgaria nuclear", "NRC license", "DOE nuclear",
        ],
        exclude_keywords=["탈원전 찬반", "사설"],
        # 원자력 전문지(에너지신문/전기신문/World Nuclear News)는 실제 네이버뉴스
        # 검색 결과에 거의 노출되지 않아, 실제로 등장하는 주요 경제지를 추가했다.
        # World Nuclear News/Reuters/DOE/NRC는 fetch_foreign_sources()로도 직접 수집된다.
        tier1_whitelist=[
            "에너지신문", "전기신문", "World Nuclear News",
            "연합뉴스", "매일경제", "헤럴드경제", "서울경제", "뉴시스",
            "Reuters", "DOE", "NRC",
        ],
        tier2_whitelist=[
            "조선비즈", "뉴스1", "파이낸셜뉴스", "머니투데이", "이데일리",
            "Utility Dive", "POWER Magazine", "Holtec", "Westinghouse", "TerraPower", "Fermi America",
        ],
        max_articles=7,
        foreign_sources=True,
    ),
    dict(
        category_name="도시정비",
        main_source_url="",
        supplementary_query="재개발 OR 재건축 OR 정비사업",
        include_keywords=["공공재개발", "재건축", "통합심의", "시공사 선정", "정비구역"],
        exclude_keywords=["시세 홍보"],
        # 대한경제/건설경제 단독으로는 결과가 거의 없어, 실제로 등장하는 매체를 추가했다.
        tier1_whitelist=["대한경제", "건설경제", "뉴시스", "연합뉴스", "한국경제", "머니투데이"],
        tier2_whitelist=["이데일리", "뉴스1", "아시아경제", "브릿지경제"],
        max_articles=7,
    ),
    dict(
        category_name="자본시장",
        main_source_url="",
        supplementary_query="현대건설 AND (목표주가 OR 회사채 OR 신용등급)",
        # "목표주가"는 기사 제목에 "목표가"로 줄여 쓰이는 경우가 대부분이라 별도로 추가했다.
        include_keywords=["목표주가", "목표가", "투자의견", "회사채", "CB", "신용등급", "배당"],
        exclude_keywords=["개인 SNS"],
        # 한국경제/매일경제/이데일리/뉴스핌 단독으로는 결과가 거의 없어, 실제로 등장하는
        # 매체를 추가했다.
        tier1_whitelist=["한국경제", "매일경제", "조선비즈", "이투데이"],
        tier2_whitelist=["이데일리", "뉴스핌", "뉴스1", "에너지경제", "연합인포맥스"],
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
