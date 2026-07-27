"""일회성 디버그 스크립트: 화이트리스트 게이트->신호 전환이 실제로 동작하는지
확인한다. 아시아투데이/더벨을 tier2에서 일부러 빼고 crawl_category_debug를
돌려서, known_press 판단으로 정상 통과 + verify_needed=true("확인필요")가
붙는지 확인한다. 실제 config.json/CATEGORY_CONFIGS는 건드리지 않는다.
확인 후 삭제한다."""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from stock_news_crawler import crawl_category_debug, CATEGORY_CONFIGS

cfg = dict(next(c for c in CATEGORY_CONFIGS if c["category_name"] == "현대건설"))
# 일부러 아시아투데이/더벨을 제외한 tier2로 테스트
cfg["tier2_whitelist"] = [t for t in cfg["tier2_whitelist"] if t not in ("아시아투데이", "더벨")]
print("테스트용 tier2_whitelist:", cfg["tier2_whitelist"])

included, excluded_low = crawl_category_debug(**cfg)

print(f"\n=== included ({len(included)}건) ===")
for a in included:
    print(" -", a["press"], "| tier:", a["tier"], "| verify_needed:", a["verify_needed"], "|", a["title"])

print(f"\n=== excluded relevance=low ({len(excluded_low)}건) ===")
for a in excluded_low:
    print(" -", a["press"], "|", a["title"])

targets = ["20260727010009445", "202607221048575840101993"]
print("\n=== 대상 기사 상태 ===")
for a in included:
    if any(t in a.get("url", "") for t in targets):
        print("INCLUDED:", a["press"], "| tier:", a["tier"], "| verify_needed:", a["verify_needed"])
for a in excluded_low:
    if any(t in a.get("url", "") for t in targets):
        print("EXCLUDED (relevance=low):", a["press"])
