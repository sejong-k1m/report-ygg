"""
외부 데이터 소스 fetcher — KRX 직접 접근이 anti-bot 으로 막힌 우회 경로.

소스:
1. todayygg.com  — 연기금 매매 일일 리포트. JSON/CSV 공개 (의도된 다운로드).
2. judal.co.kr   — 종목별 가치지표 (PBR/PER/52주변동률 등). HTML 스크래핑.

이용 약관 메모:
- todayygg: "투자권유 아님 + KRX 공개 데이터 가공물" 명시. JSON/CSV 다운로드는 명시적 제공.
- judal: 공개 HTML. 일일 1회 가벼운 스크래핑.

회사 PC에서 todayygg.com / judal.co.kr 두 URL이 안 뚫리면 이 모듈도 동작 안 함.
그 경우 CSV 수동 다운로드 워크플로로 폴백.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import requests

log = logging.getLogger(__name__)

TODAYYGG_JSON_URL = "https://todayygg.com/summary_latest.json"
TODAYYGG_CSV_URL = "https://todayygg.com/analysis_latest.csv"

JUDAL_BUY_URL = "https://www.judal.co.kr/?view=stockList&type=fundBuy"
JUDAL_SELL_URL = "https://www.judal.co.kr/?view=stockList&type=fundSell"

# Toss Securities 비공식 API — stock-prices/details (시총/현재가/전일종가)
TOSS_STOCK_PRICES_URL = "https://wts-info-api.tossinvest.com/api/v3/stock-prices/details"
TOSS_TRADING_TREND_URL = "https://wts-info-api.tossinvest.com/api/v1/stock-infos/trade/trend/trading-trend"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
}


# ==========================================================================
# todayygg
# ==========================================================================

def fetch_todayygg_summary() -> Optional[dict]:
    """todayygg.com 의 summary_latest.json 다운로드."""
    try:
        r = requests.get(TODAYYGG_JSON_URL, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("todayygg JSON fetch 실패: %s", e)
        return None


def fetch_todayygg_csv() -> Optional[str]:
    """todayygg.com 의 analysis_latest.csv 다운로드 (텍스트 반환)."""
    try:
        r = requests.get(TODAYYGG_CSV_URL, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.warning("todayygg CSV fetch 실패: %s", e)
        return None


def _to_int(v) -> int:
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).replace(",", "").strip()
    try:
        return int(float(s))
    except Exception:
        return 0


def _to_float(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    try:
        return float(s)
    except Exception:
        return 0.0


def todayygg_to_standard_rows(payload: dict) -> tuple:
    """
    todayygg JSON → 표준 row 포맷.
    return: (trade_date, [rows])  rows 는 list of dict (generate.py 가 기대하는 키들)
    """
    if not payload:
        return None, []
    trade_date = payload.get("trade_date", "")
    rows_by_code = {}

    # buy/sell/market_cap top 종목들 → dedup
    for src_key in ("buy_top30", "sell_top30", "market_cap_buy_top30", "market_cap_sell_top30"):
        for r in payload.get(src_key) or []:
            code = (r.get("symbol") or "").strip()
            if not code:
                continue
            if code in rows_by_code:
                continue
            rows_by_code[code] = {
                "stock_code": code,
                "stock_name": r.get("name", ""),
                "market": r.get("market", ""),
                "buy_amount":  _to_int(r.get("buy_amount")),
                "sell_amount": _to_int(r.get("sell_amount")),
                "net_amount":  _to_int(r.get("net_buy_amount")),
                "buy_qty":  _to_int(r.get("buy_quantity")),
                "sell_qty": _to_int(r.get("sell_quantity")),
                "net_qty":  _to_int(r.get("net_buy_quantity")),
                "market_cap": _to_int(r.get("market_cap")),
                "close_price": _to_int(r.get("close_price")),
                "today_buy_avg":   _to_float(r.get("today_buy_average_price")),
                "period_buy_avg":  _to_float(r.get("period_buy_average_price")),
                "cumulative_net_amount": _to_int(r.get("cumulative_net_buy_amount")),
                "consecutive_sell_days": _to_int(r.get("consecutive_sell_days")),
                "sector":  r.get("sector", ""),
                "industry": r.get("industry", ""),
                "in_active_period": bool(r.get("in_active_period") or False),
                "source": "todayygg",
            }
    rows = list(rows_by_code.values())
    log.info("todayygg → %d 종목 (trade_date=%s)", len(rows), trade_date)
    return trade_date, rows


# ==========================================================================
# Toss stock-prices/details — 시총/현재가/등락률 보강용
# ==========================================================================

def fetch_toss_trading_trend(codes: list) -> dict:
    """
    토스 trading-trend — 종목별 일별 투자자별 매매 (장중 분 단위 갱신).
    종목당 1 호출 (size=1로 오늘 데이터만).

    return: {stock_code: {
        base_date, updated_at,
        pension_buy_qty, pension_sell_qty, pension_net_qty,
        foreigner_buy_qty, foreigner_sell_qty, foreigner_net_qty,
        institution_buy_qty, institution_sell_qty, institution_net_qty,
        close, is_today: bool
    }}
    """
    if not codes:
        return {}
    import datetime as dt
    today = dt.date.today().strftime("%Y-%m-%d")
    result = {}

    for code in codes:
        if not code:
            continue
        product_code = code if code.startswith("A") else f"A{code}"
        try:
            r = requests.get(
                TOSS_TRADING_TREND_URL,
                params={"productCode": product_code, "size": 1},
                headers=_HEADERS,
                timeout=10,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            log.warning("toss trading-trend %s failed: %s", code, e)
            continue

        body = (payload.get("result") or {}).get("body") or []
        if not body:
            continue
        latest = body[0]
        base_date = latest.get("baseDate", "")
        is_today = (base_date == today)

        # 연기금: 매수량 + 순매수 → 매도량 도출
        pension_buy = int(latest.get("pensionFundBuyVolume") or 0)
        pension_net = int(latest.get("netPensionFundBuyVolume") or 0)
        pension_sell = pension_buy - pension_net

        # 외국인
        foreigner_buy = int(latest.get("foreignerBuyVolume") or 0)
        foreigner_sell = int(latest.get("foreignerSellVolume") or 0)
        foreigner_net = int(latest.get("netForeignerBuyVolume") or 0)

        # 기관 합계
        institution_buy = int(latest.get("institutionBuyVolume") or 0)
        institution_sell = int(latest.get("institutionSellVolume") or 0)
        institution_net = int(latest.get("netInstitutionBuyVolume") or 0)

        result[code.lstrip("A")] = {
            "base_date": base_date,
            "updated_at": latest.get("updatedAt", ""),
            "is_today": is_today,
            "pension_buy_qty": pension_buy,
            "pension_sell_qty": pension_sell,
            "pension_net_qty": pension_net,
            "foreigner_buy_qty": foreigner_buy,
            "foreigner_sell_qty": foreigner_sell,
            "foreigner_net_qty": foreigner_net,
            "institution_buy_qty": institution_buy,
            "institution_sell_qty": institution_sell,
            "institution_net_qty": institution_net,
            "close": int(latest.get("close") or 0),
        }
    log.info("toss trading-trend → %d 종목 (today=%d개)",
             len(result), sum(1 for v in result.values() if v["is_today"]))
    return result


def fetch_toss_stock_prices(codes: list, chunk_size: int = 50) -> dict:
    """
    토스 stock-prices/details 호출. 종목코드(6자리, 'A' 없이) 리스트 → 'A' 붙여 호출.
    여러 종목 한 번에 가능 (콤마 구분). 한도 회피 위해 chunk로 쪼개서.

    return: {stock_code(6자리): {close, base, market_cap, change_rate, volume, value, ...}}
    """
    if not codes:
        return {}
    result = {}
    # 'A' prefix 정규화 + chunking
    a_codes = [(c if c.startswith("A") else f"A{c}") for c in codes if c]
    for i in range(0, len(a_codes), chunk_size):
        chunk = a_codes[i:i + chunk_size]
        try:
            r = requests.get(
                TOSS_STOCK_PRICES_URL,
                params={"productCodes": ",".join(chunk)},
                headers=_HEADERS,
                timeout=15,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            log.warning("toss stock-prices fetch failed chunk %d: %s", i, e)
            continue

        for item in payload.get("result") or []:
            code = (item.get("code") or "").lstrip("A")
            if not code:
                continue
            close = _to_int(item.get("close"))
            base = _to_int(item.get("base"))
            change_rate = ((close - base) / base * 100) if base > 0 else 0.0
            result[code] = {
                "close": close,
                "base": base,
                "market_cap": _to_int(item.get("marketCap")),
                "volume": _to_int(item.get("volume")),
                "value": _to_int(item.get("value")),
                "change_rate": round(change_rate, 2),
                "change_type": item.get("changeType", ""),
                "high52w": _to_int(item.get("high52w")),
                "low52w": _to_int(item.get("low52w")),
                "trading_strength": _to_float(item.get("tradingStrength")),
            }
    log.info("toss stock-prices → %d 종목 가격/시총 수집", len(result))
    return result


# ==========================================================================
# judal
# ==========================================================================

def _parse_amount_eok(text: str) -> int:
    """'388억' / '1,234억' → int(원). '5,210만' → int(원)."""
    if not text:
        return 0
    t = text.replace(",", "").strip()
    m = re.match(r"(-?[\d.]+)\s*(억원|억|만원|만)?$", t)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2) or ""
    if "억" in unit:
        return int(val * 100_000_000)
    if "만" in unit:
        return int(val * 10_000)
    return int(val)


def fetch_judal(direction: str = "buy") -> list:
    """
    주달 연기금 매수/매도 페이지 스크래핑.
    direction: 'buy' (fundBuy) | 'sell' (fundSell)

    return: [{stock_code(없을 수 있음), stock_name, amount, current_price, change_pct_52w,
              pbr, per, eps, market_cap, ...}]
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("beautifulsoup4 미설치 → judal 스크래핑 skip")
        return []

    url = JUDAL_BUY_URL if direction == "buy" else JUDAL_SELL_URL
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log.warning("judal %s fetch 실패: %s", direction, e)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    # 데이터 테이블 찾기 (페이지에 여러 표가 있을 수 있으니 가장 큰 표를 우선)
    candidates = soup.find_all("table")
    target = None
    max_rows = 0
    for t in candidates:
        n = len(t.find_all("tr"))
        if n > max_rows:
            target = t
            max_rows = n
    if not target:
        log.warning("judal %s: table not found", direction)
        return []

    # 헤더 파싱
    header_cells = [c.get_text(strip=True) for c in target.find_all("th")]
    log.info("judal %s headers: %s", direction, header_cells)

    # 컬럼 인덱스 매핑 (퍼지)
    def _find_col(*candidates):
        for cand in candidates:
            for i, h in enumerate(header_cells):
                if cand in h:
                    return i
        return -1

    idx_name   = _find_col("종목명")
    idx_amount = _find_col("매수금액" if direction == "buy" else "매도금액", "금액")
    idx_price  = _find_col("현재가")
    idx_52w_var = _find_col("52주 변동률", "52주변동률")
    idx_3y_var  = _find_col("3년 변동률", "3년변동률")
    idx_pbr    = _find_col("PBR")
    idx_per    = _find_col("PER")
    idx_eps    = _find_col("EPS")
    idx_mcap   = _find_col("시가총액", "시총")
    idx_expected = _find_col("기대 수익률", "기대수익률")
    idx_3d_sum   = _find_col("3일합산", "3일 합산")

    rows = []
    for tr in target.find_all("tr"):
        cells = tr.find_all(["td"])
        if not cells or len(cells) < 5:
            continue
        def _cell(i):
            if i < 0 or i >= len(cells):
                return ""
            return cells[i].get_text(strip=True)

        name = _cell(idx_name)
        if not name:
            continue
        # 종목코드: judal 페이지엔 코드가 명시 안 될 수 있음 → 이후 종목명 매칭으로 보강
        rows.append({
            "stock_name": name,
            "amount": _parse_amount_eok(_cell(idx_amount)),
            "current_price": _to_int(_cell(idx_price)),
            "change_pct_52w": _cell(idx_52w_var),     # 원본 텍스트 ("−13%/244%" 형태)
            "change_pct_3y":  _cell(idx_3y_var),
            "pbr": _to_float(_cell(idx_pbr)),
            "per": _to_float(_cell(idx_per)),
            "eps": _to_float(_cell(idx_eps)),
            "market_cap": _parse_amount_eok(_cell(idx_mcap)),
            "expected_return": _cell(idx_expected),
            "three_day_sum": _parse_amount_eok(_cell(idx_3d_sum)),
            "source": "judal",
            "direction": direction,
        })
    log.info("judal %s → %d 종목", direction, len(rows))
    return rows


