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
    - "최신순" 정렬은 sort_articles()/_parse_article_datetime()이 상대/절대
      시각 표기를 모두 datetime으로 변환해 처리합니다 (여러 소스가 섞여도
      실제 발행시각 기준으로 정렬됨).
"""

import html
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import anthropic
import requests
from playwright.sync_api import sync_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 1. 메인 소스 — 네이버 금융 종목뉴스 (Playwright)
# ---------------------------------------------------------------------------

def _fetch_body_snippet(url: str, timeout: float = 5.0) -> str:
    """exclude_keywords 검사용으로 기사 본문 텍스트를 가져온다.

    네이버 금융 종목뉴스 피드(finance.naver.com/item/news_news.naver)는 본문에
    종목명이 스치듯 언급되기만 해도(예: "HD현대건설"처럼 부분 문자열이 겹치는
    별개 회사) 그 종목의 뉴스로 잘못 잡아오는 경우가 있다. 제목만으로는 이런
    오분류를 걸러낼 수 없어(제목엔 회사명이 아예 안 나오는 경우도 많음) 본문을
    가져와 exclude_keywords 매칭에 함께 쓴다. 네트워크 실패는 파이프라인을
    막아선 안 되므로 빈 문자열로 넘어간다."""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        return _strip_html(resp.text)
    except Exception:
        return ""


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
                    "body_snippet": _fetch_body_snippet(link),
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
                # 네이버 검색 UI는 "3시간 전"처럼 상대시각을 준다. 이 문자열을 그대로
                # 저장하면, 같은 세션이 재실행되어 누적 병합되거나 몇 시간 뒤 다시
                # 정렬할 때 "지금 기준 3시간 전"으로 잘못 재해석되어 정렬이 틀어진다.
                # 수집 시점에 절대시각으로 고정해둔다 (메인소스·해외 RSS는 이미 절대시각을 준다).
                time_text = _freeze_time(time_text)

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


def _parse_article_datetime(time_text: str) -> datetime:
    """기사 시각 문자열(상대/절대 표기 모두)을 정렬 가능한 datetime으로 변환한다.

    메인소스("2026.07.26 16:49"), 네이버뉴스 검색의 상대시각("3시간 전", "어제",
    "2일 전"), 해외 RSS를 메인소스 형식으로 맞춘 값을 모두 처리한다. 형식을
    알 수 없거나(예: 날짜 정보가 없는 일부 해외 소스) 비어 있으면 정렬 시 맨
    뒤로 밀리도록 datetime.min을 반환한다.
    """
    text = (time_text or "").strip()
    if not text:
        return datetime.min

    now = datetime.now()

    m = re.search(r"(\d+)\s*분\s*전$", text)
    if m:
        return now - timedelta(minutes=int(m.group(1)))

    m = re.search(r"(\d+)\s*시간\s*전$", text)
    if m:
        return now - timedelta(hours=int(m.group(1)))

    if text.startswith("어제"):
        return now - timedelta(days=1)

    m = re.search(r"(\d+)\s*일\s*전$", text)
    if m:
        return now - timedelta(days=int(m.group(1)))

    # 절대시각: "2026.07.26" 또는 "2026.07.26 16:49" 형태
    m = re.match(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", text)
    if m:
        try:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hour = int(m.group(4)) if m.group(4) else 0
            minute = int(m.group(5)) if m.group(5) else 0
            return datetime(year, month, day, hour, minute)
        except ValueError:
            return datetime.min

    return datetime.min


def _freeze_time(time_text: str) -> str:
    """상대시각("3시간 전")을 절대시각("YYYY.MM.DD HH:MM")으로 수집 시점에 고정한다.

    상대시각 문자열을 그대로 저장하면, 같은 세션이 재실행되어 누적 병합되거나
    몇 시간 뒤 다시 정렬할 때 "지금 기준 N시간 전"으로 잘못 재해석돼 정렬이
    틀어진다. 이미 절대시각이거나 파싱할 수 없으면 원본 문자열을 그대로 둔다.
    """
    if not time_text:
        return time_text
    dt = _parse_article_datetime(time_text)
    if dt == datetime.min:
        return time_text
    return dt.strftime("%Y.%m.%d %H:%M")


# 하루 1회(06:40 KST) 실행 기준. 네이버뉴스 검색(fetch_supplementary_source)은
# "최신순"이 아니라 "관련도순"으로 결과를 주므로, 키워드와 관련도는 높지만
# 발행일은 훨씬 오래된 기사(예: 몇 주 전 SMR 특집기사)가 섞여 들어올 수 있다.
# seen_urls 전역 중복 제거는 "이미 한 번이라도 결과에 포함됐던 기사"만 걸러낼 뿐
# "이번에 처음 보이지만 사실은 오래된 기사"는 걸러내지 못하므로, 발행시각 기준
# 최근 36시간(하루 주기 + 여유분) 이내 기사만 남긴다.
_MAX_ARTICLE_AGE_HOURS = 36


def filter_recent(articles: list[dict], max_age_hours: float = _MAX_ARTICLE_AGE_HOURS) -> list[dict]:
    """발행시각이 max_age_hours 이내인 기사만 남긴다.

    시각을 알 수 없는 기사(TerraPower 뉴스 목록 스크래핑처럼 애초에 시각 정보가
    없는 소스, _parse_article_datetime이 datetime.min을 반환하는 경우)는 오래됐다고
    단정할 수 없으므로 걸러내지 않고 그대로 통과시킨다.
    """
    now = datetime.now()
    kept = []
    for a in articles:
        dt = _parse_article_datetime(a.get("time", ""))
        if dt == datetime.min or (now - dt) <= timedelta(hours=max_age_hours):
            kept.append(a)
    return kept


# ---------------------------------------------------------------------------
# 2c. 전역 중복 제거 — data/seen_urls.json (실행 간 이미 결과에 포함된 기사 추적)
# ---------------------------------------------------------------------------
# crawl_category()는 실행할 때마다 소스를 처음부터 다시 조회한다(이건 막을 수
# 없다 — 네이버/RSS 어느 쪽도 "이후 새 글만" API를 제공하지 않는다). 문제는
# 그렇게 다시 조회된 기사 중 "직전 실행에서 이미 결과에 포함됐던 기사"까지
# 매번 다시 LLM으로 재태깅하고 다시 결과에 올렸다는 점이다. 이 기록 파일에
# "지금까지 한 번이라도 어떤 카테고리 결과에든 포함됐던 기사"의 URL(없으면
# 정규화된 제목)을 누적해두고, LLM 태깅 전에 걸러내 재조회 자체는 그대로 두되
# 재태깅·재노출만 막는다.

_SEEN_URLS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "seen_urls.json")
_SEEN_URLS_RETENTION_DAYS = 90


def _normalize_title_key(title: str) -> str:
    """URL이 없는 기사를 위한 폴백 키. dedup_within_source가 붙이는
    "외 N개 언론사 보도" 접미사는 실행마다 N이 달라질 수 있어 제거하고,
    공백/대소문자 차이도 무시한다."""
    t = re.sub(r"\s*외\s*\d+개\s*언론사\s*보도\s*$", "", title or "")
    return re.sub(r"\s+", "", t).lower()


def _seen_key(article: dict) -> str:
    url = (article.get("url") or "").strip()
    if url:
        return url
    return "title:" + _normalize_title_key(article.get("title", ""))


def _load_seen_urls() -> dict:
    """{key: 최초로 본 시각(isoformat)} 형태. 파일이 없거나 손상됐으면 빈 dict."""
    if not os.path.exists(_SEEN_URLS_PATH):
        return {}
    try:
        with open(_SEEN_URLS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_seen_urls(seen: dict) -> None:
    """저장 시마다 90일 지난 항목은 함께 정리해, 별도 정리 단계 없이도
    파일이 무한정 커지지 않게 한다."""
    os.makedirs(os.path.dirname(_SEEN_URLS_PATH), exist_ok=True)
    cutoff = (datetime.now() - timedelta(days=_SEEN_URLS_RETENTION_DAYS)).isoformat()
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    with open(_SEEN_URLS_PATH, "w", encoding="utf-8") as f:
        json.dump(pruned, f, ensure_ascii=False, indent=2)


def _filter_unseen(articles: list[dict], seen: dict) -> list[dict]:
    return [a for a in articles if _seen_key(a) not in seen]


def _mark_seen(articles: list[dict], seen: dict) -> None:
    now_iso = datetime.now().isoformat()
    for a in articles:
        seen[_seen_key(a)] = now_iso


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
        # exclude_keywords는 제목뿐 아니라 본문 스니펫(main_source만 해당,
        # _fetch_body_snippet 참고)도 함께 검사한다. "HD현대건설"처럼 종목명이
        # 제목에는 안 나오고 본문에만 스치듯 언급되는 다른 회사 기사를 걸러내려면
        # 제목만으로는 부족하기 때문이다.
        exclude_haystack = title + " " + (a.get("body_snippet") or "").lower()
        if any(kw.lower() in exclude_haystack for kw in exclude_keywords):
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
    """화이트리스트는 더 이상 게이트(자동 탈락)가 아니라 참고 신호다.

    Tier1/Tier2 목록에 없는 언론사라고 매번 놓치는 기사가 생겼다(아시아투데이/
    더벨 사례). 대신 목록에 없는 기사는 걸러내지 않고 통과시키되
    source_tier="unlisted"로 표시해 LLM 태깅 단계로 넘긴다. 실제 포함 여부는
    tag_articles_with_llm()이 판단하는 known_press(출처가 알려진 정상
    언론사인지)에 맡기고, known_press==True인 경우 verify_needed로 표시해
    화이트리스트 정식 등록 전까지 계속 "확인필요"로 노출한다."""
    for a in articles:
        press = a["press"]
        if press in tier1_whitelist:
            a["source_tier"] = "tier1"
        elif press in tier2_whitelist:
            a["source_tier"] = "tier2"
        else:
            a["source_tier"] = "unlisted"
    return articles


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


def apply_title_group_caps(
    articles: list[dict],
    caps: list[tuple[list[str], int]],
) -> list[dict]:
    """제목에 특정 키워드가 포함된 기사의 노출 건수를 제한한다.

    예: 원자력 카테고리에서 "두산에너빌리티" 관련 기사가 개별 종목 뉴스량이
    압도적으로 많아 웨스팅하우스/홀텍/테라파워/페르미 아메리카 등 현대건설이
    실제로 관심 있는 다른 뉴스를 밀어내는 문제가 있었다. 완전히 제외하지는
    않되(경쟁사의 해외 수주 등은 여전히 중요한 정보이므로) 한도를 넘는 기사는
    뒤로 미룬다. articles는 최신순 정렬을 가정하므로 한도 안에 드는 것은 항상
    최신 기사부터다.

    한도로 밀려난 기사는 버리지 않고 (한도 통과 기사, 밀려난 기사) 튜플로
    반환해, 호출부에서 전체 개수가 max_articles에 못 미치면 다시 채워 넣을 수
    있게 한다(원자력 기사 자체가 적은 날 화면이 비지 않도록).
    """
    counts = [0] * len(caps)
    kept, overflow = [], []
    for a in articles:
        title_lower = a["title"].lower()
        matched_idx = next(
            (i for i, (keywords, _) in enumerate(caps)
             if any(kw.lower() in title_lower for kw in keywords)),
            None,
        )
        if matched_idx is None:
            kept.append(a)
            continue
        limit = caps[matched_idx][1]
        if counts[matched_idx] < limit:
            counts[matched_idx] += 1
            kept.append(a)
        else:
            overflow.append(a)
    return kept, overflow


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
                    "known_press": {"type": "boolean"},
                    "summary_kr": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": [
                    "index", "scope", "subject", "entity", "importance", "relevance",
                    "known_press", "summary_kr",
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
        unlisted = " (화이트리스트 미등록 언론사 — known_press 판단 필요)" if a.get("source_tier") == "unlisted" else ""
        line = f"{i}. [{a['press']}]{unlisted} {a['title']}"
        desc = a.get("description")
        if desc:
            line += f"\n   설명: {desc}"
        return line

    numbered = "\n".join(_prompt_line(i, a) for i, a in enumerate(articles))

    # 자본시장 카테고리는 "개별 종목이 아닌 시장 전반 시황" 위주로 재정의됐다. domain을
    # 그대로 "자사" 판단 기준에 넣으면 "자본시장 자사"라는 의미 없는 기준이 되므로,
    # 이 카테고리만 현대건설을 명시하고 subject/entity/importance 기준을 별도로 준다.
    if domain == "자본시장":
        subject_rule = (
            '- subject: 기사가 현대건설 자체(목표주가/투자의견/신용등급/회사채 등)에 대한 것이면 "자사",\n'
            '  다른 특정 기업에 대한 것이면 "경쟁사", 정부/제도/정책 관련이면 "정책",\n'
            '  특정 기업이 아닌 코스피/코스닥/미국증시 등 시장 전체 흐름이면 "시장전반"\n'
            '  (이 카테고리는 시장 전반 기사 위주로 수집되므로 "시장전반"이 기본값에 가깝다)'
        )
        entity_rule = (
            '- entity: 기사에서 언급된 주요 지수명 또는 매크로 이벤트명(예: "코스피", "나스닥",\n'
            '  "FOMC", "국채금리 급등" 등). 특정할 수 없으면 null'
        )
        importance_rule = (
            '- importance: 코스피/코스닥/미국증시(다우존스·나스닥·S&P500)가 1% 이상 변동했거나,\n'
            '  기준금리·국채금리·환율 등 주요 매크로 이벤트가 있었던 경우 "High", 그 외는 "Normal"'
        )
    else:
        subject_rule = (
            f'- subject: 기사의 주체가 {domain} 자사 관련이면 "자사", 경쟁사 관련이면 "경쟁사",\n'
            '  정부/제도/정책 관련이면 "정책", 특정 기업이 아닌 시장 전반 동향이면 "시장전반"'
        )
        entity_rule = '- entity: 기사에서 언급된 구체적 프로젝트명/기관명/기업명 (특정할 수 없으면 null)'
        importance_rule = '- importance: 수주, 계약, 공시 관련 내용을 포함하면 "High", 그 외는 "Normal"'

    prompt = f"""다음은 "{domain}" 관련 뉴스 기사 목록이다. 일부 기사는 제목 아래에 원문 설명(설명:)이
