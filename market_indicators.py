"""
시황 지표 수집 모듈

일간 지표 13개(fetch_market_indicators)와 월간 지표 1개(fetch_construction_cost_index)를
분리해서 제공한다. 일간 지표는 매 실행마다 새로 조회하고, 월간 지표는
generate_daily_archive.py에서 이번 달 값이 이미 있으면 재사용한다.

사전 준비:
    pip install finance-datareader pykrx requests

일간 지표 소스:
    - kospi, kosdaq, nasdaq, dow, sp500, hdec, smr, tiger_nuke, nlr, ura, fx
      → FinanceDataReader (로그인 불필요)
    - wti → Yahoo Finance 차트 JSON API 직접 호출
      (FinanceDataReader의 "WTI" 심볼은 원유 가격이 아닌 다른 데이터로 확인되어 사용하지 않음)
    - cidx1(KOSPI 건설업 지수, 티커 1018) → pykrx
      (data.krx.co.kr 회원 로그인 필요 — 환경변수 KRX_ID/KRX_PW)
      주의: 1019는 존재하지 않는 코드로, 네이버금융에서 조용히 코스피 지수로
      폴백되는 것이 확인됨. 반드시 1018을 사용할 것.

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
# FinanceDataReader 기반 지표 (kospi/kosdaq/nasdaq/dow/sp500/hdec/smr/tiger_nuke/nlr/ura/fx)
# ---------------------------------------------------------------------------

# key -> (FDR 심볼, 소수점 자리수)
_FDR_TICKERS = {
    "kospi": ("KS11", 2),
    "kosdaq": ("KQ11", 2),
    "nasdaq": ("IXIC", 2),
    "dow": ("DJI", 2),
    "sp500": ("US500", 2),
    "hdec": ("000720", 0),
    "smr": ("0092B0", 0),
    "tiger_nuke": ("0091P0", 0),
    "nlr": ("NLR", 2),
    "ura": ("URA", 2),
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
# WTI — Yahoo Finance 차트 JSON API 직접 호출
# (FDR의 "WTI" 심볼은 원유 가격이 아닌 것으로 확인되어 사용하지 않음)
# ---------------------------------------------------------------------------

def _fetch_wti() -> list:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/CL=F"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)

    meta = data["chart"]["result"][0]["meta"]
    price = meta["regularMarketPrice"]
    prev_close = meta["previousClose"]
    change_ratio = (price - prev_close) / prev_close
    return [_fmt_number(price, 2), _fmt_pct(change_ratio), _direction(change_ratio)]


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
# 일간 지표 통합
# ---------------------------------------------------------------------------

def fetch_market_indicators() -> dict:
    """일간 시황 지표 13개를 수집해 {key: [값, 등락률, 방향]} 형태로 반환한다.

    kospi, kosdaq, nasdaq, dow, sp500, hdec, cidx1, smr, tiger_nuke, nlr, ura, fx, wti
    """
    result = {}
    for key in _FDR_TICKERS:
        result[key] = _fetch_fdr_field(key)
    result["fx"] = _fetch_fx()
    result["wti"] = _fetch_wti()
    result["cidx1"] = _fetch_cidx1()
    return result


# ---------------------------------------------------------------------------
# cidx2 — 건설공사비지수 (KOSIS Open API, 월 1회 갱신)
# ---------------------------------------------------------------------------

_KOSIS_ORG_ID = "397"
_KOSIS_TBL_ID = "DT_39701_A003"


def fetch_construction_cost_index(api_key: str) -> dict:
    """건설공사비지수(월간, 2020=100)를 KOSIS Open API에서 조회한다.

    매일 호출하는 지표가 아니므로 fetch_market_indicators()에는 포함하지 않는다.
    반환 형태: {"value": "137.67", "base": "2020=100", "period": "2026-05"}
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

    # "건설" 종합지수(업종별 최상위 항목)만 필터링, 최신 발표월 선택
    construction_rows = [r for r in rows if r.get("C1_NM") == "건설"]
    if not construction_rows:
        raise RuntimeError("KOSIS 응답에서 '건설' 종합지수를 찾지 못했습니다")

    latest = max(construction_rows, key=lambda r: r["PRD_DE"])
    prd_de = latest["PRD_DE"]  # "202605"
    period = f"{prd_de[:4]}-{prd_de[4:]}"
    unit = latest.get("UNIT_NM", "").replace("＝", "=")  # "2020＝100" -> "2020=100"

    return {
        "value": latest["DT"],
        "base": unit,
        "period": period,
    }


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