def fetch_judal_both() -> dict:
    """매수 + 매도 한 번에. return: {stock_name: judal_row}"""
    out = {}
    for d in ("buy", "sell"):
        for r in fetch_judal(d):
            out[r["stock_name"]] = r
    return out


# ==========================================================================
# 통합 fetcher — 외부 자동 vs CSV 수동 폴백
# ==========================================================================

def fetch_auto(merge_judal: bool = True, merge_toss_prices: bool = True,
                merge_toss_trend: bool = True) -> dict:
    """
    자동 fetch 시도. 성공 시:
      {"trade_date": ..., "rows": [...], "source": "todayygg+toss+judal"}
    실패 시 None.

    데이터 소스 머지:
    - todayygg: 활발 종목 발견 (어제 마감 기준)
    - toss trading-trend: 종목별 오늘 장중 매매 (분 단위 갱신) ← 진짜 실시간
    - toss stock-prices/details: 현재가 / 시가총액 / 등락률 (시총비/상승% 계산용)
    - judal: PBR/PER/52주변동률/기대수익률 (보조 가치지표)
    """
    yyg = fetch_todayygg_summary()
    if not yyg:
        return None
    trade_date, rows = todayygg_to_standard_rows(yyg)
    if not rows:
        return None
    sources_used = ["todayygg"]

    # 1) Toss 가격/시총/등락률 머지
    if merge_toss_prices:
        codes = [r["stock_code"] for r in rows if r.get("stock_code")]
        price_map = fetch_toss_stock_prices(codes)
        toss_merged = 0
        for r in rows:
            p = price_map.get(r["stock_code"])
            if not p:
                continue
            # 시가총액: todayygg 값보다 toss 값을 우선 (todayygg는 0인 경우 多)
            if p.get("market_cap"):
                r["market_cap"] = p["market_cap"]
            r["close_price"] = p.get("close", r.get("close_price", 0))
            r["base_price"] = p.get("base", 0)
            r["change_rate"] = p.get("change_rate", 0.0)
            r["volume"] = p.get("volume", 0)
            r["value"] = p.get("value", 0)
            r["high52w"] = p.get("high52w", 0)
            r["low52w"] = p.get("low52w", 0)
            r["trading_strength"] = p.get("trading_strength", 0.0)
            toss_merged += 1
        log.info("toss prices merged: %d/%d", toss_merged, len(rows))
        if toss_merged > 0:
            sources_used.append("toss-prices")

    # 1.5) Toss trading-trend 머지 — 오늘 장중 실시간 매매 데이터로 덮어씀
    intraday_updated_at = ""
    intraday_base_date = ""
    if merge_toss_trend:
        codes = [r["stock_code"] for r in rows if r.get("stock_code")]
        trend_map = fetch_toss_trading_trend(codes)
        today_merged = 0
        for r in rows:
            t = trend_map.get(r["stock_code"])
            if not t:
                continue
            # 토스 trading-trend가 오늘 데이터를 주면 매매 수치 덮어씀
            if t["is_today"]:
                close = r.get("close_price") or t.get("close") or 0
                # 거래량(주) × 종가 = 거래대금 근사
                r["buy_amount"] = t["pension_buy_qty"] * close
                r["sell_amount"] = t["pension_sell_qty"] * close
                r["net_amount"] = t["pension_net_qty"] * close
                r["buy_qty"] = t["pension_buy_qty"]
                r["sell_qty"] = t["pension_sell_qty"]
                r["net_qty"] = t["pension_net_qty"]
                r["intraday"] = True
                r["intraday_updated_at"] = t["updated_at"]
                # net_to_cap 재계산
                mc = r.get("market_cap", 0)
                r["net_to_cap"] = (r["net_amount"] / mc * 100) if mc > 0 else 0.0
                if not intraday_updated_at or t["updated_at"] > intraday_updated_at:
                    intraday_updated_at = t["updated_at"]
                intraday_base_date = t["base_date"]
                today_merged += 1
        log.info("toss trading-trend today data merged: %d/%d", today_merged, len(rows))
        if today_merged > 0:
            sources_used.append("toss-realtime")

    # 2) judal 가치지표 머지 (종목명 기준)
    if merge_judal:
        judal_map = fetch_judal_both()
        merged = 0
        for r in rows:
            jr = judal_map.get(r["stock_name"])
            if jr:
                r["pbr"] = jr.get("pbr")
                r["per"] = jr.get("per")
                r["eps"] = jr.get("eps")
                r["change_pct_52w"] = jr.get("change_pct_52w")
                r["change_pct_3y"] = jr.get("change_pct_3y")
                r["expected_return"] = jr.get("expected_return")
                merged += 1
        log.info("judal merged: %d/%d", merged, len(rows))
        if merged > 0:
            sources_used.append("judal")

    # trade_date 결정: 토스 trading-trend의 base_date(오늘 데이터)가 있으면 그것 우선
    final_trade_date = intraday_base_date.replace("-", "") if intraday_base_date else trade_date

    return {
        "trade_date": final_trade_date,
        "rows": rows,
        "source": "+".join(sources_used),
        "intraday_updated_at": intraday_updated_at,
        "intraday": bool(intraday_updated_at),
    }
