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


def _fetch_fdr_field(key: str) -> list:
    symbol, decimals = _FDR_TICKERS[key]
    start = (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")
    df = fdr.DataReader(symbol, start)
    if df.empty or len(df) < 2:
        raise RuntimeError(f"{key}({symbol}): FDR에서 충분한 데이터를 받지 못했습니다")

    # 'Change' 컬럼은 KRX 소스 티커에만 있고 Yahoo 소스 티커(나스닥/다우/S&P500/NLR/URA)에는
    # 없으므로, 모든 필드에 대해 종가 기준으로 직접 등락률을 계산해 일관되게 처리한다.
    latest_close = float(df.iloc[-1]["Close"])
    prev_close = float(df.iloc[-2]["Close"])
    change_ratio = (latest_close - prev_close) / prev_close
    value_str = _fmt_number(latest_close, decimals)
    return [value_str, _fmt_pct(change_ratio), _direction(change_ratio)]


def _fetch_fx() -> list:
    df = fdr.DataReader("USD/KRW", (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d"))
    if df.empty or len(df) < 2:
        raise RuntimeError("fx(USD/KRW): FDR에서 충분한 데이터를 받지 못했습니다")

    latest_close = float(df.iloc[-1]["Close"])
    prev_close = float(df.iloc[-2]["Close"])
    diff = latest_close - prev_close
    # 환율은 등락률(%)이 아니라 등락폭(원)을 그대로 표기하는 관례를 따른다.
    diff_str = f"{diff:+.1f}" if diff != 0 else "0.0"
    return [_fmt_number(latest_close, 1), diff_str, _direction(diff)]


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


def _fetch_yahoo_field(key: str) -> list:
    ticker, decimals = _YAHOO_TICKERS[key]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)

    meta = data["chart"]["result"][0]["meta"]
    price = meta["regularMarketPrice"]
    prev_close = meta["previousClose"]
    change_ratio = (price - prev_close) / prev_close
    return [_fmt_number(price, decimals), _fmt_pct(change_ratio), _direction(change_ratio)]


# ---------------------------------------------------------------------------
# cidx1 — KOSPI 건설업 지수 (pykrx, 티커 1018, KRX 로그인 필요)
# ---------------------------------------------------------------------------

def _fetch_cidx1() -> list:
    from pykrx import stock

    end = datetime.datetime.now().strftime("%Y%m%d")
    start = (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y%m%d")
    df = stock.get_index_ohlcv(start, end, "1018")
    if df is None or df.empty or len(df) < 2:
        raise RuntimeError("cidx1(1018): pykrx에서 충분한 데이터를 받지 못했습니다 (KRX_ID/KRX_PW 확인 필요)")

    latest_close = float(df.iloc[-1]["종가"])
    prev_close = float(df.iloc[-2]["종가"])
    change_ratio = (latest_close - prev_close) / prev_close
    return [_fmt_number(latest_close, 2), _fmt_pct(change_ratio), _direction(change_ratio)]


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


def _fetch_pykrx_stock_field(key: str) -> list:
    from pykrx import stock

    code, decimals = _PYKRX_STOCK_TICKERS[key]
    end = datetime.datetime.now().strftime("%Y%m%d")
    start = (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y%m%d")
    df = stock.get_market_ohlcv(start, end, code)
    if df is None or df.empty:
        raise RuntimeError(f"{key}({code}): pykrx에서 데이터를 받지 못했습니다 (KRX_ID/KRX_PW 확인 필요)")

    latest = df.iloc[-1]
    close = float(latest["종가"])
    # get_market_ohlcv는 등락률(%) 컬럼을 직접 제공하므로 전일 종가로 재계산하지 않는다.
    change_ratio = float(latest["등락률"]) / 100
    return [_fmt_number(close, decimals), _fmt_pct(change_ratio), _direction(change_ratio)]


# ---------------------------------------------------------------------------
# 일간 지표 통합
# ---------------------------------------------------------------------------

def fetch_market_indicators() -> dict:
    """일간 시황 지표를 수집해 {key: [값, 등락률, 방향]} 형태로 반환한다.

    kospi, kosdaq, nasdaq, dow, sp500, hdec, cidx1, smr, tiger_nuke, nlr, ura, fx, wti,
    daewoo_enc, gs_enc, dl_enc, samsung_ena, hdc_idc, doosan_enerbility, kepco_eng,
    frmi, ccj, ceg, oklo
    """
    result = {}
    for key in _FDR_TICKERS:
        result[key] = _fetch_fdr_field(key)
    for key in _YAHOO_TICKERS:
        result[key] = _fetch_yahoo_field(key)
    for key in _PYKRX_STOCK_TICKERS:
        result[key] = _fetch_pykrx_stock_field(key)
    result["fx"] = _fetch_fx()
    result["cidx1"] = _fetch_cidx1()
    return result


# ---------------------------------------------------------------------------
# cidx2 — 건설공사비지수 (KOSIS Open API, 월 1회 갱신)
# ---------------------------------------------------------------------------

_KOSIS_ORG_ID = "397"
_KOSIS_TBL_ID = "DT_39701_A003"


def fetch_construction_cost_index(api_key: str) -> list:
    """건설공사비지수(월간, 2020=100)를 KOSIS Open API에서 조회한다.

    매일 호출하는 지표가 아니므로 fetch_market_indicators()에는 포함하지 않는다
    (generate_daily_archive.py가 월 1회만 새로 조회하고 캐시를 재사용한다).
    다른 일간 지표와 동일하게 [값, 등락률, 방향] 형태로 반환해 "시장지표" 그리드에
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
        "newEstPrdCnt": "3",
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
    return [value_html, chg_str, direction]


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
