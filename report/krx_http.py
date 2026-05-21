"""
KRX 정보데이터시스템 직접 HTTP 호출 클라이언트.

pykrx 의존성 없이 동작 (pykrx 소스의 bld/파라미터 값을 직접 참고해 구현).
KRX(data.krx.co.kr)가 내부적으로 쓰는 JSON 엔드포인트를 호출.

엔드포인트: POST http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd
form-urlencoded: bld(리포트ID) + 파라미터 → JSON 응답

리포트 ID (bld) — pykrx 소스에서 확인:
- MDCSTAT02401 : [12010] 투자자별 순매수상위종목
- MDCSTAT02201 : [12009] 투자자별 거래실적 전체시장 기간합계
- MDCSTAT01501 : [12001] 전종목 시세 (시가총액 포함)
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

URL = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
PORTAL_INIT_URL = "http://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd"

INVESTOR_CODES = {
    "금융투자": "1000", "보험": "2000", "투신": "3000", "사모": "3100",
    "은행": "4000", "기타금융": "5000", "연기금": "6000",
    "기관합계": "7050", "기타법인": "7100",
    "개인": "8000", "외국인": "9000", "기타외국인": "9001", "전체": "9999",
}

MARKET_CODES = {
    "KOSPI": "STK", "KOSDAQ": "KSQ", "KONEX": "KNX", "ALL": "ALL",
}

# KRX 데이터 포털은 세션 쿠키(JSESSIONID + __smVisitorID)를 요구함.
# 쿠키 없으면 status 400 + body "LOGOUT" 반환.
# requests.Session()으로 쿠키 자동 추적 + pre-flight으로 쿠키 발급.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "http://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    """세션 lazy 초기화 + pre-flight으로 쿠키 받기."""
    global _session
    if _session is not None:
        return _session
    s = requests.Session()
    s.headers.update(_HEADERS)
    # pre-flight: 데이터 포털 메인 방문 → JSESSIONID + __smVisitorID 발급
    try:
        s.get(PORTAL_INIT_URL, timeout=10)
        log.info("KRX session initialized. cookies=%s", list(s.cookies.keys()))
    except Exception:
        log.exception("KRX pre-flight failed (계속 진행)")
    _session = s
    return s


def reset_session():
    """세션 만료/실패 시 강제 재발급."""
    global _session
    _session = None


def _post(form: dict, timeout: int = 15, retry: bool = True) -> dict:
    s = _get_session()
    resp = s.post(URL, data=form, timeout=timeout)
    # LOGOUT 응답이면 세션 재발급 후 1회 재시도
    if resp.status_code != 200 or resp.text.strip() == "LOGOUT":
        log.warning("KRX %s body[:200]=%r — 세션 만료 추정, 재발급+재시도",
                    resp.status_code, resp.text[:200])
        if retry:
            reset_session()
            return _post(form, timeout=timeout, retry=False)
        log.error("KRX %s body[:500]=%r", resp.status_code, resp.text[:500])
        resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        log.error("KRX JSON decode fail. body[:500]=%r", resp.text[:500])
        raise


def _to_int(v) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).replace(",", "").strip()
    if not s or s == "-":
        return 0
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


# --------------------------------------------------------------------------
# 공개 API
# --------------------------------------------------------------------------

def fetch_investor_trading_by_stock(
    trade_date: str,
    market: str = "KOSPI",
    investor: str = "연기금",
) -> list:
    """
    [12010] 투자자별 순매수상위종목 — 특정 일자에 특정 투자자가 거래한 전 종목.
    실은 "상위종목" 이지만 거래 발생한 모든 종목이 반환됨 (정렬은 클라이언트가).

    return: [{stock_code, stock_name, buy_amount, sell_amount, net_amount, buy_qty, sell_qty, net_qty}]
    """
    mkt = MARKET_CODES.get(market.upper(), market)
    inv = INVESTOR_CODES.get(investor, investor)

    form = {
        "bld": "dbms/MDC/STAT/standard/MDCSTAT02401",
        "strtDd": trade_date,
        "endDd": trade_date,
        "mktId": mkt,
        "invstTpCd": inv,
    }
    data = _post(form)
    rows = data.get("output", [])
    return [_normalize_stock_row(r) for r in rows]


def fetch_top_net_purchases(
    trade_date: str,
    market: str = "KOSPI",
    investor: str = "연기금",
    top_n: int = 50,
    direction: str = "buy",   # "buy" = 순매수 상위, "sell" = 순매도 상위
) -> list:
    """순매수/순매도 Top N — fetch_investor_trading_by_stock의 결과를 정렬/슬라이스."""
    rows = fetch_investor_trading_by_stock(trade_date, market, investor)
    rows = [r for r in rows if r.get("net_amount", 0) != 0]
    rev = (direction == "buy")
    rows.sort(key=lambda r: r["net_amount"], reverse=rev)
    return rows[:top_n] if top_n else rows


def fetch_market_summary(trade_date: str, market: str = "KOSPI") -> dict:
    """
    [12009] 투자자별 거래실적 전체시장 기간합계.
    return: {investor_name: {buy, sell, net}}
    """
    mkt = MARKET_CODES.get(market.upper(), market)
    form = {
        "bld": "dbms/MDC/STAT/standard/MDCSTAT02201",
        "strtDd": trade_date,
        "endDd": trade_date,
        "mktId": mkt,
        "etf": "",
        "etn": "",
        "elw": "",
    }
    data = _post(form)
    rows = data.get("output", [])
    summary = {}
    for r in rows:
        name = (r.get("INVST_TP_NM") or "").strip()
        if not name:
            continue
        summary[name] = {
            "buy_qty":  _to_int(r.get("BID_TRDVOL")),
            "sell_qty": _to_int(r.get("ASK_TRDVOL")),
            "net_qty":  _to_int(r.get("NETBID_TRDVOL")),
            "buy":      _to_int(r.get("BID_TRDVAL")),
            "sell":     _to_int(r.get("ASK_TRDVAL")),
            "net":      _to_int(r.get("NETBID_TRDVAL")),
        }
    return summary


def fetch_market_cap(trade_date: str, market: str = "KOSPI") -> dict:
    """[12001] 전종목 시세 — code → 시가총액 (원)."""
    mkt = MARKET_CODES.get(market.upper(), market)
    form = {
        "bld": "dbms/MDC/STAT/standard/MDCSTAT01501",
        "trdDd": trade_date,
        "mktId": mkt,
    }
    data = _post(form)
    # 이 엔드포인트는 응답 키가 OutBlock_1
    rows = data.get("OutBlock_1") or data.get("output") or []
    cap_map = {}
    for r in rows:
        code = (r.get("ISU_SRT_CD") or "").strip()
        if not code:
            continue
        cap_map[code] = _to_int(r.get("MKTCAP"))
    return cap_map


# --------------------------------------------------------------------------
# 내부 헬퍼
# --------------------------------------------------------------------------

def _normalize_stock_row(r: dict) -> dict:
    """KRX 행 → 표준 dict.

    KRX 응답 컬럼 (MDCSTAT02401):
      ISU_SRT_CD, ISU_NM, ASK_TRDVOL, BID_TRDVOL, NETBID_TRDVOL,
      ASK_TRDVAL, BID_TRDVAL, NETBID_TRDVAL

    주의: KRX 표기 기준
      - ASK_* = 매도 (호가창의 '매도호가' = ASK)
      - BID_* = 매수 (호가창의 '매수호가' = BID)
      - NETBID_TRDVAL = 순매수 거래대금 (BID - ASK)
    """
    code = (r.get("ISU_SRT_CD") or "").strip()
    name = (r.get("ISU_NM") or r.get("ISU_ABBRV") or "").strip()
    return {
        "stock_code": code,
        "stock_name": name,
        # 거래대금 (원)
        "buy_amount":  _to_int(r.get("BID_TRDVAL")),
        "sell_amount": _to_int(r.get("ASK_TRDVAL")),
        "net_amount":  _to_int(r.get("NETBID_TRDVAL")),
        # 거래량 (주)
        "buy_qty":  _to_int(r.get("BID_TRDVOL")),
        "sell_qty": _to_int(r.get("ASK_TRDVOL")),
        "net_qty":  _to_int(r.get("NETBID_TRDVOL")),
        "_raw": r,
    }


def latest_business_date(today: Optional[dt.date] = None) -> dt.date:
    """오늘이 평일이면 오늘, 주말이면 직전 금요일."""
    d = today or dt.date.today()
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d
