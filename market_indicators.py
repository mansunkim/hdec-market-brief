"""
시황 지표 수집 모듈

일간 지표 13개(fetch_market_indicators)와 월간 지표 1개(fetch_construction_cost_index)를
분리해서 제공한다. 일간 지표는 매 실행마다 새로 조회하고, 월간 지표는
generate_daily_archive.py에서 이번 달 값이 이미 있으면 재사용한다.

사전 준비:
    pip install finance-datareader pykrx requests

일간 지표 소스:
    - kospi, kosdaq, hdec, smr, tiger_nuke, fx → FinanceDataReader (KRX 소스, 로그인 불필요)
    - nasdaq, dow, sp500, nlr, ura, wti → Yahoo Finance 차트 JSON API 직접 호출
      (FDR이 이 필드들에 내부적으로 쓰는 Yahoo 경로는 GitHub Actions 같은 클라우드 IP에서
      막히거나 실패해 "nan"을 반환하는 것이 실제 운영 환경에서 확인됨. 직접 차트 API를
      호출하면 안정적으로 동작한다. WTI의 "WTI" FDR 심볼도 애초에 원유 가격이 아닌
      다른 데이터로 확인되어 사용하지 않았던 것과 같은 이유로 전부 통일함)
    - cidx1(KOSPI 건설업 지수, 티커 1018) → pykrx
      (data.krx.co.kr 회원 로그인 필요 — 환경변수 KRX_ID/KRX_PW)
      주의: 1019는 존재하지 않는 코드로, 네이버금융에서 조용히 코스피 지수로
      폴백되는 것이 확인됨. 반드시 1018을 사용할 것.
    - 국내 동종사(건설사 5곳) + 국내 원자력 관련주(2곳) → pykrx
      (data.krx.co.kr 회원 로그인 필요 — 환경변수 KRX_ID/KRX_PW)
    - 해외 원자력 관련주(FRMI/CCJ/CEG/OKLO) → Yahoo Finance 차트 JSON API 직접 호출
      (USD 가격 그대로 반환)

월간 지표 소스:
    - cidx2(건설공사비지수) → KOSIS Open API (orgId=397, tblId=DT_39701_A003)
      환경변수 KOSIS_API_KEY 필요. 통계표 내 "건설" 종합지수(C1_NM="건설") 최신월 값을 사용.
"""

import datetime
import json
import urllib.parse
import urllib.request

import FinanceDataReader as fdr

# ---------------------------------------------------------------------------
# 포맷 헬퍼
# ---------------------------------------------------------------------------

def _direction(change_ratio: float) -> str:
    if change_ratio > 0:
        return "rise"
    if change_ratio < 0:
        return "fall"
    return "flat"


def _fmt_pct(change_ratio: float) -> str:
    pct = change_ratio * 100
    if abs(pct) < 0.005:
        return "0.00%"
    return f"{pct:+.2f}%"


def _fmt_number(value: float, decimals: int) -> str:
    return f"{value:,.{decimals}f}"


# ---------------------------------------------------------------------------
# FinanceDataReader 기반 지표 (KRX 소스: kospi/kosdaq/hdec/smr/tiger_nuke)
# ---------------------------------------------------------------------------

# key -> (FDR 심볼, 소수점 자리수)
_FDR_TICKERS = {
    "kospi": ("KS11", 2),
    "kosdaq": ("KQ11", 2),
    "hdec": ("000720", 0),
    "smr": ("0092B0", 0),
    "tiger_nuke": ("0091P0", 0),
}


# 히스토리 조회 기간. 하루 지표 흐름 카드 호버 차트에 "최근 1개월(약 22거래일)"을
# 보여주려면 주말/휴일을 감안해 넉넉히 40일치를 받아온 뒤 최근 거래일만 자른다.
_HISTORY_LOOKBACK_DAYS = 40
_HISTORY_TRADING_DAYS = 22


def _history_from_points(points: list, decimals: int, trading_days: int = _HISTORY_TRADING_DAYS) -> list:
    tail = points[-trading_days:]
    return [[d, round(v, decimals)] for d, v in tail]


def _snapshot_from_points(points: list, decimals: int) -> list:
    latest_close = points[-1][1]
    prev_close = points[-2][1]
    change_ratio = (latest_close - prev_close) / prev_close
    return [_fmt_number(latest_close, decimals), _fmt_pct(change_ratio), _direction(change_ratio)]