함께 제공된다. 각 기사를 아래 기준으로 분류하라.

- scope: 기사 내용이 국내 사업/이슈면 "국내", 해외 사업/이슈면 "해외"
{subject_rule}
{entity_rule}
{importance_rule}
- relevance: 기사의 실질적 가치를 "high" 또는 "low"로 판단
  - "low": 단순 홍보/이벤트/사내소식/동정성 기사, 보도자료를 사실상 그대로 옮긴 기사,
    회사명이 스치듯 한 번만 언급되고 핵심 내용이 아닌 경우, Holtec/Westinghouse/Fermi/
    TerraPower 등 기업의 단순 채용공고·컨퍼런스 참가 소식처럼 현대건설 프로젝트와
    직접적 연관이 없는 기사
  - "high": 사업/실적/계약/정책/시장에 실질적 영향이 있는 내용. 회사의 실적·사업전략을
    분석하는 특집/인터뷰 형식 기사나, thebell·IB토마토 등 자본시장 전문지의 데스크
    칼럼("[OO desk]" 등)이라도 CB·회사채·신용등급 등 해당 회사의 실제 자금조달·재무
    이슈를 다루는 내용이면 형식과 무관하게 "high"로 판단한다 (칼럼 형식이라는 이유만으로
    "low"로 낮추지 않는다)
