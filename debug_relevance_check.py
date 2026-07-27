"""일회성 디버그 스크립트: 현대건설 카테고리에서 relevance="low"로 제외된
기사 목록을 출력한다. 특정 두 기사(아시아투데이 실적 반등, 더벨 CB)가 어느
단계에서 빠지는지 최종 확인하기 위한 용도이며, 원인 확인 후 삭제한다."""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from stock_news_crawler import crawl_category_debug, CATEGORY_CONFIGS

cfg = next(c for c in CATEGORY_CONFIGS if c["category_name"] == "현대건설")
included, excluded_low = crawl_category_debug(**cfg)

print(f"=== included (relevance=high, 최종 {len(included)}건) ===")
for a in included:
    print(" -", a["press"], "|", a["title"], "| subject:", a["subject"])

print(f"\n=== excluded (relevance=low, {len(excluded_low)}건) ===")
for a in excluded_low:
    print(" -", a["press"], "|", a["title"], "| subject:", a["subject"])

targets = ["20260727010009445", "202607221048575840101993"]
print("\n=== 대상 기사 위치 ===")
for a in included:
    if any(t in a.get("url", "") for t in targets):
        print("INCLUDED:", a["press"], a["title"])
for a in excluded_low:
    if any(t in a.get("url", "") for t in targets):
        print("EXCLUDED_LOW_RELEVANCE:", a["press"], a["title"])