def _fdr_close_points(symbol: str, key: str) -> list:
    start = (datetime.datetime.now() - datetime.timedelta(days=_HISTORY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    df = fdr.DataReader(symbol, start)
    if df.empty or len(df) < 2:
        raise RuntimeError(f"{key}({symbol}): FDR에서 충분한 데이터를 받지 못했습니다")
    return [(idx.strftime("%Y-%m-%d"), float(v)) for idx, v in df["Close"].items()]


def _fetch_fdr_full(key: str) -> tuple:
    """(스냅샷, 히스토리)를 한 번의 조회로 함께 만든다.

    'Change' 컬럼은 KRX 소스 티커에만 있고 Yahoo 소스 티커에는 없으므로,
    모든 필드에 대해 종가 기준으로 직접 등락률을 계산해 일관되게 처리한다.
    """
    symbol, decimals = _FDR_TICKERS[key]
    points = _fdr_close_points(symbol, key)
    return _snapshot_from_points(points, decimals), _history_from_points(points, decimals)


def _fetch_fx_full() -> tuple:
    points = _fdr_close_points("USD/KRW", "fx(USD/KRW)")
    latest_close = points[-1][1]
    prev_close = points[-2][1]
    diff = latest_close - prev_close
    # 환율은 등락률(%)이 아니라 등락폭(원)을 그대로 표기하는 관례를 따른다.
    diff_str = f"{diff:+.1f}" if diff != 0 else "0.0"
    snapshot = [_fmt_number(latest_close, 1), diff_str, _direction(diff)]
    history = _history_from_points(points, 1)
    return snapshot, history


# ---------------------------------------------------------------------------
# Yahoo Finance 차트 JSON API 직접 호출
# (nasdaq/dow/sp500/nlr/ura/wti — FDR이 이 필드들에 쓰는 Yahoo 경로가 GitHub Actions
# 같은 클라우드 IP에서 막혀 "nan"을 반환하는 것이 확인되어, 직접 API를 호출한다)
# ---------------------------------------------------------------------------

# key -> (Yahoo 티커, 소수점 자리수)
_YAHOO_TICKERS = {
    "nasdaq": ("^IXIC", 2),
    "dow": ("^DJI", 2),
    "sp500": ("^GSPC", 2),
    "nlr": ("NLR", 2),
    "ura": ("URA", 2),
    "wti": ("CL=F", 2),
    # 해외 원자력 관련주 (USD 가격 그대로)
    "frmi": ("FRMI", 2),
    "ccj": ("CCJ", 2),
    "ceg": ("CEG", 2),
    "oklo": ("OKLO", 2),
}


def _yahoo_close_points(ticker: str, key: str) -> list:
    # range=1mo&interval=1d로 받으면 meta.previousClose가 응답에서 빠지는 경우가 있어
    # (검증됨), 스냅샷/히스토리 모두 종가 배열의 마지막 값들에서 직접 계산한다.
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
        f"?range=1mo&interval=1d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)

    result = data["chart"]["result"][0]
    timestamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    points = [
        (datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"), float(c))
        for t, c in zip(timestamps, closes)
        if c is not None
    ]
    if len(points) < 2:
        raise RuntimeError(f"{key}({ticker}): Yahoo에서 충분한 데이터를 받지 못했습니다")
    return points


def _fetch_yahoo_full(key: str) -> tuple:
    ticker, decimals = _YAHOO_TICKERS[key]
    points = _yahoo_close_points(ticker, key)
    return _snapshot_from_points(points, decimals), _history_from_points(points, decimals)


# ---------------------------------------------------------------------------
# cidx1 — KOSPI 건설업 지수 (pykrx, 티커 1018, KRX 로그인 필요)
# ---------------------------------------------------------------------------

def _fetch_cidx1_full() -> tuple:
    from pykrx import stock

    end = datetime.datetime.now().strftime("%Y%m%d")
    start = (datetime.datetime.now() - datetime.timedelta(days=_HISTORY_LOOKBACK_DAYS)).strftime("%Y%m%d")
    df = stock.get_index_ohlcv(start, end, "1018")
    if df is None or df.empty or len(df) < 2:
        raise RuntimeError("cidx1(1018): pykrx에서 충분한 데이터를 받지 못했습니다 (KRX_ID/KRX_PW 확인 필요)")

    points = [(idx.strftime("%Y-%m-%d"), float(v)) for idx, v in df["종가"].items()]
    return _snapshot_from_points(points, 2), _history_from_points(points, 2)


# ---------------------------------------------------------------------------
# 국내 동종사 + 국내 원자력 관련주 (pykrx, 개별 종목)
# ---------------------------------------------------------------------------

# key -> (종목코드, 소수점 자리수) — 종가는 원(KRW) 단위라 정수로 표기
_PYKRX_STOCK_TICKERS = {
    # 건설사 동종사
    "daewoo_enc": ("047040", 0),    # 대우건설
    "gs_enc": ("006360", 0),        # GS건설
    "dl_enc": ("375500", 0),        # DL이앤씨
    "samsung_ena": ("028050", 0),   # 삼성E&A
    "hdc_idc": ("294870", 0),       # HDC현대산업개발
    # 국내 원자력 관련주
    "doosan_enerbility": ("034020", 0),  # 두산에너빌리티
    "kepco_eng": ("052690", 0),          # 한전기술
}


def _fetch_pykrx_stock_full(key: str) -> tuple:
    from pykrx import stock

    code, decimals = _PYKRX_STOCK_TICKERS[key]
    end = datetime.datetime.now().strftime("%Y%m%d")
    start = (datetime.datetime.now() - datetime.timedelta(days=_HISTORY_LOOKBACK_DAYS)).strftime("%Y%m%d")
    df = stock.get_market_ohlcv(start, end, code)
    if df is None or df.empty:
        raise RuntimeError(f"{key}({code}): pykrx에서 데이터를 받지 못했습니다 (KRX_ID/KRX_PW 확인 필요)")

    latest = df.iloc[-1]
    close = float(latest["종가"])
    # get_market_ohlcv는 등락률(%) 컬럼을 직접 제공하므로 스냅샷은 전일 종가로 재계산하지
    # 않고 그대로 쓴다. 히스토리는 종가 시계열을 그대로 뽑는다.
    change_ratio = float(latest["등락률"]) / 100
    snapshot = [_fmt_number(close, decimals), _fmt_pct(change_ratio), _direction(change_ratio)]
    points = [(idx.strftime("%Y-%m-%d"), float(v)) for idx, v in df["종가"].items()]
    history = _history_from_points(points, decimals)
    return snapshot, history


# ---------------------------------------------------------------------------
# 일간 지표 통합
# ---------------------------------------------------------------------------

def fetch_market_indicators_full() -> tuple:
    """일간 시황 지표의 스냅샷과 히스토리를 함께 수집한다.

    (snapshot, history) 튜플을 반환한다.
    - snapshot: {key: [값, 등락률, 방향]} — 기존 fetch_market_indicators()와 동일한 모양
    - history: {key: [[날짜, 값], ...]} — 최근 약 1개월(22거래일)치 시계열
      (지표 카드 호버 시 보여줄 차트용. cidx2는 별도로 월간 캐시에서 처리한다)

    스냅샷/히스토리를 따로 조회하면 소스별 API를 두 번씩 호출하게 되므로,
    각 필드마다 한 번만 조회해서 두 결과를 함께 뽑아낸다.
    """
    snapshot, history = {}, {}
    for key in _FDR_TICKERS:
        snapshot[key], history[key] = _fetch_fdr_full(key)
    for key in _YAHOO_TICKERS:
        snapshot[key], history[key] = _fetch_yahoo_full(key)
    for key in _PYKRX_STOCK_TICKERS:
        snapshot[key], history[key] = _fetch_pykrx_stock_full(key)
    snapshot["fx"], history["fx"] = _fetch_fx_full()
    snapshot["cidx1"], history["cidx1"] = _fetch_cidx1_full()
    return snapshot, history


def fetch_market_indicators() -> dict:
    """일간 시황 지표를 수집해 {key: [값, 등락률, 방향]} 형태로 반환한다.

    kospi, kosdaq, nasdaq, dow, sp500, hdec, cidx1, smr, tiger_nuke, nlr, ura, fx, wti,
    daewoo_enc, gs_enc, dl_enc, samsung_ena, hdc_idc, doosan_enerbility, kepco_eng,
    frmi, ccj, ceg, oklo

    (하위 호환용 — 히스토리도 필요하면 fetch_market_indicators_full()을 직접 쓸 것.
    이 함수는 내부적으로 동일하게 조회하고 히스토리를 버릴 뿐이라, 스냅샷과 히스토리가
    둘 다 필요한 경우 이 함수 대신 fetch_market_indicators_full()을 써야 API를
    중복 호출하지 않는다.)
    """
    snapshot, _ = fetch_market_indicators_full()
    return snapshot


# ---------------------------------------------------------------------------
# cidx2 — 건설공사비지수 (KOSIS Open API, 월 1회 갱신)
# ---------------------------------------------------------------------------

_KOSIS_ORG_ID = "397"
_KOSIS_TBL_ID = "DT_39701_A003"


def fetch_construction_cost_index_full(api_key: str, months: int = 13) -> tuple:
    """건설공사비지수(월간, 2020=100)를 KOSIS Open API에서 조회한다.

    (snapshot, history) 튜플을 반환한다. months=13이면 전월대비 계산에 필요한 여유분을
    포함해 최근 13개월치를 받아오고, 히스토리는 그중 최근 12개월(1년치)을 오름차순으로 담는다.
    다른 일간 지표와 동일하게 스냅샷은 [값, 등락률, 방향] 형태로 "시장지표" 그리드에
    카드로 그대로 렌더링할 수 있게 하되, 월간 지표라는 성격을 드러내기 위해
    등락률은 전일대비가 아니라 전월말대비로 계산하고, 값 옆에 기준월을 주석으로 덧붙인다.
    """
    params = {
        "method": "getList",
        "apiKey": api_key,
        "itmId": "ALL",
        "objL1": "ALL",
        "format": "json",
        "jsonVD": "Y",
        "prdSe": "M",
        "newEstPrdCnt": str(months),
        "orgId": _KOSIS_ORG_ID,
        "tblId": _KOSIS_TBL_ID,
    }
    url = "https://kosis.kr/openapi/Param/statisticsParameterData.do?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        rows = json.load(resp)

    if isinstance(rows, dict) and "err" in rows:
        raise RuntimeError(f"KOSIS API 오류: {rows.get('errMsg', rows)}")

    # "건설" 종합지수(업종별 최상위 항목)만 필터링, 최신 발표월 순으로 정렬
    construction_rows = sorted(
        (r for r in rows if r.get("C1_NM") == "건설"),
        key=lambda r: r["PRD_DE"],
        reverse=True,
    )
    if not construction_rows:
        raise RuntimeError("KOSIS 응답에서 '건설' 종합지수를 찾지 못했습니다")

    latest = construction_rows[0]
    value = float(latest["DT"])
    prd_de = latest["PRD_DE"]  # "202605"
    period_label = f"{prd_de[:4]}년 {int(prd_de[4:])}월"

    if len(construction_rows) >= 2:
        prev_value = float(construction_rows[1]["DT"])
        change_ratio = (value - prev_value) / prev_value
        chg_str = f"{_fmt_pct(change_ratio)}(전월말대비)"
        direction = _direction(change_ratio)
    else:
        chg_str = "-(전월말대비)"
        direction = "flat"

    value_html = (
        f"{value:,.2f}"
        f'<span style="font-size:9.5px;font-weight:500;color:var(--ink-faint);margin-left:4px;">'
        f"※{period_label}기준</span>"
    )
    snapshot = [value_html, chg_str, direction]

    # 히스토리는 최근 12개월치를 오름차순("YYYY-MM" 라벨)으로 담는다.
    history_rows = sorted(construction_rows, key=lambda r: r["PRD_DE"])[-12:]
    history = [
        [f"{r['PRD_DE'][:4]}-{r['PRD_DE'][4:]}", round(float(r["DT"]), 2)]
        for r in history_rows
    ]
    return snapshot, history


def fetch_construction_cost_index(api_key: str) -> list:
    """(하위 호환용) 스냅샷만 필요할 때 사용. fetch_construction_cost_index_full() 참고."""
    snapshot, _ = fetch_construction_cost_index_full(api_key, months=3)
    return snapshot


# ---------------------------------------------------------------------------
# 단독 실행 테스트
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    print(json.dumps(fetch_market_indicators(), ensure_ascii=False, indent=2))

    kosis_key = os.environ.get("KOSIS_API_KEY")
    if kosis_key:
        print(json.dumps(fetch_construction_cost_index(kosis_key), ensure_ascii=False, indent=2))
    else:
        print("KOSIS_API_KEY not set — skipping fetch_construction_cost_index() test")
