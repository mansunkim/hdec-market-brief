"""일회성 디버그 스크립트: 화이트리스트 게이트->신호 전환이 실제로 동작하는지
확인한다. 아시아투데이/더벨을 tier2에서 일부러 빼고, seen_urls 전역 중복
제거는 건너뛴 채(오늘 이미 한 번 수집돼 seen_urls.json에 있으므로) 파이프라인의
나머지 단계(키워드 필터 -> 화이트리스트 신호 부여 -> dedup -> LLM 태깅)를
그대로 재현해, known_press 판단으로 정상 통과 + verify_needed=true("확인필요")가
붙는지 확인한다. 실제 config.json/CATEGORY_CONFIGS/seen_urls.json은 건드리지
않는다. 확인 후 삭제한다."""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import anthropic
from stock_news_crawler import (
    CATEGORY_CONFIGS, fetch_main_source, fetch_supplementary_source,
    apply_keyword_filter, apply_whitelist_filter, dedup_cross_source,
    dedup_within_source, tag_articles_with_llm, sort_articles, _to_output_article,
)

cfg = dict(next(c for c in CATEGORY_CONFIGS if c["category_name"] == "현대건설"))
cfg["tier2_whitelist"] = [t for t in cfg["tier2_whitelist"] if t not in ("아시아투데이", "더벨")]
print("테스트용 tier2_whitelist:", cfg["tier2_whitelist"])

main_raw = fetch_main_source("000720", pages=2)
supp_raw = fetch_supplementary_source(cfg["supplementary_query"])

main_filtered = apply_keyword_filter(main_raw, cfg["include_keywords"], cfg["exclude_keywords"])
supp_filtered = apply_keyword_filter(supp_raw, cfg["include_keywords"], cfg["exclude_keywords"])
supp_whitelisted = apply_whitelist_filter(supp_filtered, cfg["tier1_whitelist"], cfg["tier2_whitelist"])
supp_deduped = dedup_cross_source(main_filtered, supp_whitelisted, threshold=0.7)
main_merged = dedup_within_source(main_filtered, threshold=0.8)
supp_merged = dedup_within_source(supp_deduped, threshold=0.8)
all_articles = main_merged + supp_merged

targets = ["20260727010009445", "202607221048575840101993"]
print("\n=== 태깅 전 후보 중 대상 기사 source_tier ===")
for a in all_articles:
    if any(t in a.get("url", "") for t in targets):
        print(" -", a["press"], "| source_tier:", a["source_tier"], "|", a["title"])

client = anthropic.Anthropic()
all_articles = tag_articles_with_llm(all_articles, "현대건설", client)

for a in all_articles:
    if a.get("source_tier") == "unlisted" and not a.get("known_press", True):
        a["relevance"] = "low"

included = [a for a in all_articles if a.get("relevance", "high") != "low"]
excluded = [a for a in all_articles if a.get("relevance", "high") == "low"]

for a in included:
    verify_needed = False
    if a.get("source_tier") == "tier2" and "단독" in a["title"]:
        verify_needed = True
    if a.get("source_tier") == "unlisted":
        verify_needed = True
    a["verify_needed"] = verify_needed

included = sort_articles(included)
output_included = [_to_output_article(a, "현대건설") for a in included]
output_excluded = [_to_output_article(a, "현대건설") for a in excluded]

print(f"\n=== included ({len(output_included)}건) ===")
for a in output_included:
    print(" -", a["press"], "| tier:", a["tier"], "| verify_needed:", a["verify_needed"], "|", a["title"])

print(f"\n=== excluded relevance=low ({len(output_excluded)}건) ===")
for a in output_excluded:
    print(" -", a["press"], "|", a["title"])

print("\n=== 대상 기사 최종 상태 ===")
for a in output_included:
    if any(t in a.get("url", "") for t in targets):
        print("INCLUDED:", a["press"], "| tier:", a["tier"], "| verify_needed:", a["verify_needed"])
for a in output_excluded:
    if any(t in a.get("url", "") for t in targets):
        print("EXCLUDED (relevance=low):", a["press"])