- summary_kr: 기사 제목이 영어로 작성된 경우에만 작성한다. 제목과 설명(있는 경우)에 담긴
  핵심 사실(주체/행위/시점/숫자)을 바탕으로 한국어 문장을 새로 구성해 2~3문장으로 요약한다.
  원문을 그대로 번역하거나 원문 문장 구조를 그대로 따라가지 않는다. 제목이 한글이면 null로 둔다.
- known_press: "(화이트리스트 미등록 언론사 — known_press 판단 필요)"라고 표시된 기사에 대해,
  press가 일반적으로 알려진 정상 국내/해외 언론사(신문사·통신사·방송사·전문 매체 등)면 true,
  개인 블로그·광고성 사이트·출처 불명 매체처럼 언론사로 보기 어려우면 false로 판단한다.
  표시가 없는(이미 화이트리스트에 등록된) 기사는 항상 true로 둔다.

기사 목록:
{numbered}
"""

    response = client.messages.create(
        model="claude-sonnet-5",
        # summary_kr(영문 기사 국문 요약) 추가 이후 원자력 카테고리처럼 기사 수가
        # 많고 영문 비중이 높은 경우 4096으로는 응답이 중간에 잘려 JSON 파싱이
        # 실패했다 (Unterminated string). 여유 있게 상향한다.
        max_tokens=16000,
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
        a["known_press"] = tag.get("known_press", True)
        a["summary_kr"] = tag.get("summary_kr")
    return articles


# ---------------------------------------------------------------------------
# 7. 메인 오케스트레이터
# ---------------------------------------------------------------------------

def sort_articles(articles: list[dict]) -> list[dict]:
    """발행시각 기준 최신순(내림차순)으로 정렬한다.

    _parse_article_datetime()으로 상대/절대 시각 표기를 모두 실제 datetime으로
    바꿔 비교하므로, 메인소스/보조소스/해외 RSS 등 서로 다른 소스가 섞여도
    실제 최신순으로 정렬된다. 시각을 알 수 없는 기사는 맨 뒤로 밀린다.
    generate_daily_archive.py의 오케스트레이션 단계(카테고리 간 기사 이관·병합)에서도
    재사용한다.
    """
    return sorted(articles, key=lambda a: _parse_article_datetime(a.get("time", "")), reverse=True)


def dedup_by_entity(articles: list[dict], title_threshold: float = 0.45) -> list[dict]:
    """같은 사안(같은 프로젝트/기관/기업)을 다루는 기사가 언론사마다 제목을
    전혀 다르게 써서 dedup_within_source(제목 유사도만 보는, 임계값 0.8)로는
    못 걸러지는 중복을 잡는다. 예: "힐스테이트 회룡역파크뷰 1순위 청약 흥행"과
    "현대건설, 회룡역파크뷰 정당계약 돌입"은 제목 유사도는 낮지만 LLM이 매긴
    entity(프로젝트명/기관명/기업명)는 둘 다 "힐스테이트 회룡역파크뷰"로 같다.

    LLM 태깅(tag_articles_with_llm) 이후에만 호출 가능하다(entity가 그 전엔
    없음). articles는 최신순 정렬되어 있다고 가정 — 그룹의 첫 기사(최신)만
    남기고 나머지는 "외 N건"으로 병합한다. entity가 없는(null/빈 문자열)
    기사는 과도한 병합을 막기 위해 대상에서 제외하고 그대로 통과시킨다.
    entity만으로는 같은 회사/기관이 등장하는 서로 다른 사안까지 묶어버릴 수
    있어(예: "두산에너빌리티 3분기 실적" vs "두산에너빌리티 체코 원전 수주"),
    제목도 어느 정도(title_threshold) 겹칠 때만 같은 사안으로 판단한다.
    """
    groups: list[list[dict]] = []
    passthrough: list[dict] = []
    for a in articles:
        entity = (a.get("entity") or "").strip().lower()
        if not entity:
            passthrough.append(a)
            continue
        placed = False
        for g in groups:
            g_entity = (g[0].get("entity") or "").strip().lower()
            # 그룹 대표(g[0])하고만 비교하면, 같은 사안이라도 헤드라인이 점점
            # 달라지는 기사들이 연달아 묶이지 못하고 각자 새 그룹으로 흩어진다
            # (A-B, B-C는 유사해도 A-C는 안 유사한 경우가 실제로 흔함). 그룹 내
            # 아무 기사와나 유사하면 합류시켜 연쇄적으로 묶이게 한다.
            if g_entity == entity and any(
                _title_similarity(a["title"], m["title"]) >= title_threshold for m in g
            ):
                g.append(a)
                placed = True
                break
        if not placed:
            groups.append([a])

    merged = []
    for g in groups:
        kept = dict(g[0])  # 최신순 정렬 입력이므로 그룹의 첫 기사 = 최신 기사
        if len(g) > 1:
            kept["title"] = f"{kept['title']} 외 {len(g) - 1}건"
        merged.append(kept)
    return merged + passthrough


def _tier_label(a: dict) -> str:
    if a.get("source") == "main" or a.get("source_tier") == "tier1":
        return "t1"
    if a.get("source_tier") == "tier2":
        return "t2"
    return "unlisted"


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
        # 보조소스는 apply_whitelist_filter가 매긴 "tier1"/"tier2"/"unlisted" 값을
        # HTML이 기대하는 짧은 형식("t1"/"t2"/"unlisted")으로 정규화한다.
        "tier": _tier_label(a),
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
    title_group_caps: list[tuple[list[str], int]] | None = None,
    entity_merge_threshold: float = 0.45,
) -> tuple[list[dict], list[dict]]:
    """crawl_category() / crawl_category_debug()의 공통 구현.

    (최종 포함된 기사, relevance="low"로 제외된 기사) 튜플을 반환한다.
    foreign_sources=True면 fetch_foreign_sources()(원자력 전용 해외 RSS/스크래핑)
    결과를 보조소스에 합쳐 동일한 필터링/태깅 파이프라인을 태운다.
    title_group_caps가 주어지면 apply_title_group_caps()로 특정 키워드 기사의
    노출 건수를 제한한다(원자력 카테고리의 두산에너빌리티 쏠림 완화용).
    entity_merge_threshold는 dedup_by_entity()에 그대로 전달된다. 자본시장처럼
    entity가 회사/프로젝트가 아니라 "코스피"·"연준"류 거시 지표인 카테고리는
    같은 날 같은 지표를 다루면 사실상 항상 같은 마감 시황이라 0.0(제목 유사도
    안 보고 entity만 일치해도 병합)으로 낮춰 쓴다.
    """

    # main_source_url 예: "https://finance.naver.com/item/news_news.naver?code=000720"
    code_match = re.search(r"code=(\d+)", main_source_url or "")
    stock_code = code_match.group(1) if code_match else None

    main_raw = fetch_main_source(stock_code, pages=2)
    supp_raw = fetch_supplementary_source(supplementary_query)
    if foreign_sources:
        supp_raw = supp_raw + fetch_foreign_sources()

    # 네이버뉴스 검색(보조소스)은 최신순이 아니라 관련도순으로 결과를 주기 때문에
    # 키워드와는 관련 있지만 발행일이 훨씬 오래된 기사가 섞여 들어올 수 있다.
    # 하루 1회 실행 기준으로 최근 36시간 이내 기사만 남겨, "과거 뉴스가 누적되는"
    # 문제를 원천 차단한다(시각을 알 수 없는 기사는 판단 불가라 통과시킴).
    main_raw = filter_recent(main_raw)
    supp_raw = filter_recent(supp_raw)

    # 1차 필터 (포함/제외 키워드). relevance가 2차 필터 역할을 하므로
    # exclude_keywords는 명백한 노이즈(스포츠단, 광고성 문구 등) 정도만 최소로 둔다.
    main_filtered = apply_keyword_filter(main_raw, include_keywords, exclude_keywords)
    supp_filtered = apply_keyword_filter(supp_raw, include_keywords, exclude_keywords)

    # 보조소스 언론사 화이트리스트 신호 부여 (Tier1/Tier2 외는 더 이상 자동 제외하지
    # 않고 source_tier="unlisted"로 표시만 해 LLM 태깅 단계(known_press)로 넘긴다)
    supp_whitelisted = apply_whitelist_filter(supp_filtered, tier1_whitelist, tier2_whitelist)

    # 메인소스와 제목 유사도 70% 이상 → 중복으로 간주해 보조소스에서 제외
    supp_deduped = dedup_cross_source(main_filtered, supp_whitelisted, threshold=0.7)

    # 동일 소스 내 제목 유사도 72% 이상 → 최신 1건만 남기고 병합. 언론사마다
    # 헤드라인을 조금씩 다르게 써서 기존 0.8 기준으로는 놓치는 중복이 있어
    # 낮췄다(마지막 안전망은 LLM 태깅 이후 dedup_by_entity가 담당).
    main_merged = dedup_within_source(main_filtered, threshold=0.72)
    supp_merged = dedup_within_source(supp_deduped, threshold=0.72)

    all_articles = main_merged + supp_merged

    # 전역 중복 제거: 직전 실행들에서 이미 결과에 포함됐던 기사는 LLM 태깅 전에
    # 걸러낸다 (재조회 자체는 막을 수 없지만, 재태깅·재노출은 막는다).
    seen_urls = _load_seen_urls()
    before_seen_filter = len(all_articles)
    all_articles = _filter_unseen(all_articles, seen_urls)
    print(
        f"[{category_name}] 후보 {before_seen_filter}건 중 이미 본 기사 "
        f"{before_seen_filter - len(all_articles)}건 제외, {len(all_articles)}건 신규 태깅 예정"
    )

    # LLM 태깅 (domain/scope/subject/entity/importance/relevance/known_press)
    client = anthropic.Anthropic()
    all_articles = tag_articles_with_llm(all_articles, category_name, client)

    # 화이트리스트 미등록(source_tier="unlisted") 언론사는 known_press==False(정상
    # 언론사로 보기 어려움)면 relevance를 "low"로 강제 덮어써 최종 제외한다.
    # known_press==True면 relevance 판단을 그대로 존중하고 verify_needed로만 표시한다.
    for a in all_articles:
        if a.get("source_tier") == "unlisted" and not a.get("known_press", True):
            a["relevance"] = "low"

    # relevance 2차 필터: 홍보성/사소한 기사·미등록 매체는 exclude_keywords 없이도 걸러낸다
    included = [a for a in all_articles if a.get("relevance", "high") != "low"]
    excluded_low_relevance = [a for a in all_articles if a.get("relevance", "high") == "low"]

    # verify_needed 플래그: ① Tier2 매체 + 제목에 "단독" 포함 → importance Normal 강등,
    # ② 기업이 직접 발행한 보도자료(Holtec/Westinghouse/TerraPower/Fermi America)는
    #    홍보성 가능성이 있어 무조건 verify_needed (importance는 그대로 둔다)
    # ③ 화이트리스트 미등록 매체(known_press==True로 통과한 경우)는 정식 등록 전까지
    #    계속 verify_needed로 노출한다
    for a in included:
        verify_needed = False
        if a.get("source_tier") == "tier2" and "단독" in a["title"]:
            a["importance"] = "Normal"
            verify_needed = True
        if a.get("press") in _COMPANY_PR_PRESS_NAMES:
            verify_needed = True
        if a.get("source_tier") == "unlisted":
            verify_needed = True
        a["verify_needed"] = verify_needed

    included = sort_articles(included)

    # 제목만으로는 못 잡는 "같은 사안, 다른 표현" 중복(예: 같은 분양 단지를
    # 언론사마다 다른 헤드라인으로 보도)을 LLM이 매긴 entity 기준으로 한 번 더
    # 걸러낸다. dedup_by_entity는 시간순을 그대로 보존하지 않으므로(entity 없는
    # 기사를 뒤로 몰아둠) 다시 정렬한다.
    included = sort_articles(dedup_by_entity(included, title_threshold=entity_merge_threshold))

    if title_group_caps:
        capped, overflow = apply_title_group_caps(included, title_group_caps)
        # 한도로 밀려난 기사라도 max_articles를 못 채우면 다시 채워 넣는다
        # (원자력 기사 자체가 적은 날 화면이 비지 않도록).
        if len(capped) < max_articles:
            capped = capped + overflow[: max_articles - len(capped)]
        included = capped

    final_articles = included[:max_articles]

    # 이번 실행 결과에 포함된 기사는 앞으로의 실행에서 "이미 본 기사"로
    # 취급되도록 기록한다 (같은 스크립트 실행 안에서 여러 카테고리가 순서대로
    # crawl_category()를 호출하면, 뒤에 도는 카테고리는 앞선 카테고리가 방금
    # 기록한 것까지 자연히 반영해서 본다).
    _mark_seen(final_articles, seen_urls)
    _save_seen_urls(seen_urls)

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
    title_group_caps: list[tuple[list[str], int]] | None = None,
    entity_merge_threshold: float = 0.45,
) -> list[dict]:
    """카테고리별 종목뉴스를 크롤링/필터링/태깅해 최종 기사 리스트를 반환한다."""
    included, _ = _crawl_category_impl(
        category_name, main_source_url, supplementary_query,
        include_keywords, exclude_keywords, tier1_whitelist, tier2_whitelist,
        max_articles, foreign_sources, title_group_caps, entity_merge_threshold,
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
    title_group_caps: list[tuple[list[str], int]] | None = None,
    entity_merge_threshold: float = 0.45,
) -> tuple[list[dict], list[dict]]:
    """crawl_category()와 동일하지만 relevance="low"로 제외된 기사도 함께 반환한다.

    (최종 포함된 기사, 제외된 기사) — 필터링이 실제로 타당한지 육안 검증할 때 사용.
    """
    return _crawl_category_impl(
        category_name, main_source_url, supplementary_query,
        include_keywords, exclude_keywords, tier1_whitelist, tier2_whitelist,
        max_articles, foreign_sources, title_group_caps, entity_merge_threshold,
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
        # "현대건설" 단독 검색은 네이버 정렬 특성상 목표주가/신용등급 등 리포트성 기사가
        # 상위 30건 밖으로 밀리는 경우가 많아, 예전 카테고리5가 쓰던 조합 검색어를
        # 그대로 가져와 목표주가/신용등급/회사채 기사를 직접 찾도록 추가했다.
        supplementary_query=(
            "현대건설 OR 현대엔지니어링 OR 현대건설 목표주가 OR 현대건설 신용등급 OR 현대건설 회사채"
        ),
        include_keywords=[
            "수주", "계약", "실적", "공시", "배당", "인수", "지분", "소송", "MOU", "착공", "준공",
            # 카테고리5(자본시장)가 개별종목 리포트가 아닌 시장 전반 시황 위주로 재정의되면서
            # 더 이상 "현대건설 AND 목표주가" 식으로 검색하지 않으므로, 현대건설 자체의
            # 목표주가/투자의견/신용등급/회사채 뉴스는 이 카테고리에서 직접 수집한다.
            "목표주가", "목표가", "투자의견", "신용등급", "회사채", "CB",
        ],
        exclude_keywords=["구단", "야구단", "축구단", "테마주", "급등주"],
        tier1_whitelist=["대한경제", "건설경제", "매일경제", "한국경제", "서울경제"],
        # 아시아투데이(일반 경제지)·더벨(자본시장/IR 전문지, CB·회사채 등 자금조달
        # 기사에 강함)이 화이트리스트에 없어 현대건설 자사 기사가 통째로 누락된
        # 사례(2026-07-27, "실적 반등" 아시아투데이 기사·"현대건설 CB" 더벨 기사)가
        # 있어 추가했다.
        tier2_whitelist=["더그루", "이데일리", "뉴스핌", "파이낸셜뉴스", "아시아투데이", "더벨"],
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
        supplementary_query=(
            "원전 OR SMR OR 원자력 OR 미국 원전 OR 웨스팅하우스 OR 홀텍 OR 테라파워 OR 페르미 아메리카"
        ),
        include_keywords=[
            "SMR", "원전 수주", "원전 수출", "원전 시장", "미국 원전", "해외 원전", "원전 정책",
            "AP1000", "우라늄", "한수원", "웨스팅하우스", "홀텍", "테라파워", "페르미 아메리카",
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
        # 두산에너빌리티는 국내 최대 원전 관련 상장사라 검색 결과량 자체가 압도적으로
        # 많아, 그대로 두면 현대건설이 실제로 관심 있는 웨스팅하우스/홀텍/테라파워/
        # 페르미 아메리카·미국 원전 시장 동향 기사를 밀어낸다. 완전히 빼지는 않되
        # (해외 수주 등은 여전히 중요한 정보) 최대 2건으로 제한해 나머지 슬롯을 확보한다.
        title_group_caps=[(["두산에너빌리티"], 2)],
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
        # 개별 종목 리포트가 아니라 "오늘 코스피/미국증시가 왜 오르고 내렸는지" 같은
        # 시장 전체 흐름/원인 기사를 수집한다 (현대건설 개별 자본시장 뉴스는
        # generate_daily_archive.py에서 subject=="자사" 기준으로 카테고리1로 이관됨).
        supplementary_query="코스피 마감 OR 뉴욕증시 마감 OR 국내증시 마감 OR 미국증시 마감",
        include_keywords=[
            "코스피", "코스닥", "다우존스", "나스닥", "S&P500",
            "연준", "기준금리", "국채금리", "환율", "유가",
            "외국인 순매수", "기관 순매수", "시황",
        ],
        exclude_keywords=["목표주가", "투자의견", "개별종목 리포트"],
        tier1_whitelist=["한국경제", "매일경제", "서울경제", "연합인포맥스"],
        tier2_whitelist=["이데일리", "뉴스핌", "한국투자증권 리서치"],
        max_articles=7,
        # 이 카테고리의 entity는 "코스피"/"연준"처럼 회사·프로젝트가 아니라 거시
        # 지표라, 같은 날 같은 지표를 다루면 사실상 항상 같은 마감 시황이다(반면
        # "두산에너빌리티"처럼 회사 entity는 하루에도 실적/수주 등 서로 다른
        # 사안이 여러 건 나올 수 있어 제목 유사도 확인이 필요함). 실제로 코스피
        # 마감 기사 6~7건이 언론사마다 표현만 달라 제목 유사도 0.45 기준으로는
        # 다 따로 남는 문제가 있어, 이 카테고리만 제목 유사도를 안 보고 entity
        # 일치만으로 병합한다.
        entity_merge_threshold=0.0,
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
