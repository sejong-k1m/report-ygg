"""
일일 연기금 리포트 생성기.

흐름:
1. KRX에서 당일 (또는 직전 영업일) 연기금 매매 데이터 fetch
2. SQLite에 적재 (히스토리 누적용)
3. HTML 리포트 생성 → report/output/index.html
4. 요약 JSON / CSV 도 같이 출력

실행:
    python -m report.generate              # 기본: 오늘(또는 직전 영업일)
    python -m report.generate 20241230     # 특정 일자
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

# 프로젝트 루트를 sys.path에 추가 (단독 실행 지원)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from report import krx_http
from report import csv_import
from report import sources

OUTPUT_DIR = ROOT / "report" / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
INPUT_DIR = ROOT / "report" / "input"
INPUT_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = ROOT / "data" / "pension_report.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

log = logging.getLogger(__name__)


# ==========================================================================
# DB
# ==========================================================================

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS pension_daily_report (
    trade_date  TEXT NOT NULL,
    stock_code  TEXT NOT NULL,
    stock_name  TEXT NOT NULL,
    market      TEXT NOT NULL,
    buy_amount  INTEGER NOT NULL DEFAULT 0,
    sell_amount INTEGER NOT NULL DEFAULT 0,
    net_amount  INTEGER NOT NULL DEFAULT 0,
    buy_qty     INTEGER NOT NULL DEFAULT 0,
    sell_qty    INTEGER NOT NULL DEFAULT 0,
    net_qty     INTEGER NOT NULL DEFAULT 0,
    market_cap  INTEGER NOT NULL DEFAULT 0,
    close_price INTEGER NOT NULL DEFAULT 0,
    sector      TEXT    NOT NULL DEFAULT '',
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (trade_date, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_pdr_date ON pension_daily_report(trade_date);
CREATE INDEX IF NOT EXISTS idx_pdr_net  ON pension_daily_report(trade_date, net_amount);
CREATE INDEX IF NOT EXISTS idx_pdr_code ON pension_daily_report(stock_code, trade_date);

CREATE TABLE IF NOT EXISTS market_summary_daily (
    trade_date  TEXT NOT NULL,
    market      TEXT NOT NULL,
    investor    TEXT NOT NULL,
    buy_total   INTEGER NOT NULL DEFAULT 0,
    sell_total  INTEGER NOT NULL DEFAULT 0,
    net_total   INTEGER NOT NULL DEFAULT 0,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (trade_date, market, investor)
);
"""


def _migrate_schema(conn):
    """기존 DB 에 누락된 컬럼 자동 추가."""
    cur = conn.cursor()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(pension_daily_report)").fetchall()]
    if "close_price" not in cols:
        cur.execute("ALTER TABLE pension_daily_report ADD COLUMN close_price INTEGER NOT NULL DEFAULT 0")
        log.info("DB 마이그레이션: close_price 컬럼 추가됨")
    if "sector" not in cols:
        cur.execute("ALTER TABLE pension_daily_report ADD COLUMN sector TEXT NOT NULL DEFAULT ''")
        log.info("DB 마이그레이션: sector 컬럼 추가됨")
    conn.commit()


def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(DB_SCHEMA)
    _migrate_schema(conn)
    return conn


def upsert_pension_daily(conn, trade_date: str, market: str, rows: list, cap_map: dict):
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.cursor()
    for r in rows:
        if not r["stock_code"]:
            continue
        mcap = cap_map.get(r["stock_code"], 0)
        close = int(r.get("close_price", 0) or 0)
        sector = (r.get("sector") or "").strip()
        cur.execute("""
            INSERT INTO pension_daily_report
              (trade_date, stock_code, stock_name, market, buy_amount, sell_amount, net_amount,
               buy_qty, sell_qty, net_qty, market_cap, close_price, sector, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trade_date, stock_code) DO UPDATE SET
              stock_name = excluded.stock_name,
              market = excluded.market,
              buy_amount = excluded.buy_amount,
              sell_amount = excluded.sell_amount,
              net_amount = excluded.net_amount,
              buy_qty = excluded.buy_qty,
              sell_qty = excluded.sell_qty,
              net_qty = excluded.net_qty,
              market_cap = excluded.market_cap,
              close_price = CASE WHEN excluded.close_price > 0 THEN excluded.close_price ELSE pension_daily_report.close_price END,
              sector = CASE WHEN excluded.sector != '' THEN excluded.sector ELSE pension_daily_report.sector END,
              fetched_at = excluded.fetched_at
        """, (
            trade_date, r["stock_code"], r["stock_name"], market,
            r["buy_amount"], r["sell_amount"], r["net_amount"],
            r["buy_qty"], r["sell_qty"], r["net_qty"],
            mcap, close, sector, now,
        ))
    conn.commit()


def upsert_market_summary(conn, trade_date: str, market: str, summary: dict):
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.cursor()
    for investor, s in summary.items():
        cur.execute("""
            INSERT INTO market_summary_daily
              (trade_date, market, investor, buy_total, sell_total, net_total, fetched_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(trade_date, market, investor) DO UPDATE SET
              buy_total = excluded.buy_total,
              sell_total = excluded.sell_total,
              net_total = excluded.net_total,
              fetched_at = excluded.fetched_at
        """, (
            trade_date, market, investor,
            s.get("buy", 0), s.get("sell", 0), s.get("net", 0),
            now,
        ))
    conn.commit()


# ==========================================================================
# 데이터 수집
# ==========================================================================

def collect_one_day(trade_date: str, markets=("KOSPI", "KOSDAQ"), use_auto: bool = True,
                     mode: str = "realtime") -> dict:
    """
    하루치 연기금 데이터 수집 + DB 저장.

    mode:
      - "realtime": 오늘 장중 toss trading-trend 머지 (장중 진행 데이터)
      - "closing":  trading-trend 제외, todayygg 마감 데이터만 (직전 영업일 15:30 기준)

    데이터 소스 우선순위:
    1. use_auto=True 면 todayygg + toss + judal 자동 fetch
    2. 자동 실패 시 report/input/ CSV (수동 다운로드)
    3. 둘 다 없으면 빈 리포트 + 안내 배너
    """
    conn = _db_conn()
    all_rows = []
    summaries = {}
    actual_trade_date = trade_date
    source_used = "none"

    # closing 모드는 toss trading-trend 머지 안 함 (장중 데이터로 오염 방지)
    merge_intraday = (mode == "realtime")

    # --- 1) AUTO: todayygg + judal ---
    if use_auto:
        log.info("자동 fetch 시도: todayygg + judal ... (mode=%s, intraday=%s)",
                 mode, merge_intraday)
        auto = sources.fetch_auto(merge_judal=True, merge_toss_trend=merge_intraday)
        if auto and auto.get("rows"):
            actual_trade_date = auto.get("trade_date") or trade_date
            source_used = auto.get("source", "todayygg")
            log.info("자동 fetch 성공 [%s]: %d 종목 (date=%s)",
                     source_used, len(auto["rows"]), actual_trade_date)
            # todayygg는 KOSPI/KOSDAQ 한 덩어리로 줌. row의 market 필드로 그룹핑.
            for r in auto["rows"]:
                mkt = r.get("market") or "KOSPI"
                # 정규화: 'STK' → 'KOSPI' 등
                if mkt in ("STK", "유가증권"):
                    mkt = "KOSPI"
                elif mkt in ("KSQ", "코스닥"):
                    mkt = "KOSDAQ"
                r["market"] = mkt
                mc = r.get("market_cap", 0)
                r["net_to_cap"] = (r["net_amount"] / mc * 100) if mc > 0 else 0.0
            all_rows = auto["rows"]
            # market별 cap_map / DB upsert
            for market in markets:
                market_rows = [r for r in all_rows if r["market"] == market]
                cap_map = {r["stock_code"]: r.get("market_cap", 0) for r in market_rows if r.get("market_cap")}
                upsert_pension_daily(conn, actual_trade_date, market, market_rows, cap_map)
                # 시장 총합
                tb = sum(r["buy_amount"] for r in market_rows)
                ts = sum(r["sell_amount"] for r in market_rows)
                summary = {"연기금": {"buy": tb, "sell": ts, "net": tb - ts}}
                upsert_market_summary(conn, actual_trade_date, market, summary)
                summaries[market] = summary
            conn.close()
            return {
                "trade_date": actual_trade_date,
                "markets": list(markets),
                "rows": all_rows,
                "summaries": summaries,
                "source": source_used,
            }
        else:
            log.warning("자동 fetch 실패 → CSV 폴백 시도")

    # --- 2) CSV 폴백 ---
    csv_map = csv_import.find_csvs_for_date(trade_date, INPUT_DIR)
    if csv_map:
        log.info("CSV input 발견: %s", {k: v.name for k, v in csv_map.items()})
        source_used = "csv"
    else:
        log.warning("CSV input 없음 (date=%s) — report/input/ 에 krx_%s_KOSPI.csv 등이 필요",
                    trade_date, trade_date)

    for market in markets:
        log.info("Processing %s %s ...", trade_date, market)
        cap_map = {}
        summary = {}
        rows = []

        csv_path = csv_map.get(market) or csv_map.get("ALL")
        if csv_path:
            log.info("  CSV에서 로드: %s", csv_path.name)
            rows = csv_import.parse_krx_csv(csv_path)
            for r in rows:
                if r.get("market_cap"):
                    cap_map[r["stock_code"]] = r["market_cap"]

        if not summary and rows:
            total_buy = sum(r["buy_amount"] for r in rows)
            total_sell = sum(r["sell_amount"] for r in rows)
            summary = {"연기금": {
                "buy": total_buy, "sell": total_sell,
                "net": total_buy - total_sell,
            }}

        upsert_pension_daily(conn, trade_date, market, rows, cap_map)
        upsert_market_summary(conn, trade_date, market, summary)

        for r in rows:
            r["market"] = market
            r["market_cap"] = cap_map.get(r["stock_code"], r.get("market_cap", 0))
            mc = r["market_cap"]
            r["net_to_cap"] = (r["net_amount"] / mc * 100) if mc > 0 else 0.0
        all_rows.extend(rows)
        summaries[market] = summary

    conn.close()
    return {
        "trade_date": trade_date,
        "markets": list(markets),
        "rows": all_rows,
        "summaries": summaries,
        "source": source_used,
    }


def query_recent_summaries(days: int = 7, market: str = "KOSPI") -> list:
    """최근 N거래일 연기금 총합 (HTML 차트용)."""
    conn = _db_conn()
    rows = conn.execute("""
        SELECT trade_date, buy_total, sell_total, net_total
        FROM market_summary_daily
        WHERE market=? AND investor='연기금'
        ORDER BY trade_date DESC
        LIMIT ?
    """, (market, days)).fetchall()
    conn.close()
    return [dict(r) for r in rows]  # 최신 날짜가 맨 위 (DESC 정렬 유지)


def query_cumulative_top(top_n: int = 30, direction: str = "buy") -> list:
    """
    REPORT-YGG 시행 이후 전체 누적 (날짜 제한 없음).
    DB의 pension_daily_report 전 일자 합산해서 종목별 net_amount sum.
    """
    order = "DESC" if direction == "buy" else "ASC"
    conn = _db_conn()
    rows = conn.execute(f"""
        SELECT stock_code,
               MAX(stock_name) AS stock_name,
               MAX(market)     AS market,
               SUM(buy_amount)  AS buy_sum,
               SUM(sell_amount) AS sell_sum,
               SUM(net_amount)  AS net_sum,
               COUNT(DISTINCT trade_date) AS day_count,
               MAX(market_cap)  AS market_cap,
               MIN(trade_date)  AS first_date,
               MAX(trade_date)  AS last_date
        FROM pension_daily_report
        GROUP BY stock_code
        HAVING net_sum != 0
        ORDER BY net_sum {order}
        LIMIT ?
    """, (top_n,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def query_weekly_top(days: int = 7, top_n: int = 30, direction: str = "buy") -> list:
    """
    최근 N거래일 누적 연기금 순매수/순매도 종목 Top.
    direction='buy' → 누적 순매수 desc, 'sell' → 누적 순매수 asc (= 순매도 큰 순)
    """
    import datetime as dt
    earliest = (dt.date.today() - dt.timedelta(days=days * 2)).strftime("%Y%m%d")
    order = "DESC" if direction == "buy" else "ASC"
    conn = _db_conn()
    rows = conn.execute(f"""
        SELECT stock_code,
               MAX(stock_name) AS stock_name,
               MAX(market)     AS market,
               SUM(buy_amount)  AS buy_sum,
               SUM(sell_amount) AS sell_sum,
               SUM(net_amount)  AS net_sum,
               COUNT(DISTINCT trade_date) AS day_count,
               MAX(market_cap)  AS market_cap
        FROM pension_daily_report
        WHERE trade_date >= ?
        GROUP BY stock_code
        HAVING net_sum != 0
        ORDER BY net_sum {order}
        LIMIT ?
    """, (earliest, top_n)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def query_bulk_qty_daily(threshold: int = 100_000, days: int = 30, limit: int = 200) -> list:
    """
    최근 N일 동안 일일 |net_qty| >= threshold (10만주) 인 연기금 매매 내역.
    return: list of dict (trade_date, stock_code, stock_name, market, net_qty, net_amount, ...)
    """
    import datetime as dt
    earliest = (dt.date.today() - dt.timedelta(days=days)).strftime("%Y%m%d")
    conn = _db_conn()
    rows = conn.execute("""
        SELECT trade_date, stock_code, stock_name, market,
               buy_qty, sell_qty, net_qty,
               buy_amount, sell_amount, net_amount, market_cap
        FROM pension_daily_report
        WHERE trade_date >= ?
          AND ABS(net_qty) >= ?
        ORDER BY trade_date DESC, ABS(net_qty) DESC
        LIMIT ?
    """, (earliest, threshold, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def query_bulk_qty_cumulative(threshold: int = 100_000, days: int = 7, limit: int = 100) -> list:
    """
    최근 N거래일 누적 |SUM(net_qty)| >= threshold 인 연기금 종목 누적 매매.
    return: list of dict (stock_code, stock_name, market, net_qty_sum, net_amount_sum, day_count, ...)
    """
    import datetime as dt
    earliest = (dt.date.today() - dt.timedelta(days=days * 2)).strftime("%Y%m%d")
    conn = _db_conn()
    rows = conn.execute("""
        SELECT stock_code,
               MAX(stock_name) AS stock_name,
               MAX(market)     AS market,
               SUM(buy_qty)    AS buy_qty_sum,
               SUM(sell_qty)   AS sell_qty_sum,
               SUM(net_qty)    AS net_qty_sum,
               SUM(buy_amount) AS buy_amount_sum,
               SUM(sell_amount) AS sell_amount_sum,
               SUM(net_amount) AS net_amount_sum,
               COUNT(DISTINCT trade_date) AS day_count,
               MAX(market_cap) AS market_cap
        FROM pension_daily_report
        WHERE trade_date >= ?
        GROUP BY stock_code
        HAVING ABS(net_qty_sum) >= ?
        ORDER BY ABS(net_qty_sum) DESC
        LIMIT ?
    """, (earliest, threshold, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def query_traded_stock_codes(days: int = 90) -> set:
    """
    최근 N일 동안 연기금이 매매한 적 있는 종목 코드 집합.
    (5%룰 공시 필터링용 — '연기금 매매 종목만' 노출 위해)
    """
    import datetime as dt
    earliest = (dt.date.today() - dt.timedelta(days=days)).strftime("%Y%m%d")
    conn = _db_conn()
    rows = conn.execute("""
        SELECT DISTINCT stock_code FROM pension_daily_report
        WHERE trade_date >= ?
    """, (earliest,)).fetchall()
    conn.close()
    return {r[0] for r in rows if r[0]}


def query_stock_meta_map() -> dict:
    """DB 의 모든 종목 → {stock_code: {name, market}} 매핑 (5%룰 표 종목명 채우기용)."""
    conn = _db_conn()
    rows = conn.execute("""
        SELECT stock_code, MAX(stock_name) AS name, MAX(market) AS market
        FROM pension_daily_report
        GROUP BY stock_code
    """).fetchall()
    conn.close()
    return {r["stock_code"]: {"name": r["name"], "market": r["market"]} for r in rows}


# ==========================================================================
# 테마/업종 (sector) 별 집계 — themes.html 페이지용
# ==========================================================================

def aggregate_by_sector(rows: list, sector_field: str = "sector") -> list:
    """
    rows → sector 별 매수/매도/순매수 집계.
    return: [{sector, count, buy, sell, net, buy_stocks, sell_stocks, codes}, ...]
            (net desc 정렬)
    """
    by_sector = {}
    for r in rows:
        sec = _clean_sector_name((r.get(sector_field) or "").strip())
        if sec not in by_sector:
            by_sector[sec] = {
                "sector": sec,
                "count": 0, "buy": 0, "sell": 0, "net": 0,
                "buy_stocks": 0, "sell_stocks": 0,
                "codes": [],
            }
        s = by_sector[sec]
        s["count"] += 1
        s["buy"] += r.get("buy_amount", 0) or 0
        s["sell"] += r.get("sell_amount", 0) or 0
        s["net"] += r.get("net_amount", 0) or 0
        net = r.get("net_amount", 0) or 0
        if net > 0:
            s["buy_stocks"] += 1
        elif net < 0:
            s["sell_stocks"] += 1
        s["codes"].append(r["stock_code"])
    return sorted(by_sector.values(), key=lambda x: x["net"], reverse=True)


def _classify_burning(today_avg: float, period_avg: float) -> str:
    """
    오늘 매수평단 vs 구간 평단 비교:
    - today > period (+0.5%↑) → "burning" (불타기 🔥) — 오늘 평단이 더 비싸게 사고 있음
    - today < period (-0.5%↑) → "watering" (물타기 💧) — 오늘 평단이 더 싸게 사고 있음
    - 그 외 → "neutral" (보합 ━)
    - 데이터 없음 → "unknown" (❓)
    """
    if today_avg <= 0 or period_avg <= 0:
        return "unknown"
    diff_pct = abs(today_avg - period_avg) / period_avg * 100
    if diff_pct < 0.5:
        return "neutral"
    return "burning" if today_avg > period_avg else "watering"


def build_continuity_data(rows: list = None, min_days: int = 2,
                          min_cumul: int = 0) -> list:
    """
    우리 DB (pension_daily_report) 시계열 기반 연속 매수 종목 계산.
    각 종목별로 가장 최근 trade_date 부터 거꾸로 가며 net_amount > 0 인 연속 일수 카운트.
    그 구간의 sum(net), sum(buy_amount), sum(buy_qty) 로 누적/평단 계산.

    조건: 연속 일수 >= min_days
    정렬: 구간 누적 순매수 desc
    return: list of dict
    """
    conn = _db_conn()
    db_rows = conn.execute("""
        SELECT stock_code, trade_date, stock_name, market,
               net_amount, buy_amount, sell_amount,
               buy_qty, sell_qty, close_price, market_cap
        FROM pension_daily_report
        WHERE trade_date >= ?
        ORDER BY stock_code, trade_date DESC
    """, ((dt.date.today() - dt.timedelta(days=180)).strftime("%Y%m%d"),)).fetchall()
    conn.close()

    # 종목별 그룹화 (이미 trade_date DESC 정렬)
    by_code = {}
    for r in db_rows:
        by_code.setdefault(r["stock_code"], []).append(dict(r))

    items = []
    pos_today_count = 0
    consec_2_count = 0
    for code, series in by_code.items():
        if not series:
            continue
        # 가장 최근부터 net > 0 연속 카운트
        consec = 0
        consec_buy_amount = 0
        consec_buy_qty = 0
        consec_net_amount = 0
        latest_date = series[0]["trade_date"]
        oldest_consec_date = latest_date
        today = series[0]
        if (today["net_amount"] or 0) > 0:
            pos_today_count += 1
        for s in series:
            if (s["net_amount"] or 0) > 0:
                consec += 1
                consec_buy_amount += s["buy_amount"] or 0
                consec_buy_qty += s["buy_qty"] or 0
                consec_net_amount += s["net_amount"] or 0
                oldest_consec_date = s["trade_date"]
            else:
                break   # 연속 끊김
        if consec >= 2:
            consec_2_count += 1

        # 필터: 연속 일수 + 누적
        if consec < min_days:
            continue
        if consec_net_amount < min_cumul:
            continue

        # 오늘 (최신) 평단
        today_buy_amt = today["buy_amount"] or 0
        today_buy_q = today["buy_qty"] or 0
        today_avg = (today_buy_amt / today_buy_q) if today_buy_q > 0 else 0
        # 구간 평단 (오늘 포함 전체 가중 평균)
        period_avg = (consec_buy_amount / consec_buy_qty) if consec_buy_qty > 0 else 0
        # 오늘 평단 vs 이전 (구간 안 오늘 제외) 평단
        prev_buy_amt = consec_buy_amount - today_buy_amt
        prev_buy_q = consec_buy_qty - today_buy_q
        prev_avg = (prev_buy_amt / prev_buy_q) if prev_buy_q > 0 else 0
        burn = _classify_burning(today_avg, prev_avg) if prev_avg > 0 else "unknown"

        def _fmt_d(d):
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d and len(d) == 8 else (d or "")

        items.append({
            "code": code,
            "name": today["stock_name"],
            "market": today["market"],
            "days": consec,
            "period_start": _fmt_d(oldest_consec_date),
            "period_end": _fmt_d(latest_date),
            "today_avg": int(today_avg),
            "period_avg": int(period_avg),
            "prev_avg": int(prev_avg),
            "burn": burn,
            "cumul": int(consec_net_amount),
            "net_amount": today["net_amount"] or 0,
            "close_price": today["close_price"] or 0,
        })

    items.sort(key=lambda x: x["cumul"], reverse=True)
    log.info(
        "continuity (DB 시계열): 종목 %d개 중 오늘 매수 %d개, 연속2일↑ %d개, %s일↑ 조건 통과 %d개",
        len(by_code), pos_today_count, consec_2_count, min_days, len(items),
    )
    return items[:200]


# 표준 sector 카테고리 (이것만 사용)
_ALLOWED_SECTORS = {
    "반도체", "디스플레이", "2차전지", "바이오/제약", "자동차", "방산", "조선",
    "금융", "IT/소프트웨어", "전기/전자", "미디어/통신",
    "화학/정유", "철강/금속", "소비재/유통", "음식료/소비재",
    "건설/인프라", "산업재", "부동산", "ETF/펀드",
    "에너지", "유틸리티", "운송", "헬스케어", "화장품", "기타",
}

# 별칭 → 표준 이름 매핑 (중복 카테고리 통합)
_SECTOR_ALIASES = {
    "소프트웨어":   "IT/소프트웨어",
    "IT":          "IT/소프트웨어",
    "인터넷":       "IT/소프트웨어",
    "IT/전자":     "전기/전자",
    "전자":        "전기/전자",
    "전기전자":     "전기/전자",
    "통신":        "미디어/통신",
    "미디어":       "미디어/통신",
    "음식료":       "음식료/소비재",
    "유통":        "소비재/유통",
    "소비재":       "소비재/유통",
    "ETF":         "ETF/펀드",
    "펀드":        "ETF/펀드",
    "화학":        "화학/정유",
    "정유":        "화학/정유",
    "철강":        "철강/금속",
    "건설":        "건설/인프라",
    "농업":        "기타",   # 종목 수 적어서 통합
    "광물":        "철강/금속",
}


def _clean_sector_name(sec: str) -> str:
    """sector 이름 정규화: 별칭→표준, dirty→'기타'."""
    if not sec:
        return "기타"
    sec = sec.strip()
    # 별칭 매핑 우선
    if sec in _SECTOR_ALIASES:
        return _SECTOR_ALIASES[sec]
    if sec in _ALLOWED_SECTORS:
        return sec
    # dirty 패턴
    if len(sec) > 10:
        return "기타"
    if "(" in sec or ")" in sec:
        return "기타"
    if sec.count(" ") > 1:
        return "기타"
    return sec   # 짧고 단순하면 통과 (혹시 새 카테고리 일 수도)


def query_sector_aggregates(days_offset: int = 0, days_count: int = 1) -> list:
    """
    DB 의 sector 별 집계. distinct trade_date 기준 영업일 슬라이스.
    days_offset=0, days_count=1 → 가장 최근 영업일 (오늘)
    days_offset=1, days_count=1 → 그 직전 영업일 (어제)
    days_offset=0, days_count=7 → 최근 7 영업일 누적
    return: [{sector, count, buy, sell, net, buy_stocks, sell_stocks}, ...] (net desc)
    """
    conn = _db_conn()
    dates = conn.execute("""
        SELECT DISTINCT trade_date FROM pension_daily_report
        ORDER BY trade_date DESC LIMIT 30
    """).fetchall()
    dates = [r[0] for r in dates]
    if days_offset >= len(dates):
        conn.close()
        return []
    target_dates = dates[days_offset:days_offset + days_count]
    if not target_dates:
        conn.close()
        return []
    placeholders = ",".join("?" * len(target_dates))
    # 종목 단위로 가져옴 — Python 에서 종목명 기반 재분류 후 sector 별 그룹핑
    rows = conn.execute(f"""
        SELECT
          stock_code,
          MAX(stock_name) AS stock_name,
          MAX(sector) AS sector,
          SUM(buy_amount)  AS buy,
          SUM(sell_amount) AS sell,
          SUM(net_amount)  AS net
        FROM pension_daily_report
        WHERE trade_date IN ({placeholders})
        GROUP BY stock_code
        HAVING SUM(buy_amount) + SUM(sell_amount) > 0
    """, target_dates).fetchall()
    conn.close()

    # 종목명 기반 재분류 → sector 별 그룹핑
    from report.sources import auto_classify_sector
    grouped = {}
    for r in rows:
        # 1) 키워드 매핑 우선 (한양디지텍 등 명시 매핑)
        sec = auto_classify_sector(r["stock_code"], r["stock_name"])
        # 2) 매핑 실패 시 DB sector 를 _clean 한 값
        if not sec:
            sec = _clean_sector_name(r["sector"] or "")
        if sec not in grouped:
            grouped[sec] = {
                "sector": sec, "count": 0, "buy": 0, "sell": 0, "net": 0,
                "buy_stocks": 0, "sell_stocks": 0,
            }
        g = grouped[sec]
        g["count"] += 1
        g["buy"] += r["buy"] or 0
        g["sell"] += r["sell"] or 0
        g["net"] += r["net"] or 0
        net = r["net"] or 0
        if net > 0:
            g["buy_stocks"] += 1
        elif net < 0:
            g["sell_stocks"] += 1
    return sorted(grouped.values(), key=lambda x: x["net"], reverse=True)


def query_sector_stocks_for_period(days_offset: int = 0, days_count: int = 1) -> dict:
    """
    DB 기간 안의 sector 별 종목 누적 데이터.
    return: {sector: [{code, name, market, net, net_to_cap, ...}, ...]}
    """
    conn = _db_conn()
    dates = conn.execute("""
        SELECT DISTINCT trade_date FROM pension_daily_report
        ORDER BY trade_date DESC LIMIT 30
    """).fetchall()
    dates = [r[0] for r in dates]
    if days_offset >= len(dates):
        conn.close()
        return {}
    target = dates[days_offset:days_offset + days_count]
    if not target:
        conn.close()
        return {}
    ph = ",".join("?" * len(target))
    rows = conn.execute(f"""
        SELECT
          stock_code,
          MAX(stock_name) AS stock_name,
          MAX(market) AS market,
          MAX(sector) AS sector,
          SUM(net_amount) AS net,
          MAX(market_cap) AS market_cap,
          MAX(close_price) AS close_price,
          COUNT(DISTINCT trade_date) AS day_count
        FROM pension_daily_report
        WHERE trade_date IN ({ph})
        GROUP BY stock_code
        HAVING SUM(net_amount) != 0
    """, target).fetchall()
    conn.close()

    stocks_by_sec = {}
    for r in rows:
        sec = _clean_sector_name(r["sector"] or "")
        cap = r["market_cap"] or 0
        net = r["net"] or 0
        net_to_cap = (net / cap * 100) if cap > 0 else 0
        item = {
            "code": r["stock_code"],
            "name": r["stock_name"],
            "market": r["market"],
            "net": net,
            "net_to_cap": round(net_to_cap, 4),
            "today_buy_avg": 0,
            "period_buy_avg": 0,
            "period_start": "",
            "period_end": "",
            "change_rate": 0,
            "close_price": r["close_price"] or 0,
            "day_count": r["day_count"] or 0,
        }
        stocks_by_sec.setdefault(sec, []).append(item)

    for sec in stocks_by_sec:
        stocks_by_sec[sec].sort(key=lambda x: x["net"], reverse=True)
    return stocks_by_sec


def build_theme_data(rows: list) -> dict:
    """
    테마 페이지용 JSON 페이로드.
    return: {
        sectors: [{sector, count, buy, sell, net, buy_stocks, sell_stocks}, ...],
        stocks_by_sector: {sector: [{code, name, net, net_to_cap, today_buy_avg,
                                     period_buy_avg, period_start, period_end}, ...]},
    }
    """
    aggs = aggregate_by_sector(rows, "sector")
    stocks_by_sec = {}
    for r in rows:
        sec = _clean_sector_name((r.get("sector") or "").strip())
        stocks_by_sec.setdefault(sec, []).append({
            "code": r["stock_code"],
            "name": r["stock_name"],
            "net": r.get("net_amount", 0) or 0,
            "net_to_cap": round((r.get("net_to_cap", 0) or 0) * 100, 4),  # %
            "today_buy_avg": int(r.get("today_buy_avg", 0) or 0),
            "period_buy_avg": int(r.get("period_buy_avg", 0) or 0),
            "period_start": r.get("period_start_date", "") or "",
            "period_end": r.get("period_end_date", "") or "",
            "change_rate": r.get("change_rate", 0) or 0,
            "close_price": r.get("close_price", 0) or 0,
        })
    # 각 sector 내에서 net desc 정렬
    for sec in stocks_by_sec:
        stocks_by_sec[sec].sort(key=lambda x: x["net"], reverse=True)
    # codes 키 제외하고 반환 (페이로드 크기 줄임)
    sectors_out = [
        {k: v for k, v in s.items() if k != "codes"}
        for s in aggs
    ]
    return {"sectors": sectors_out, "stocks_by_sector": stocks_by_sec}


# ==========================================================================
# RSI 14일 — DB close_price 시계열 기반
# ==========================================================================

def compute_rsi(prices: list, period: int = 14) -> Optional[float]:
    """
    가격 시계열 (오래된 → 최신) → RSI (0~100).
    시계열 길이 < period+1 이면 None.
    SMA 기반 (Wilder smoothing 대신 단순화).
    """
    if not prices or len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def query_recent_close_prices(stock_codes: list, days: int = 30) -> dict:
    """
    종목별 최근 N일 close_price 시계열 (오래된 → 최신).
    return: {stock_code: [(trade_date, close_price), ...]}
    """
    if not stock_codes:
        return {}
    earliest = (dt.date.today() - dt.timedelta(days=days * 2)).strftime("%Y%m%d")
    # SQL IN 절: 종목 수가 많으면 placeholder chunk 처리
    result = {}
    conn = _db_conn()
    CHUNK = 500
    for i in range(0, len(stock_codes), CHUNK):
        chunk = list(stock_codes[i:i + CHUNK])
        ph = ",".join("?" * len(chunk))
        rows = conn.execute(f"""
            SELECT stock_code, trade_date, close_price
            FROM pension_daily_report
            WHERE stock_code IN ({ph})
              AND trade_date >= ?
              AND close_price > 0
            ORDER BY stock_code, trade_date ASC
        """, chunk + [earliest]).fetchall()
        for r in rows:
            result.setdefault(r["stock_code"], []).append(
                (r["trade_date"], int(r["close_price"]))
            )
    conn.close()
    return result


def compute_rsi_for_rows(rows: list, period: int = 14) -> dict:
    """
    rows 의 각 종목에 대해 RSI 계산.
    오늘 close_price 도 시계열에 추가 (DB 에 아직 안 들어간 최신 데이터 포함).
    return: {stock_code: rsi (float) or None}
    """
    codes = [r["stock_code"] for r in rows if r.get("stock_code")]
    if not codes:
        return {}
    series = query_recent_close_prices(codes, days=30)
    today = dt.date.today().strftime("%Y%m%d")
    by_code_today = {r["stock_code"]: int(r.get("close_price", 0) or 0) for r in rows}

    out = {}
    for code in codes:
        ts = series.get(code, [])
        prices = [p for _, p in ts]
        today_price = by_code_today.get(code, 0)
        if today_price > 0:
            last_date = ts[-1][0] if ts else ""
            if last_date != today:
                prices.append(today_price)
        out[code] = compute_rsi(prices, period=period)
    return out


def attach_rsi_to_rows(rows: list, period: int = 14):
    """rows in-place 에 'rsi' 필드 추가."""
    rsi_map = compute_rsi_for_rows(rows, period=period)
    for r in rows:
        r["rsi"] = rsi_map.get(r["stock_code"])
    return rows


# ==========================================================================
# HTML 생성
# ==========================================================================

def _fmt_won(amount: int, scale: str = "auto") -> str:
    """금액 표시. scale='auto': 억 단위."""
    if amount is None:
        return "-"
    abs_v = abs(amount)
    sign = "-" if amount < 0 else ""
    if abs_v >= 100_000_000:
        return f"{sign}{abs_v / 100_000_000:,.1f}억"
    if abs_v >= 10_000:
        return f"{sign}{abs_v / 10_000:,.0f}만"
    return f"{sign}{abs_v:,}원"


def _fmt_pct(p: float) -> str:
    if p is None:
        return "-"
    return f"{p:+.2f}%"


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _table_top_rows(rows: list, key="net_amount", desc=True, n=30) -> list:
    sorted_rows = sorted(rows, key=lambda r: r.get(key, 0), reverse=desc)
    return [r for r in sorted_rows if r.get(key, 0) != 0][:n]


def _period_days(r: dict) -> int:
    """todayygg period_start/end → 활발 매매 기간 일수."""
    s = r.get("period_start_date", "")
    e = r.get("period_end_date", "")
    if not s or not e:
        return 0
    try:
        s_d = dt.date.fromisoformat(s[:10])
        e_d = dt.date.fromisoformat(e[:10])
        return (e_d - s_d).days + 1
    except Exception:
        return 0


def _consecutive_label(r: dict) -> str:
    """연속 매수/매도 라벨. 예: '🔥 5일 연속매수' / '❄️ 3일 연속매도'."""
    net = r.get("net_amount", 0)
    days = _period_days(r)
    sell_days = r.get("consecutive_sell_days", 0)
    if sell_days >= 2:
        return f"❄️ {sell_days}일 연속매도"
    if net > 0 and days >= 2:
        return f"🔥 {days}일 연속매수"
    return ""


def _ai_score_breakdown(r: dict) -> dict:
    """
    AI 점수의 각 변수 기여도 분해. AI 점수 페이지에서 표시용.
    return: {component_name: score_contribution, ..., "total": 합계, "rsi": 원본 RSI(0~100)}
    """
    cap = round((r.get("net_to_cap", 0) or 0) * 100, 1)
    val_ratio = round((r.get("net_vs_prev_val_ratio") or 0) * 5, 1)
    change = round((r.get("change_rate", 0) or 0) * 1.5, 1)
    ts = r.get("trading_strength", 0) or 0
    strength = round((ts - 100) * 0.05, 1) if ts > 0 else 0.0
    period = round(_period_days(r) * 4, 1) if r.get("net_amount", 0) > 0 else 0.0
    cumul = round(((r.get("cumulative_net_amount") or 0) / 100_000_000) * 0.005, 1)
    delta = round(((r.get("delta_net_amount") or 0) / 100_000_000) * 0.02, 1)
    sell_penalty = round(-(r.get("consecutive_sell_days", 0) or 0) * 8, 1)
    dart = r.get("dart_score", 0) or 0
    # RSI 14일: 과매수(≥70) → 페널티, 과매도(≤30) → 보너스, 중간 → 0
    rsi = r.get("rsi")
    rsi_score = 0.0
    if rsi is not None:
        if rsi >= 70:
            rsi_score = round(-(rsi - 70) * 0.8, 1)
        elif rsi <= 30:
            rsi_score = round((30 - rsi) * 0.8, 1)
    total = cap + val_ratio + change + strength + period + cumul + delta + sell_penalty + dart + rsi_score
    return {
        "cap": cap,                # 매수비율 × 100
        "val_ratio": val_ratio,    # 전일거래액 비율 × 5
        "change": change,          # 등락률 × 1.5
        "strength": strength,      # 거래량 강도 × 0.05
        "period": period,          # 활발 기간 × 4
        "cumul": cumul,            # 누적 순매수 × 0.005
        "delta": delta,            # 전일대비 변화 × 0.02
        "sell_penalty": sell_penalty,  # 연속매도 × -8
        "dart": dart,              # DART 공시
        "rsi_score": rsi_score,    # RSI 14일 과매수/과매도 (-16 ~ +16)
        "rsi": rsi,                # 원본 RSI 값 (표시용, None 가능)
        "total": round(total, 1),
    }


def _ai_score_explain(r: dict, breakdown: dict) -> dict:
    """AI 점수 자연어 근거 + 투자 추천."""
    total = breakdown["total"]
    reasons = []
    net_to_cap = r.get("net_to_cap", 0) or 0
    if breakdown["cap"] >= 30:
        reasons.append(f"🔥 매수비율 +{net_to_cap:.3f}% — 시총 대비 강한 자금 유입 (+{breakdown['cap']:.0f})")
    elif breakdown["cap"] >= 10:
        reasons.append(f"📈 매수비율 +{net_to_cap:.3f}% — 적극 매수 (+{breakdown['cap']:.0f})")
    elif breakdown["cap"] <= -30:
        reasons.append(f"❄️ 매도비율 {net_to_cap:.3f}% — 시총 대비 강한 자금 이탈 ({breakdown['cap']:.0f})")
    elif breakdown["cap"] <= -10:
        reasons.append(f"📉 매도비율 {net_to_cap:.3f}% — 매도 우세 ({breakdown['cap']:.0f})")
    if breakdown["period"] > 0:
        d = _period_days(r)
        reasons.append(f"🔥 {d}일 연속 활발 매수 (+{breakdown['period']:.0f})")
    if breakdown["sell_penalty"] <= -16:
        d = r.get("consecutive_sell_days", 0) or 0
        reasons.append(f"❄️ {d}일 연속 매도 — 강한 매도세 ({breakdown['sell_penalty']:.0f})")
    elif breakdown["sell_penalty"] <= -8:
        d = r.get("consecutive_sell_days", 0) or 0
        reasons.append(f"❄️ {d}일 연속 매도 ({breakdown['sell_penalty']:.0f})")
    if breakdown["dart"] >= 10:
        matched = r.get("dart_matched") or []
        kws = ", ".join(matched[:3]) if matched else "호재 공시"
        reasons.append(f"📜 DART 호재: {kws} (+{breakdown['dart']:.0f})")
    elif breakdown["dart"] <= -10:
        matched = r.get("dart_matched") or []
        kws = ", ".join(matched[:3]) if matched else "악재 공시"
        reasons.append(f"⚠ DART 악재: {kws} ({breakdown['dart']:.0f})")
    change = r.get("change_rate", 0) or 0
    if breakdown["change"] >= 5:
        reasons.append(f"📈 오늘 등락률 +{change:.2f}% — 강한 상승 (+{breakdown['change']:.1f})")
    elif breakdown["change"] <= -5:
        reasons.append(f"📉 오늘 등락률 {change:.2f}% — 큰 하락 ({breakdown['change']:.1f})")
    ts = r.get("trading_strength", 0) or 0
    if breakdown["strength"] >= 5:
        reasons.append(f"💪 거래량 강도 {ts:.0f} — 평소 대비 활발 (+{breakdown['strength']:.1f})")
    if breakdown["delta"] >= 3:
        reasons.append(f"📊 전일 대비 순매수 확대 (+{breakdown['delta']:.1f})")
    elif breakdown["delta"] <= -3:
        reasons.append(f"📊 전일 대비 순매수 축소 ({breakdown['delta']:.1f})")
    if breakdown["cumul"] >= 2:
        cumul_eok = (r.get("cumulative_net_amount") or 0) / 100_000_000
        reasons.append(f"📅 활발 기간 누적 +{cumul_eok:,.0f}억 매수 (+{breakdown['cumul']:.1f})")
    elif breakdown["cumul"] <= -2:
        cumul_eok = (r.get("cumulative_net_amount") or 0) / 100_000_000
        reasons.append(f"📅 활발 기간 누적 {cumul_eok:,.0f}억 매도 ({breakdown['cumul']:.1f})")
    # RSI 14일 — 과매수/과매도 시그널
    rsi = breakdown.get("rsi")
    rsi_score = breakdown.get("rsi_score", 0)
    if rsi is not None:
        if rsi >= 80:
            reasons.append(f"⚠ RSI {rsi:.1f} — 강한 과매수 구간 ({rsi_score:+.1f})")
        elif rsi >= 70:
            reasons.append(f"⚠ RSI {rsi:.1f} — 과매수 ({rsi_score:+.1f})")
        elif rsi <= 20:
            reasons.append(f"💎 RSI {rsi:.1f} — 강한 과매도 (반등 기회) (+{rsi_score:.1f})")
        elif rsi <= 30:
            reasons.append(f"💎 RSI {rsi:.1f} — 과매도 (+{rsi_score:.1f})")
    if not reasons:
        reasons.append("의미 있는 시그널 없음 (중립 상태)")
    if total >= 50:
        recommend, level = "🟢 강한 매수 시그널 — 다중 호재. 분할 매수 권장", "strong-buy"
    elif total >= 20:
        recommend, level = "🟢 매수 우세 — 긍정 시그널 다수", "buy"
    elif total >= 5:
        recommend, level = "🟡 약한 매수 — 추가 확인 후 결정", "weak-buy"
    elif total >= -5:
        recommend, level = "⚪ 중립 — 명확한 방향성 없음", "neutral"
    elif total >= -20:
        recommend, level = "🔴 매도 우세 — 부정 시그널", "sell"
    elif total >= -50:
        recommend, level = "🔴 강한 매도 — 매도세 압력", "strong-sell"
    else:
        recommend, level = "🔴 매우 강한 매도 — 분명한 하방 시그널", "very-sell"
    return {"reasons": reasons, "recommend": recommend, "level": level}


def _ai_score(r: dict) -> float:
    """
    종합 점수 (가중 합산, 9개 변수):

    매매 강도 (큰 영향):
    - 시총비 (%)              × 100
    - 시총비 vs 전일거래대금  × 5

    모멘텀:
    - 등락률 (%)              × 1.5
    - 거래량 강도 (100 기준)  × 0.05

    매수 누적 / 지속성:
    - 활발 기간 (일)          × 4
    - 누적 순매수 (억)        × 0.005
    - 전일 대비 순매수 증가(억) × 0.02

    기술 지표:
    - RSI 14일 — 과매수(≥70) 페널티 / 과매도(≤30) 보너스, 가중치 0.8

    음수 페널티:
    - 연속 매도일수           × -8

    공시:
    - DART 호재/악재 키워드

    return: float — 양수=매수 강세, 음수=매도 강세
    """
    return _ai_score_breakdown(r)["total"]


def _ai_score_label(score: float) -> str:
    """점수 → 등급 라벨."""
    if score >= 50:
        return f'<span class="score-grade s-aplus">★★★ {score:+.1f}</span>'
    if score >= 20:
        return f'<span class="score-grade s-a">★★ {score:+.1f}</span>'
    if score >= 5:
        return f'<span class="score-grade s-b">★ {score:+.1f}</span>'
    if score <= -50:
        return f'<span class="score-grade s-fminus">▼▼▼ {score:+.1f}</span>'
    if score <= -20:
        return f'<span class="score-grade s-f">▼▼ {score:+.1f}</span>'
    if score <= -5:
        return f'<span class="score-grade s-d">▼ {score:+.1f}</span>'
    return f'<span class="score-grade s-c">{score:+.1f}</span>'


def _toss_order_url(code: str) -> str:
    """토스증권 주문 페이지 (매수/매도 둘 다 한 페이지에서 처리)."""
    return f"https://tossinvest.com/stocks/A{code}/order"

# 매수/매도 둘 다 같은 페이지지만 의미 구분 위해 별도 함수 유지
def _toss_buy_url(code: str) -> str:
    return _toss_order_url(code)

def _toss_sell_url(code: str) -> str:
    return _toss_order_url(code)


def render_html(payload: dict, mode: str = "realtime") -> str:
    """
    단일 HTML 페이지 생성.
    mode: 'realtime' 또는 'closing' — 헤더/네비게이션 표시 다름.
    """
    trade_date = payload["trade_date"]
    rows = payload["rows"]
    summaries = payload["summaries"]
    has_data = len(rows) > 0
    no_data_banner = "" if has_data else f"""
<div class="banner-warn">
  <h2>⚠ 데이터 없음 — CSV 다운로드 필요</h2>
  <p><b>기준일자 {trade_date} 의 CSV 파일을 못 찾았습니다.</b></p>
  <ol>
    <li><a href="http://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201020403" target="_blank">
        KRX 투자자별 순매수상위종목 페이지</a> 열기</li>
    <li>필터: 시장=유가증권, 투자자=<b>연기금</b>, 거래대금/순매수</li>
    <li>조회 → CSV 다운로드</li>
    <li>파일을 <code>report\\input\\krx_{trade_date}_KOSPI.csv</code> 로 저장</li>
    <li><code>build_report.bat {trade_date}</code> 재실행</li>
  </ol>
</div>
"""

    # 시장 총합
    kospi_sum = summaries.get("KOSPI", {}).get("연기금", {})
    kosdaq_sum = summaries.get("KOSDAQ", {}).get("연기금", {})
    total_buy = (kospi_sum.get("buy", 0) + kosdaq_sum.get("buy", 0))
    total_sell = (kospi_sum.get("sell", 0) + kosdaq_sum.get("sell", 0))
    total_net = (kospi_sum.get("net", 0) + kosdaq_sum.get("net", 0))

    # Top 매수/매도
    top_buy_20 = _table_top_rows(rows, "net_amount", desc=True, n=20)
    top_sell_20 = _table_top_rows(rows, "net_amount", desc=False, n=20)
    top_buy_50 = _table_top_rows(rows, "net_amount", desc=True, n=50)
    top_sell_50 = _table_top_rows(rows, "net_amount", desc=False, n=50)
    top_cap_buy = _table_top_rows(rows, "net_to_cap", desc=True, n=30)
    top_cap_sell = _table_top_rows(rows, "net_to_cap", desc=False, n=30)
    # 호환용 — 기존 변수명 (Top 5 카드 등에서 사용)
    top_buy = top_buy_20
    top_sell = top_sell_20

    # 7거래일 추이 + 주간 누적 + 전체 누적
    recent = query_recent_summaries(7, "KOSPI")
    weekly_buy = query_weekly_top(days=7, top_n=30, direction="buy")
    weekly_sell = query_weekly_top(days=7, top_n=30, direction="sell")
    cumul_buy = query_cumulative_top(top_n=30, direction="buy")
    cumul_sell = query_cumulative_top(top_n=30, direction="sell")

    def _toss_btns(code):
        # 토스의 매수/매도는 동일 주문창 → 한 버튼으로 통합
        return (
            f"<a href='{_toss_order_url(code)}' target='_blank' class='btn-trade' title='토스증권 주문창'>매매</a>"
        )

    def _row_today(r, key="net_amount"):
        # 첫 컬럼은 항상 net_amount(순매수액) 표시. key 인자는 호출자가 정렬용으로만 사용.
        net = r.get("net_amount", 0)
        chg = r.get("change_rate", 0)
        chg_class = "pos" if chg > 0 else ("neg" if chg < 0 else "")
        score = _ai_score(r)
        consec = _consecutive_label(r)
        consec_cls = "consec-buy" if "연속매수" in consec else ("consec-sell" if "연속매도" in consec else "")
        consec_html = f"<span class='consec-badge {consec_cls}'>{consec}</span>" if consec else ""
        code = _esc(r['stock_code'])
        name = _esc(r['stock_name'])
        return (
            f"<tr class='stock-row' data-stock-code='{code}' data-stock-name='{name}'>"
            f"<td class='code'><span class='fav-star' data-stock='{code}' title='즐겨찾기'>☆</span>{code}</td>"
            f"<td class='name'>{name}{consec_html}</td>"
            f"<td class='num {'pos' if net >= 0 else 'neg'}' data-value='{net}'>{_fmt_won(net)}</td>"
            f"<td class='num' data-value='{r.get('buy_amount', 0)}'>{_fmt_won(r.get('buy_amount', 0))}</td>"
            f"<td class='num' data-value='{r.get('sell_amount', 0)}'>{_fmt_won(r.get('sell_amount', 0))}</td>"
            f"<td class='num {chg_class}' data-value='{chg}'>{_fmt_pct(chg)}</td>"
            f"<td class='num' data-value='{r.get('close_price', 0)}'>{r.get('close_price', 0):,}</td>"
            f"<td class='num' data-value='{r.get('net_to_cap', 0)}'>{_fmt_pct(r.get('net_to_cap', 0))}</td>"
            f"<td class='num' data-value='{score}'>{_ai_score_label(score)}</td>"
            f"<td class='market'>{_esc(r.get('market', ''))}</td>"
            f"<td class='actions'>{_toss_btns(r['stock_code'])}</td>"
            f"</tr>"
        )

    def _row_weekly(r):
        code = _esc(r['stock_code'])
        name = _esc(r['stock_name'])
        return (
            f"<tr class='stock-row' data-stock-code='{code}' data-stock-name='{name}'>"
            f"<td class='code'><span class='fav-star' data-stock='{code}' title='즐겨찾기'>☆</span>{code}</td>"
            f"<td class='name'>{name}</td>"
            f"<td class='num {'pos' if r['net_sum'] >= 0 else 'neg'}' data-value='{r['net_sum']}'>{_fmt_won(r['net_sum'])}</td>"
            f"<td class='num' data-value='{r['buy_sum']}'>{_fmt_won(r['buy_sum'])}</td>"
            f"<td class='num' data-value='{r['sell_sum']}'>{_fmt_won(r['sell_sum'])}</td>"
            f"<td class='num' data-value='{r['day_count']}'>{r['day_count']}일</td>"
            f"<td class='market'>{_esc(r.get('market', ''))}</td>"
            f"<td class='actions'>{_toss_btns(r['stock_code'])}</td>"
            f"</tr>"
        )

    empty_today = "<tr><td colspan='11' class='empty'>데이터 없음</td></tr>"
    empty_weekly = "<tr><td colspan='8' class='empty'>히스토리 누적 중 (며칠 빌드 후 표시)</td></tr>"

    top_buy_html = "\n".join(_row_today(r) for r in top_buy_20) or empty_today
    top_sell_html = "\n".join(_row_today(r) for r in top_sell_20) or empty_today
    top_buy50_html = "\n".join(_row_today(r) for r in top_buy_50) or empty_today
    top_sell50_html = "\n".join(_row_today(r) for r in top_sell_50) or empty_today
    top_cap_buy_html = "\n".join(_row_today(r, "net_to_cap") for r in top_cap_buy) or empty_today
    top_cap_sell_html = "\n".join(_row_today(r, "net_to_cap") for r in top_cap_sell) or empty_today
    weekly_buy_html = "\n".join(_row_weekly(r) for r in weekly_buy) or empty_weekly
    weekly_sell_html = "\n".join(_row_weekly(r) for r in weekly_sell) or empty_weekly
    cumul_buy_html = "\n".join(_row_weekly(r) for r in cumul_buy) or empty_weekly
    cumul_sell_html = "\n".join(_row_weekly(r) for r in cumul_sell) or empty_weekly

    # AI 점수 페이지용 — 전 종목 점수 + 분해
    ai_rows_sorted = sorted(rows, key=_ai_score, reverse=True)
    def _row_ai(r):
        # 좌측 표는 핵심 컬럼만 (8개). 변수 기여도는 우측 카드 그리드로 분리.
        bd = _ai_score_breakdown(r)
        total = bd["total"]
        chg = r.get("change_rate", 0)
        code = _esc(r['stock_code'])
        name = _esc(r['stock_name'])
        rsi = bd.get("rsi")
        if rsi is None:
            rsi_cell = "<td class='num' style='color:#bdc3c7;'>-</td>"
        else:
            rsi_cls = "neg" if rsi >= 70 else ("pos" if rsi <= 30 else "")
            rsi_cell = f"<td class='num {rsi_cls}' data-value='{rsi}'>{rsi:.1f}</td>"
        return (
            f"<tr class='stock-row' data-stock-code='{code}' data-stock-name='{name}'>"
            f"<td class='code'><span class='fav-star' data-stock='{code}' title='즐겨찾기'>☆</span>{code}</td>"
            f"<td class='name'>{name}</td>"
            f"<td class='num' data-value='{total}'>{_ai_score_label(total)}</td>"
            f"{rsi_cell}"
            f"<td class='num' data-value='{r.get('net_amount',0)}'>{_fmt_won(r.get('net_amount',0))}</td>"
            f"<td class='num {'pos' if chg>0 else ('neg' if chg<0 else '')}' data-value='{chg}'>{_fmt_pct(chg)}</td>"
            f"<td class='market'>{_esc(r.get('market',''))}</td>"
            f"<td class='actions'>{_toss_btns(r['stock_code'])}</td>"
            f"</tr>"
        )
    ai_html = "\n".join(_row_ai(r) for r in ai_rows_sorted) or "<tr><td colspan='8' class='empty'>데이터 없음</td></tr>"

    # AI 점수 상세 카드 (Top 매수 30 + Top 매도 10)
    def _ai_card(r):
        bd = _ai_score_breakdown(r)
        exp = _ai_score_explain(r, bd)
        total = bd["total"]
        close = r.get("close_price", 0)
        change = r.get("change_rate", 0)
        net = r.get("net_amount", 0)
        reasons_html = "".join(f"<li>{_esc(reason)}</li>" for reason in exp["reasons"])
        return (
            f"<div class='ai-card stock-row ai-{exp['level']}' data-stock-code='{_esc(r['stock_code'])}' data-stock-name='{_esc(r['stock_name'])}'>"
            f"  <div class='ai-card-head'>"
            f"    <div class='ai-card-name'>"
            f"      <span class='fav-star' data-stock='{_esc(r['stock_code'])}' title='즐겨찾기'>☆</span>"
            f"      {_esc(r['stock_name'])} "
            f"      <span class='ai-card-code'>{_esc(r['stock_code'])} · {_esc(r.get('market',''))}</span></div>"
            f"    <div class='ai-card-score'>{_ai_score_label(total)}</div>"
            f"  </div>"
            f"  <div class='ai-card-meta'>"
            f"    현재가 {close:,} · 등락률 {_fmt_pct(change)} · 순매수 {_fmt_won(net)}"
            f"  </div>"
            f"  <div class='ai-card-recommend'>{_esc(exp['recommend'])}</div>"
            f"  <div class='ai-card-reasons'><b>점수 산출 근거</b><ul>{reasons_html}</ul></div>"
            f"  <div class='ai-card-actions'>"
            f"    <a href='{_toss_buy_url(r['stock_code'])}' target='_blank' class='btn-buy-big'>토스 매수창</a>"
            f"    <a href='{_toss_sell_url(r['stock_code'])}' target='_blank' class='btn-sell-big'>토스 매도창</a>"
            f"  </div>"
            f"</div>"
        )

    # 매수 시그널 Top 20 + 매도 시그널 Top 10 (좌우 분할 UX 로 변경되어 본문엔 사용 안 함, 변수만 유지)
    ai_buy_cards = [r for r in ai_rows_sorted if _ai_score(r) > 0][:20]
    ai_sell_cards = sorted([r for r in ai_rows_sorted if _ai_score(r) < 0], key=_ai_score)[:10]
    ai_buy_cards_html = ""   # 사용 안 함 (좌우 분할 UX)
    ai_sell_cards_html = ""

    # AI 페이지 좌우 분할용 — 모든 종목의 카드 데이터 (JSON inject, JS 가 행 클릭시 우측에 렌더)
    if mode == "ai":
        ai_cards_data = {}
        for r in ai_rows_sorted:
            bd = _ai_score_breakdown(r)
            exp = _ai_score_explain(r, bd)
            ai_cards_data[r['stock_code']] = {
                "code": r['stock_code'],
                "name": r['stock_name'],
                "market": r.get('market', ''),
                "score": bd["total"],
                "level": exp["level"],
                "close": int(r.get('close_price', 0) or 0),
                "change_rate": r.get('change_rate', 0) or 0,
                "net_amount": r.get('net_amount', 0) or 0,
                "rsi": bd.get("rsi"),
                "recommend": exp["recommend"],
                "reasons": exp["reasons"],
                "breakdown": {
                    "매수비율": bd["cap"],
                    "전일거래": bd["val_ratio"],
                    "등락률": bd["change"],
                    "거래량": bd["strength"],
                    "활발일": bd["period"],
                    "누적": bd["cumul"],
                    "전일比": bd["delta"],
                    "매도페널티": bd["sell_penalty"],
                    "DART공시": bd["dart"],
                    "RSI점수": bd["rsi_score"],
                },
            }
        ai_cards_data_json = json.dumps(ai_cards_data, ensure_ascii=False)
    else:
        ai_cards_data_json = "{}"

    recent_html = "".join(
        f"<tr><td>{_esc(r['trade_date'])}</td>"
        f"<td class='num'>{_fmt_won(r['buy_total'])}</td>"
        f"<td class='num'>{_fmt_won(r['sell_total'])}</td>"
        f"<td class='num {'pos' if r['net_total'] >= 0 else 'neg'}'>{_fmt_won(r['net_total'])}</td></tr>"
        for r in recent
    ) or "<tr><td colspan='4' class='empty'>히스토리 없음 (오늘이 첫 실행)</td></tr>"

    # 차트용: 7거래일 시장 수급 데이터 (단위: 억원, 오래된 날짜→최신 순)
    chart_data = list(reversed([
        {
            "date": r["trade_date"][4:],   # MMDD 만 표시
            "buy": round((r["buy_total"] or 0) / 100_000_000, 1),
            "sell": round((r["sell_total"] or 0) / 100_000_000, 1),
            "net": round((r["net_total"] or 0) / 100_000_000, 1),
        }
        for r in recent
    ]))
    chart_data_json = json.dumps(chart_data, ensure_ascii=False)

    # =====================================================================
    # bulk 페이지: 5%룰 공시 + 10만주↑ 매매 (모드 == "bulk" 일 때만 데이터 채움)
    # =====================================================================
    if mode == "bulk":
        bulk_daily_rows = query_bulk_qty_daily(threshold=100_000, days=30, limit=200)
        bulk_cumul_rows = query_bulk_qty_cumulative(threshold=100_000, days=7, limit=100)
        meta_map = query_stock_meta_map()
    else:
        bulk_daily_rows = []
        bulk_cumul_rows = []
        meta_map = {}

    nps_holdings_map = payload.get("nps_holdings", {})       # {code: [items, ...]} 보고자=국민연금
    majorstock_all_map = payload.get("majorstock_all", {})   # {code: [items, ...]} 모든 보고자, traded 한정

    def _row_bulk_daily(r):
        qty = r.get('net_qty', 0) or 0
        amt = r.get('net_amount', 0) or 0
        kind = "매수" if qty > 0 else "매도"
        kind_cls = "pos" if qty > 0 else "neg"
        cap = r.get('market_cap', 0) or 0
        cap_pct = (amt / cap * 100) if cap > 0 else 0
        code = _esc(r['stock_code'])
        name = _esc(r['stock_name'])
        td = str(r.get('trade_date', ''))
        td_disp = f"{td[:4]}-{td[4:6]}-{td[6:8]}" if len(td) == 8 else td
        return (
            f"<tr class='stock-row' data-stock-code='{code}' data-stock-name='{name}'>"
            f"<td>{td_disp}</td>"
            f"<td class='code'><span class='fav-star' data-stock='{code}'>☆</span>{code}</td>"
            f"<td class='name'>{name}</td>"
            f"<td class='{kind_cls}'><b>{kind}</b></td>"
            f"<td class='num {kind_cls}' data-value='{qty}'>{abs(qty):,}주</td>"
            f"<td class='num {kind_cls}' data-value='{amt}'>{_fmt_won(abs(amt))}</td>"
            f"<td class='num' data-value='{cap_pct}'>{cap_pct:.3f}%</td>"
            f"<td class='market'>{_esc(r.get('market', ''))}</td>"
            f"<td class='actions'>{_toss_btns(r['stock_code'])}</td>"
            f"</tr>"
        )

    def _row_bulk_cumul(r):
        qty = r.get('net_qty_sum', 0) or 0
        amt = r.get('net_amount_sum', 0) or 0
        kind = "매수" if qty > 0 else "매도"
        kind_cls = "pos" if qty > 0 else "neg"
        code = _esc(r['stock_code'])
        name = _esc(r['stock_name'])
        return (
            f"<tr class='stock-row' data-stock-code='{code}' data-stock-name='{name}'>"
            f"<td class='code'><span class='fav-star' data-stock='{code}'>☆</span>{code}</td>"
            f"<td class='name'>{name}</td>"
            f"<td class='{kind_cls}'><b>{kind}</b></td>"
            f"<td class='num {kind_cls}' data-value='{qty}'>{abs(qty):,}주</td>"
            f"<td class='num {kind_cls}' data-value='{amt}'>{_fmt_won(abs(amt))}</td>"
            f"<td class='num' data-value='{r.get('day_count', 0)}'>{r.get('day_count', 0)}일</td>"
            f"<td class='market'>{_esc(r.get('market', ''))}</td>"
            f"<td class='actions'>{_toss_btns(r['stock_code'])}</td>"
            f"</tr>"
        )

    def _row_majorstock(code, item):
        rcept_dt = item.get('rcept_dt', '')
        rcept_disp = f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]}" if len(rcept_dt) == 8 else rcept_dt
        rate = item.get('stkrt', 0) or 0
        rate_irds = item.get('stkrt_irds', 0) or 0
        qty = item.get('stkqy', 0) or 0
        qty_irds = item.get('stkqy_irds', 0) or 0
        rt_tp = item.get('report_tp', '')
        repror = item.get('repror', '')
        meta_name = meta_map.get(code, {}).get('name') or item.get('corp_name') or code
        name = _esc(meta_name)
        code_esc = _esc(code)
        # 신규/변동/보유 색상
        tp_cls = ""
        if "신규" in rt_tp:
            tp_cls = "pos"
        elif "변동" in rt_tp and rate_irds > 0:
            tp_cls = "pos"
        elif "변동" in rt_tp and rate_irds < 0:
            tp_cls = "neg"
        irds_html = ""
        if rate_irds:
            cls = "pos" if rate_irds > 0 else "neg"
            irds_html = f" <span class='{cls}' style='font-size:11px;'>({rate_irds:+.2f}%p)</span>"
        qty_irds_html = ""
        if qty_irds:
            cls = "pos" if qty_irds > 0 else "neg"
            qty_irds_html = f" <span class='{cls}' style='font-size:10px;'>({qty_irds:+,})</span>"
        return (
            f"<tr class='stock-row' data-stock-code='{code_esc}' data-stock-name='{name}'>"
            f"<td>{rcept_disp}</td>"
            f"<td class='code'><span class='fav-star' data-stock='{code_esc}'>☆</span>{code_esc}</td>"
            f"<td class='name'>{name}</td>"
            f"<td class='{tp_cls}'>{_esc(rt_tp)}</td>"
            f"<td>{_esc(repror)}</td>"
            f"<td class='num' data-value='{qty}'>{qty:,}주{qty_irds_html}</td>"
            f"<td class='num' data-value='{rate}'>{rate:.2f}%{irds_html}</td>"
            f"<td class='actions'>{_toss_btns(code)}</td>"
            f"</tr>"
        )

    bulk_daily_html = "\n".join(_row_bulk_daily(r) for r in bulk_daily_rows) \
        or "<tr><td colspan='9' class='empty'>최근 30일 10만주↑ 매매 없음</td></tr>"
    bulk_cumul_html = "\n".join(_row_bulk_cumul(r) for r in bulk_cumul_rows) \
        or "<tr><td colspan='8' class='empty'>최근 7거래일 10만주↑ 누적 매매 없음</td></tr>"

    # nps_holdings_map → 모든 (code, item) flatten + 보고일 desc 정렬
    nps_pairs = [(c, it) for c, items in nps_holdings_map.items() for it in items]
    nps_pairs.sort(key=lambda x: x[1].get('rcept_dt', ''), reverse=True)
    nps_html_rows = "\n".join(_row_majorstock(c, it) for c, it in nps_pairs[:300]) \
        or "<tr><td colspan='8' class='empty'>국민연금공단 5%룰 공시 없음 (DB 매매 종목 한정)</td></tr>"

    all_pairs = [(c, it) for c, items in majorstock_all_map.items() for it in items]
    all_pairs.sort(key=lambda x: x[1].get('rcept_dt', ''), reverse=True)
    all_html_rows = "\n".join(_row_majorstock(c, it) for c, it in all_pairs[:500]) \
        or "<tr><td colspan='8' class='empty'>5%룰 공시 없음 (DB 매매 종목 한정)</td></tr>"

    # Top 5 카드 (압축 — 현재가/등락률/순매수액 정보 포함)
    def _top5_card(r, kind="buy"):
        code = r['stock_code']
        net = r.get('net_amount', 0)
        close = r.get('close_price', 0)
        base = r.get('base_price', 0)
        change_amt = close - base if base > 0 else 0
        chg_pct = r.get('change_rate', 0)
        amount_label = "순매수" if kind == "buy" else "순매도"
        amount_val = abs(net)
        amount_cls = "pos" if kind == "buy" else "neg"
        chg_cls = "pos" if change_amt > 0 else ("neg" if change_amt < 0 else "neutral")
        arrow = "▲" if change_amt > 0 else ("▼" if change_amt < 0 else "—")
        action_label = "매수" if kind == "buy" else "매도"
        action_url = _toss_buy_url(code) if kind == "buy" else _toss_sell_url(code)
        action_cls = "btn-buy-big" if kind == "buy" else "btn-sell-big"

        price_html = (
            f"<div class='top5-price-line'>"
            f"<span class='top5-price'>{close:,}</span> "
            f"<span class='top5-change {chg_cls}'>{arrow}{abs(change_amt):,} ({chg_pct:+.2f}%)</span>"
            f"</div>"
        ) if close > 0 else "<div class='top5-price-na'>현재가 N/A</div>"

        # 연속 매수/매도 배지 + AI 점수
        consec = _consecutive_label(r)
        consec_cls = "consec-buy" if "연속매수" in consec else ("consec-sell" if "연속매도" in consec else "")
        consec_html = f"<span class='consec-badge {consec_cls}'>{consec}</span>" if consec else ""
        score = _ai_score(r)
        score_html = f"<span class='top5-score-inline'>AI {_ai_score_label(score)}</span>"
        return (
            f"<div class='top5-card stock-row' data-stock-code='{_esc(code)}' data-stock-name='{_esc(r['stock_name'])}'>"
            f"<span class='fav-star fav-star-top5' data-stock='{_esc(code)}' title='즐겨찾기'>☆</span>"
            f"<div class='top5-rank'>{r.get('_rank', '')}</div>"
            f"<div class='top5-head'>"
            f"  <div class='top5-name'>{_esc(r['stock_name'])}</div>"
            f"  <div class='top5-code'>{_esc(code)} · {_esc(r.get('market', ''))}</div>"
            f"</div>"
            f"{price_html}"
            f"<div class='top5-amount {amount_cls}'>{amount_label} {_fmt_won(amount_val)}</div>"
            f"<div class='top5-meta'>{consec_html}{score_html}</div>"
            f"<a href='{action_url}' target='_blank' class='{action_cls}'>토스 {action_label}창</a>"
            f"</div>"
        )

    top5_buy = top_buy[:5]
    top5_sell = top_sell[:5]
    for i, r in enumerate(top5_buy, 1):
        r['_rank'] = i
    for i, r in enumerate(top5_sell, 1):
        r['_rank'] = i
    top5_buy_html = "".join(_top5_card(r, "buy") for r in top5_buy) or "<div class='empty'>데이터 없음</div>"
    top5_sell_html = "".join(_top5_card(r, "sell") for r in top5_sell) or "<div class='empty'>데이터 없음</div>"

    now = dt.datetime.now()
    generated_at = now.strftime("%Y-%m-%d %H:%M:%S")
    mode_active_rt = "active" if mode == "realtime" else ""
    mode_active_cl = "active" if mode == "closing" else ""
    mode_active_ai = "active" if mode == "ai" else ""
    mode_active_bulk = "active" if mode == "bulk" else ""
    mode_active_themes = "active" if mode == "themes" else ""
    mode_active_cont = "active" if mode == "continuity" else ""
    auto_refresh_meta = '<meta http-equiv="refresh" content="3600">' if mode == "realtime" else ''

    # 테마 페이지 데이터 (mode=='themes' 일 때만 큰 페이로드 생성, 3개 기간)
    if mode == "themes":
        today_data = build_theme_data(rows)
        yesterday_aggs = query_sector_aggregates(days_offset=1, days_count=1)
        yesterday_stocks = query_sector_stocks_for_period(days_offset=1, days_count=1)
        week_aggs = query_sector_aggregates(days_offset=0, days_count=7)
        week_stocks = query_sector_stocks_for_period(days_offset=0, days_count=7)
        theme_data_multi = {
            "today":     today_data,
            "yesterday": {"sectors": yesterday_aggs, "stocks_by_sector": yesterday_stocks},
            "week":      {"sectors": week_aggs,      "stocks_by_sector": week_stocks},
        }
        theme_data_json = json.dumps(theme_data_multi, ensure_ascii=False)
    else:
        theme_data_json = "{}"
    # 연속 누적 페이지 데이터 (mode=='continuity' 일 때만, DB 시계열 기반)
    continuity_items = build_continuity_data(min_days=2) if mode == "continuity" else []
    continuity_data_json = json.dumps(continuity_items, ensure_ascii=False)

    # 마지막 업데이트 + 다음 예정 시각 (실시간 모드)
    next_update_at = (now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)).strftime("%H:%M")
    last_update_hhmm = now.strftime("%H:%M")
    today_str = now.strftime("%Y%m%d")

    # 모드별 부제 + 데이터 신선도 안내
    if mode == "realtime":
        intraday_ts = payload.get("intraday_updated_at", "")
        # ISO 형식 "2026-05-22T11:08:04.000+09:00" → "11:08" 추출
        intraday_hhmm = ""
        if intraday_ts:
            try:
                intraday_hhmm = intraday_ts[11:16]   # "11:08"
            except Exception:
                intraday_hhmm = ""
        # 다음 5분 단위 시각 계산
        cur_min = now.minute
        next_min = ((cur_min // 5) + 1) * 5
        if next_min >= 60:
            next_update_at = (now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)).strftime("%H:%M")
        else:
            next_update_at = now.replace(minute=next_min, second=0, microsecond=0).strftime("%H:%M")
        if intraday_hhmm:
            mode_subtitle = (
                f"⏱ 실시간 — 토스 trading-trend <b>{intraday_hhmm}</b> KST 기준 "
                f"· 페이지 빌드: {last_update_hhmm} "
                f"· 다음 갱신 예정: {next_update_at} "
                f"· 업데이트 주기: 5분"
            )
        else:
            mode_subtitle = (
                f"⏱ 실시간 — 마지막 업데이트: <b>{last_update_hhmm}</b> "
                f"· 다음 갱신 예정: {next_update_at} "
                f"· 업데이트 주기: 5분"
            )
        data_freshness_note = ""
    elif mode == "ai":
        mode_subtitle = (
            f"🤖 AI 점수 — 종목별 점수 + 변수별 기여도 분해 "
            f"· 마지막 빌드: <b>{last_update_hhmm}</b>"
        )
        data_freshness_note = (
            '<div class="freshness-note">'
            '📊 AI 점수는 <b>매수비율 ×100 + 전일거래비율 ×5 + 등락률 ×1.5 + '
            '거래량강도 ×0.05 + 활발기간 ×4 + 누적 ×0.005 + 전일대비 ×0.02 '
            '+ 연속매도 ×−8 + DART공시</b> 의 가중합입니다. '
            '머신러닝 아닌 휴리스틱이며 투자 판단 보조 지표일 뿐입니다.'
            '</div>'
        )
    elif mode == "bulk":
        mode_subtitle = (
            f"📦 대량매매 — 연기금 매매 종목 중 5%룰 공시 + 10만주↑ 매매 추적 "
            f"· 마지막 빌드: <b>{last_update_hhmm}</b>"
        )
        data_freshness_note = (
            '<div class="freshness-note">'
            '📌 5%룰 공시: DART OpenAPI 대량보유상황보고서 (보고일 = DART 접수일). '
            '캐시 TTL 12시간. 10만주↑ 매매: 우리 DB의 연기금 일일/누적 net_qty 기준.'
            '</div>'
        )
    elif mode == "themes":
        mode_subtitle = (
            f"🏷 테마/업종별 수급 — 연기금 오늘 매매 종목을 sector 로 묶어 집계 "
            f"· 마지막 빌드: <b>{last_update_hhmm}</b>"
        )
        data_freshness_note = (
            '<div class="freshness-note">'
            '📊 sector 분류는 todayygg 응답의 업종 분류 기반. 차트의 막대를 클릭하거나 '
            '우측 표의 행을 클릭하면 하단에 해당 테마 구성 종목이 나타납니다.'
            '</div>'
        )
    elif mode == "continuity":
        mode_subtitle = (
            f"🔥 연속 매수 종목 — 우리 DB 시계열에서 net_amount&gt;0 인 연속 거래일 ≥2일 "
            f"· 마지막 빌드: <b>{last_update_hhmm}</b>"
        )
        data_freshness_note = (
            '<div class="freshness-note">'
            '📌 <b>🔥 불타기</b> = 오늘 매수평단이 어제까지 평균보다 비쌈 (단가↑ 추격 매수) · '
            '<b>💧 물타기</b> = 오늘 매수평단이 더 쌈 (단가↓ 추가 매수) · '
            '<b>━ 보합</b> · <b>❓ 첫날 (이전 데이터 없음)</b>. '
            '연속 일수 = 우리 DB의 영업일 카운트. 빌드 누적 며칠 이내엔 표시 종목 적음 (정상).'
            '</div>'
        )
    else:  # closing
        # 데이터의 trade_date가 오늘이면 ✅, 아니면 직전 영업일 데이터 안내
        td = str(trade_date)
        if td == today_str:
            mode_subtitle = f"📊 마감 기준 — <b>오늘({td}) 15:30 마감 데이터</b>"
            data_freshness_note = ""
        elif td and td != "—":
            mode_subtitle = f"📊 마감 기준 — <b>{td}</b> 마감 데이터"
            data_freshness_note = (
                f'<div class="freshness-note">📌 오늘 마감 데이터는 16시 이후 자동 갱신됩니다. '
                f'현재는 {td} 기준 데이터.</div>'
            )
        else:
            mode_subtitle = "📊 마감 기준 — 데이터 준비 중"
            data_freshness_note = ""

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>연기금 매매 리포트 — {trade_date} ({mode})</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- 실시간 모드만 1시간 자동 새로고침 -->
{auto_refresh_meta}
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
    margin: 0 auto; padding: 12px 16px; max-width: 1500px;
    background: #f5f7fa; color: #2c3e50; line-height: 1.4;
  }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  h2 {{ font-size: 14px; margin: 16px 0 6px; padding-bottom: 3px; border-bottom: 2px solid #3498db; }}
  .meta {{ color: #7f8c8d; font-size: 11px; margin-bottom: 12px; }}
  .banner-warn {{ background:#fef3cd; border:2px solid #f0ad4e; border-radius:6px; padding:12px; margin-bottom:12px; }}
  .banner-warn h2 {{ margin-top:0; color:#8a6d3b; border:0; }}
  .banner-warn ol {{ line-height:1.6; }}
  .summary {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }}
  .card {{ background: white; border: 1px solid #e1e8ed; border-radius: 6px; padding: 8px 12px; flex: 1; min-width: 130px; }}
  .card .label {{ font-size: 10px; color: #95a5a6; text-transform: uppercase; }}
  .card .value {{ font-size: 18px; font-weight: 600; margin-top: 2px; }}

  /* 헤더 레이아웃: 좌측 카드들 / 우측 7거래일 표 */
  .layout-header {{ display: grid; grid-template-columns: 1fr 280px; gap: 14px; align-items: start; margin-bottom: 8px; }}
  @media (max-width: 1100px) {{ .layout-header {{ grid-template-columns: 1fr; }} }}
  .layout-right table {{ font-size: 11px; }}
  .layout-right th, .layout-right td {{ padding: 4px 6px; }}
  .layout-right h2 {{ margin-top: 0; }}
  .pos {{ color: #27ae60; }}
  .neg {{ color: #c0392b; }}
  .empty {{ text-align:center; color:#95a5a6; padding:12px !important; }}

  /* Top 5 카드 — 압축형 */
  .top5-wrap {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 6px; margin-bottom: 4px; }}
  @media (max-width: 1100px) {{ .top5-wrap {{ grid-template-columns: repeat(3, 1fr); }} }}
  @media (max-width: 700px)  {{ .top5-wrap {{ grid-template-columns: repeat(2, 1fr); }} }}
  .top5-card {{
    background: white; border-radius: 5px; padding: 6px 9px;
    border-top: 3px solid #3498db; box-shadow: 0 1px 2px rgba(0,0,0,0.05); position: relative;
  }}
  .top5-card .top5-rank {{
    position: absolute; top: -5px; right: 6px; background: #3498db; color: white;
    font-size: 9px; font-weight: 700; padding: 1px 5px; border-radius: 7px;
  }}
  .top5-head {{ margin-bottom: 3px; }}
  .top5-card .top5-name {{ font-size: 12px; font-weight: 600; line-height: 1.2; }}
  .top5-card .top5-code {{ font-size: 9px; color: #95a5a6; font-family: Consolas, monospace; margin-top: 1px; }}
  .top5-card .top5-price {{ font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .top5-card .top5-change {{ font-size: 10px; font-weight: 500; font-variant-numeric: tabular-nums; }}
  .top5-card .top5-change.pos {{ color: #c0392b; }}    /* 한국 관습: 상승=빨강 */
  .top5-card .top5-change.neg {{ color: #2980b9; }}    /* 하락=파랑 */
  .top5-card .top5-change.neutral {{ color: #7f8c8d; }}
  .top5-card .top5-price-na {{ font-size: 10px; color: #95a5a6; font-style: italic; }}
  .top5-card .top5-amount {{ font-size: 13px; font-weight: 700; margin: 4px 0 3px; }}
  .top5-meta {{ display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 5px; align-items: center; }}
  .top5-score-inline {{ font-size: 9px; color: #7f8c8d; }}
  .btn-buy-big, .btn-sell-big {{
    display: block; text-align: center; padding: 4px;
    border-radius: 3px; text-decoration: none; font-size: 10px; font-weight: 600;
  }}
  .btn-buy-big {{ background: #e74c3c; color: white; }}
  .btn-buy-big:hover {{ background: #c0392b; }}
  .btn-sell-big {{ background: #2980b9; color: white; }}
  .btn-sell-big:hover {{ background: #2471a3; }}

  /* 일반 표 */
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 6px; overflow: hidden; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }}
  th, td {{ padding: 5px 7px; font-size: 11px; border-bottom: 1px solid #ecf0f1; white-space: nowrap; }}
  th {{ background: #34495e; color: white; text-align: left; font-weight: 500; font-size: 10px; }}
  td.name {{ overflow: hidden; text-overflow: ellipsis; max-width: 120px; }}
  table.sortable th[data-sort] {{ cursor: pointer; user-select: none; }}
  table.sortable th[data-sort]:hover {{ background: #2c3e50; }}
  table.sortable th[data-sort]::after {{ content: " ⇅"; opacity: 0.4; font-size: 9px; }}
  table.sortable th[data-dir="asc"]::after {{ content: " ▲"; opacity: 1; }}
  table.sortable th[data-dir="desc"]::after {{ content: " ▼"; opacity: 1; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  /* num 컬럼 헤더도 우측 정렬 통일 */
  table th.num {{ text-align: right; }}
  td.code {{ font-family: Consolas, monospace; color: #7f8c8d; }}
  td.name {{ font-weight: 500; }}
  td.market {{ color: #95a5a6; font-size: 11px; }}
  td.actions {{ white-space: nowrap; text-align: center; }}
  td.actions a {{ display:inline-block; padding:4px 12px; margin:0 2px; font-size:11px; border-radius:3px; text-decoration:none; font-weight:600; }}
  .btn-buy {{ background:#e74c3c; color:white; }}
  .btn-buy:hover {{ background:#c0392b; }}
  .btn-sell {{ background:#2980b9; color:white; }}
  .btn-sell:hover {{ background:#2471a3; }}
  /* 통합 매매 버튼 */
  .btn-trade {{ background:#34495e; color:white; }}
  .btn-trade:hover {{ background:#2c3e50; }}
  tr:hover td {{ background: #f8f9fa; }}

  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  @media (max-width: 1000px) {{ .grid2 {{ grid-template-columns: 1fr; }} }}
  .filter-hint {{ font-size: 11px; color: #95a5a6; margin: 4px 0 8px; }}
  .footer {{ text-align: center; color: #95a5a6; font-size: 11px; margin-top: 24px; padding: 16px; }}

  /* AI 점수 페이지 */
  .ai-page {{ margin-top: 8px; }}
  .ai-page table {{ font-size: 10px; }}
  .ai-page th, .ai-page td {{ padding: 4px 5px; font-size: 10px; }}
  .ai-table th[title] {{ cursor: help; }}

  /* AI 상세 카드 */
  .ai-cards-wrap {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }}
  @media (max-width: 900px) {{ .ai-cards-wrap {{ grid-template-columns: 1fr; }} }}
  .ai-card {{
    background: white; border-radius: 8px; padding: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    border-left: 5px solid #95a5a6;
  }}
  .ai-card.ai-strong-buy {{ border-left-color: #c0392b; }}
  .ai-card.ai-buy        {{ border-left-color: #e74c3c; }}
  .ai-card.ai-weak-buy   {{ border-left-color: #f39c12; }}
  .ai-card.ai-neutral    {{ border-left-color: #95a5a6; }}
  .ai-card.ai-sell       {{ border-left-color: #3498db; }}
  .ai-card.ai-strong-sell{{ border-left-color: #2980b9; }}
  .ai-card.ai-very-sell  {{ border-left-color: #2471a3; }}
  .ai-card-head {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px; }}
  .ai-card-name {{ font-size: 15px; font-weight: 700; }}
  .ai-card-code {{ font-size: 11px; color: #95a5a6; font-family: Consolas, monospace; font-weight: 400; }}
  .ai-card-score {{ font-size: 13px; }}
  .ai-card-meta {{ font-size: 11px; color: #7f8c8d; margin-bottom: 6px; font-variant-numeric: tabular-nums; }}
  .ai-card-recommend {{
    background: #f8f9fa; padding: 8px 10px; border-radius: 4px;
    font-size: 12px; font-weight: 600; margin-bottom: 8px;
  }}
  .ai-card-reasons {{ font-size: 11px; color: #2c3e50; }}
  .ai-card-reasons ul {{ margin: 4px 0 0; padding-left: 18px; }}
  .ai-card-reasons li {{ margin-bottom: 2px; line-height: 1.4; }}
  .ai-card-actions {{ display: flex; gap: 6px; margin-top: 8px; }}
  .ai-card-actions a {{ flex: 1; font-size: 11px; padding: 5px; }}
  .ai-footnote {{
    margin-top: 24px; padding: 12px; background: #fef9e7;
    border: 1px solid #f1c40f; border-radius: 4px;
    font-size: 11px; color: #7f6e1f;
  }}

  /* PDF 다운로드 버튼 */
  .pdf-btn {{
    background: #16a085; color: white; border: 0; padding: 6px 12px;
    border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600;
    margin-left: 8px;
  }}
  .pdf-btn:hover {{ background: #138d75; }}

  /* 인쇄(PDF 저장) 전용 — 버튼/토글/댓글 등 제외, 표 그대로 */
  @media print {{
    body {{ padding: 8px; max-width: none; background: white; color: black; }}
    .nav-tabs, .layer-toggles, .pdf-btn, .comments-section, .search-bar,
    .bulk-sub-tabs, .bulk-main-tabs,
    .footer, td.actions, th:last-child {{ display: none !important; }}
    /* bulk PDF 인쇄시 모든 탭 컨텐츠 표시 */
    .bulk-tab-content, .bulk-main-content {{ display: block !important; }}
    .fav-star {{ display: none !important; }}
    table.sortable td.actions {{ display: none !important; }}
    .layout-header {{ grid-template-columns: 1fr; }}
    h1 {{ font-size: 16px; }}
    h2 {{ font-size: 12px; margin: 8px 0 4px; }}
    .top5-wrap {{ grid-template-columns: repeat(5, 1fr); gap: 4px; }}
    .top5-card {{ padding: 4px 6px; break-inside: avoid; }}
    .top5-card .btn-buy-big, .top5-card .btn-sell-big {{ display: none !important; }}
    table {{ font-size: 9px; break-inside: avoid; }}
    th, td {{ padding: 2px 4px !important; font-size: 9px !important; }}
    .grid2 {{ grid-template-columns: 1fr 1fr; gap: 6px; break-inside: avoid; }}
    /* 색상 유지 */
    * {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}
  }}

  /* 커뮤니티 우측 사이드바 (Firebase 익명 게시판) */
  .community-panel {{
    position: fixed; right: 12px; top: 80px; width: 320px;
    max-height: calc(100vh - 100px); overflow-y: auto;
    background: white; border-radius: 8px; padding: 14px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.08); z-index: 50;
    font-size: 12px;
  }}
  @media (max-width: 1500px) {{
    .community-panel {{ position: static; width: auto; max-height: none; margin-top: 24px; }}
  }}
  .community-title {{ margin: 0 0 4px; font-size: 14px; border-bottom: 2px solid #16a085; padding-bottom: 3px; }}
  .comments-hint {{ font-size: 11px; color: #95a5a6; margin-bottom: 8px; }}

  .nick-row {{ display: flex; gap: 4px; margin-bottom: 8px; align-items: center; }}
  .nick-label {{ font-size: 11px; color: #7f8c8d; }}
  #nick-input {{ flex: 1; padding: 4px 8px; border: 1px solid #ddd; border-radius: 4px; font-size: 11px; }}
  .nick-save {{ padding: 4px 8px; border: 0; border-radius: 4px; background: #16a085; color: white;
                font-size: 10px; cursor: pointer; }}
  .nick-saved {{ background: #95a5a6 !important; }}

  .thread-compose textarea {{
    width: 100%; padding: 6px; border: 1px solid #ddd; border-radius: 4px;
    font-size: 11px; resize: vertical; font-family: inherit;
  }}
  .thread-editor {{
    width: 100%; min-height: 56px; max-height: 200px; overflow-y: auto;
    padding: 6px; border: 1px solid #ddd; border-radius: 4px;
    font-size: 11px; font-family: inherit; line-height: 1.4;
    outline: none; word-break: break-word; white-space: pre-wrap;
  }}
  .thread-editor:focus {{ border-color: #16a085; }}
  .thread-editor:empty::before {{
    content: attr(data-placeholder); color: #aaa; pointer-events: none;
  }}
  .thread-editor .mention {{
    color: #2980b9; font-weight: 600; background: #ecf6fc;
    padding: 0 3px; border-radius: 3px;
  }}
  .thread-submit {{
    background: #16a085; color: white; border: 0; padding: 6px 14px;
    border-radius: 4px; font-size: 11px; cursor: pointer; margin-top: 4px;
    font-weight: 600;
  }}
  .thread-submit:disabled {{ background: #bdc3c7; cursor: not-allowed; }}

  .thread-list {{ margin-top: 12px; }}
  .thread-loading {{ text-align: center; color: #95a5a6; padding: 16px 0; font-size: 11px; }}
  .thread-item {{
    background: #fafbfc; border-radius: 4px; padding: 8px;
    margin-bottom: 6px; border-left: 3px solid #16a085;
  }}
  .thread-head {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px; }}
  .thread-nick {{ font-size: 11px; font-weight: 600; color: #2c3e50; }}
  .thread-time {{ font-size: 10px; color: #95a5a6; }}
  .thread-text {{ font-size: 12px; color: #2c3e50; white-space: pre-wrap; word-break: break-word; }}
  .thread-text .mention {{ color: #2980b9; font-weight: 600; cursor: pointer; }}
  .thread-text .mention:hover {{ text-decoration: underline; color: #1f5f8b; }}
  .reply-item .mention {{ color: #2980b9; font-weight: 600; cursor: pointer; }}
  .reply-item .mention:hover {{ text-decoration: underline; }}
  /* 종목 필터 chip */
  .stock-filter {{
    background: #d6eaf8; color: #1f5f8b; padding: 4px 10px;
    border-radius: 12px; font-size: 11px; font-weight: 600;
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 8px;
  }}
  .stock-filter .clear-btn {{
    background: transparent; border: 1px solid #2980b9; color: #2980b9;
    padding: 2px 8px; border-radius: 8px; cursor: pointer; font-size: 10px;
  }}
  .stock-filter .clear-btn:hover {{ background: #2980b9; color: white; }}
  .reply-toggle {{ font-size: 10px; color: #3498db; cursor: pointer; margin-top: 4px; display: inline-block; }}
  .reply-list {{ margin-top: 6px; padding-left: 8px; border-left: 2px solid #ecf0f1; }}
  .reply-item {{ background: white; padding: 5px; border-radius: 3px; margin-bottom: 3px; font-size: 11px; }}
  .reply-nick {{ font-weight: 600; color: #2c3e50; margin-right: 4px; }}
  .reply-compose {{ display: flex; gap: 4px; margin-top: 4px; }}
  .reply-compose input {{ flex: 1; padding: 3px 6px; border: 1px solid #ddd; border-radius: 3px; font-size: 11px; }}
  .reply-compose button {{ background: #3498db; color: white; border: 0; padding: 3px 8px; border-radius: 3px;
                           font-size: 10px; cursor: pointer; }}
  /* 수정/삭제 버튼 */
  .thread-meta {{ display: inline-flex; gap: 6px; align-items: center; }}
  .thread-actions, .reply-actions {{ display: inline-flex; gap: 4px; }}
  .thread-actions a, .reply-actions a {{
    cursor: pointer; color: #95a5a6; font-size: 11px; padding: 0 2px;
  }}
  .thread-actions a:hover {{ color: #16a085; }}
  .th-del:hover, .rp-del:hover {{ color: #c0392b !important; }}
  .reply-actions {{ margin-left: 4px; }}

  /* 섹션 표시 토글 (todayygg 스타일 chip) */
  .layer-toggles {{
    display: flex; gap: 6px; flex-wrap: wrap;
    background: white; border-radius: 6px; padding: 8px 10px;
    margin-bottom: 8px; box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    font-size: 11px;
  }}
  .layer-toggles .lt-label {{ color: #7f8c8d; font-weight: 600; margin-right: 4px; align-self: center; }}
  .layer-toggles label {{
    display: inline-flex; align-items: center; gap: 3px;
    padding: 4px 9px; background: #ecf0f1; border-radius: 14px;
    cursor: pointer; user-select: none; transition: all 0.1s;
  }}
  .layer-toggles label:hover {{ background: #d6dbdf; }}
  .layer-toggles label.active {{ background: #3498db; color: white; }}
  .layer-toggles input {{ display: none; }}

  /* 연속매수/매도 배지 */
  .consec-badge {{
    display: inline-block; padding: 1px 6px; font-size: 10px; font-weight: 600;
    border-radius: 8px; margin-left: 4px; vertical-align: middle;
  }}
  .consec-buy {{ background: #fee; color: #c0392b; border: 1px solid #f5c6cb; }}
  .consec-sell {{ background: #eef; color: #2980b9; border: 1px solid #b8daff; }}

  /* AI 분석 점수 grade */
  .score-grade {{ font-size: 10px; font-weight: 700; padding: 1px 5px; border-radius: 8px; }}
  .s-aplus {{ background: #c0392b; color: white; }}
  .s-a     {{ background: #e74c3c; color: white; }}
  .s-b     {{ background: #fadbd8; color: #c0392b; }}
  .s-c     {{ background: #ecf0f1; color: #7f8c8d; }}
  .s-d     {{ background: #d6eaf8; color: #2980b9; }}
  .s-f     {{ background: #3498db; color: white; }}
  .s-fminus{{ background: #2471a3; color: white; }}

  /* 카테고리 네비게이션 (실시간/마감 토글) */
  .nav-tabs {{
    display: flex; gap: 4px; background: white; border-radius: 8px;
    padding: 4px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); width: fit-content;
  }}
  .nav-tabs a {{
    display: inline-block; padding: 10px 20px; border-radius: 5px;
    text-decoration: none; color: #7f8c8d; font-weight: 500; font-size: 14px;
    transition: all 0.15s;
  }}
  .nav-tabs a:hover {{ background: #ecf0f1; color: #2c3e50; }}
  .nav-tabs a.active {{ background: #3498db; color: white; }}
  .mode-subtitle {{
    font-size: 13px; color: #2c3e50; margin: 4px 0 12px;
    padding: 8px 14px; background: #ecf0f1; border-radius: 4px; display: inline-block;
  }}
  .mode-subtitle b {{ color: #3498db; }}
  .freshness-note {{
    background: #e8f4f8; border-left: 4px solid #3498db; padding: 10px 14px;
    margin: 8px 0 16px; border-radius: 4px; font-size: 13px; color: #2c3e50;
  }}

  /* 검색바 + 즐겨찾기 토글 */
  .search-bar {{
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    background: white; border-radius: 8px; padding: 8px 12px;
    margin: 0 0 12px; box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  }}
  .search-bar input[type="text"] {{
    flex: 1; min-width: 200px; padding: 7px 10px; border: 1px solid #d6dde3;
    border-radius: 5px; font-size: 13px; outline: none;
  }}
  .search-bar input[type="text"]:focus {{ border-color: #3498db; }}
  .fav-only-toggle {{
    display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: #2c3e50;
    cursor: pointer; user-select: none;
  }}
  .fav-only-toggle input {{ margin: 0; }}
  .fav-count {{ font-size: 11px; color: #7f8c8d; }}
  .search-clear {{
    background: #ecf0f1; border: 0; padding: 5px 10px; border-radius: 4px;
    cursor: pointer; font-size: 12px; color: #2c3e50;
  }}
  .search-clear:hover {{ background: #d6dde3; }}

  /* 즐겨찾기 별 */
  .fav-star {{
    display: inline-block; cursor: pointer; color: #d6dde3; margin-right: 4px;
    font-size: 13px; user-select: none; transition: color 0.1s, transform 0.1s;
  }}
  .fav-star:hover {{ transform: scale(1.2); }}
  .fav-star.fav-on {{ color: #f1c40f; }}
  .fav-star-top5 {{
    position: absolute; top: 6px; right: 8px; font-size: 16px; margin: 0;
  }}
  .top5-card {{ position: relative; }}

  /* 검색에 안 맞은 행 숨김 */
  .stock-row.hidden-search {{ display: none !important; }}

  /* 7거래일 차트 */
  .chart-section {{
    background: white; border-radius: 8px; padding: 12px 16px;
    margin: 12px 0; box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  }}
  .chart-section h2 {{ margin-top: 0; }}
  .chart-wrap {{ position: relative; height: 280px; }}
  @media (max-width: 700px) {{ .chart-wrap {{ height: 220px; }} }}

  /* 연속 누적 매수 페이지 */
  .continuity-page {{ margin-top: 8px; }}
  .continuity-chart-section, .continuity-table-section {{
    background: white; border-radius: 8px; padding: 12px 16px;
    margin-bottom: 12px; box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  }}
  .continuity-chart-section h2, .continuity-table-section h2 {{ margin-top: 0; font-size: 15px; }}
  .continuity-chart-wrap {{ position: relative; min-height: 560px; }}
  .burn-icon {{ display: inline-block; margin-right: 4px; font-size: 13px; }}
  .burn-burning {{ color: #e74c3c; }}
  .burn-watering {{ color: #3498db; }}
  .burn-neutral  {{ color: #7f8c8d; }}
  .burn-unknown  {{ color: #bdc3c7; }}
  .continuity-table td.burn-cell {{
    font-size: 11px; line-height: 1.3;
  }}
  .continuity-table td.burn-cell .avg-info {{
    color: #7f8c8d; margin-left: 4px;
  }}

  /* AI 페이지 좌우 분할 */
  .ai-page-split {{
    display: grid;
    grid-template-columns: minmax(0, 1fr) 380px;
    gap: 14px;
    align-items: start;
    margin-top: 8px;
  }}
  @media (max-width: 1100px) {{
    .ai-page-split {{ grid-template-columns: 1fr; }}
  }}
  .ai-table-pane {{
    background: white; border-radius: 8px; padding: 12px 14px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    min-width: 0;   /* grid item overflow 방지 */
  }}
  .ai-table-pane h2 {{ margin-top: 0; }}
  .ai-table-scroll {{ overflow-x: auto; }}
  .ai-table-pane tbody tr.stock-row {{ cursor: pointer; }}
  .ai-table-pane tbody tr.stock-row:hover {{ background: #ecf0f1; }}
  .ai-table-pane tbody tr.stock-row.selected {{
    background: #d6eaf8 !important;
    box-shadow: inset 3px 0 0 #3498db;
  }}
  .ai-card-pane {{
    position: sticky; top: 12px;
    background: white; border-radius: 8px; padding: 0;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    max-height: calc(100vh - 24px); overflow-y: auto;
  }}
  @media (max-width: 1100px) {{
    .ai-card-pane {{ position: static; max-height: none; }}
  }}
  .ai-card-pane-inner {{ padding: 12px 14px; }}
  .ai-card-empty {{
    text-align: center; color: #95a5a6; padding: 40px 12px;
    font-size: 13px;
  }}
  .ai-card-pane .ai-card {{
    border: 0; padding: 0; margin: 0; box-shadow: none;
  }}
  /* 우측 카드 내부 변수 기여도 그리드 */
  .ai-card-breakdown {{
    margin-top: 10px; padding-top: 8px;
    border-top: 1px solid #ecf0f1;
  }}
  .ai-card-breakdown b {{ font-size: 11px; color: #2c3e50; }}
  .bd-grid {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 3px 8px;
    font-size: 11px; margin-top: 6px;
  }}
  .bd-grid > div {{
    display: flex; justify-content: space-between; align-items: center;
    padding: 3px 6px; border-radius: 3px; background: #f8f9fa;
  }}
  .bd-grid span {{ color: #7f8c8d; }}
  .bd-grid b {{ font-variant-numeric: tabular-nums; font-size: 11px; }}
  .bd-grid b.zero {{ color: #bdc3c7; font-weight: 400; }}

  /* AI 표 폰트 살짝 키움 (슬림화로 컬럼 줄어든 만큼) */
  .ai-table th, .ai-table td {{ font-size: 12px; padding: 6px 8px; }}
  .ai-table td.code, .ai-table td.name {{ font-size: 12px; }}

  /* 테마 페이지 */
  .themes-page {{ margin-top: 8px; }}
  .themes-grid {{
    display: grid;
    /* 좌측 차트 영역 넓게, 우측 표 좁게 (3:2) */
    grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
    gap: 14px; margin-bottom: 14px;
  }}
  @media (max-width: 1100px) {{ .themes-grid {{ grid-template-columns: 1fr; }} }}
  .theme-chart-section, .theme-table-section, .theme-stocks-section {{
    background: white; border-radius: 8px; padding: 12px 16px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  }}
  .theme-chart-section {{ display: flex; flex-direction: column; }}
  .theme-chart-section h2, .theme-table-section h2, .theme-stocks-section h2 {{ margin-top: 0; }}
  .theme-note {{ font-size: 11px; color: #7f8c8d; margin: 2px 0 8px; }}

  /* 기간 토글 탭 */
  .theme-period-tabs {{
    display: flex; gap: 4px; background: #f8f9fa; border-radius: 6px;
    padding: 3px; margin: 6px 0 10px; width: fit-content;
  }}
  .theme-period-tabs button {{
    padding: 6px 14px; border: 0; background: transparent;
    border-radius: 4px; cursor: pointer; font-size: 12px;
    color: #7f8c8d; font-weight: 500;
  }}
  .theme-period-tabs button.active {{ background: #3498db; color: white; }}
  .theme-period-tabs button:hover:not(.active) {{ background: #ecf0f1; color: #2c3e50; }}

  /* 매수/매도 차트 두 개 세로 스택 */
  .theme-chart-stack {{ display: flex; flex-direction: column; gap: 14px; flex: 1; }}
  .theme-chart-sub h3 {{
    margin: 0 0 4px; font-size: 12px; font-weight: 600;
    padding: 4px 8px; border-radius: 4px; display: inline-block;
  }}
  .theme-chart-sub.buy h3 {{ background: #fde9e7; color: #c0392b; }}
  .theme-chart-sub.sell h3 {{ background: #e3f0fa; color: #2471a3; }}
  .theme-chart-sub .chart-wrap {{ height: 320px; }}
  @media (max-width: 700px) {{ .theme-chart-sub .chart-wrap {{ height: 220px; }} }}

  /* 표는 차트가 길어지면 같이 늘어남 (height match) */
  .theme-table-section {{ overflow: hidden; }}
  .theme-table-section table {{ font-size: 11px; }}
  .theme-table-section th, .theme-table-section td {{ padding: 4px 6px; }}

  .theme-summary-table tr {{ cursor: pointer; }}
  .theme-summary-table tr:hover {{ background: #ecf0f1; }}
  .theme-summary-table tr.selected {{ background: #d6eaf8 !important; }}
  .theme-summary-table tr.selected td {{ font-weight: 600; }}

  /* 대량매매 페이지 */
  .bulk-page {{ margin-top: 8px; }}
  .bulk-page h2 {{ font-size: 15px; margin-top: 18px; }}
  /* 상위 메인 탭 (10만주 일일 / 주간 / 5%보유) */
  .bulk-main-tabs {{
    display: flex; gap: 6px; background: white; border-radius: 8px;
    padding: 6px; margin: 0 0 12px; width: fit-content;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  }}
  .bulk-main-tabs button {{
    padding: 10px 22px; border: 0; background: transparent;
    border-radius: 6px; cursor: pointer; font-size: 14px;
    color: #7f8c8d; font-weight: 600;
  }}
  .bulk-main-tabs button.active {{ background: #3498db; color: white; }}
  .bulk-main-tabs button:hover:not(.active) {{ background: #ecf0f1; color: #2c3e50; }}
  .bulk-main-content {{ margin-top: 4px; }}
  /* 5%룰 내부의 보조 서브탭 (국민연금만 / 전체) */
  .bulk-sub-tabs {{
    display: flex; gap: 4px; background: white; border-radius: 8px;
    padding: 4px; margin: 10px 0; width: fit-content;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
  }}
  .bulk-sub-tabs button {{
    padding: 8px 16px; border: 0; background: transparent;
    border-radius: 5px; cursor: pointer; font-size: 13px;
    color: #7f8c8d; font-weight: 500;
  }}
  .bulk-sub-tabs button.active {{ background: #95a5a6; color: white; }}
  .bulk-sub-tabs button:hover:not(.active) {{ background: #ecf0f1; color: #2c3e50; }}
  .bulk-tab-content {{ margin-top: 8px; }}
</style>
</head>
<body>

<h1>연기금 매매 리포트</h1>
<div class="nav-tabs">
  <a href="realtime.html" class="{mode_active_rt}">⏱ 실시간 업데이트</a>
  <a href="closing.html" class="{mode_active_cl}">📊 마감 기준</a>
  <a href="ai.html" class="{mode_active_ai}">🤖 AI 점수</a>
  <a href="bulk.html" class="{mode_active_bulk}">📦 대량매매</a>
  <a href="themes.html" class="{mode_active_themes}">🏷 테마</a>
  <a href="continuity.html" class="{mode_active_cont}">🔥 연속매수</a>
  <button class="pdf-btn" onclick="window.print()">📥 PDF 저장</button>
</div>

<div class="search-bar">
  <input type="text" id="stock-search" placeholder="🔍 종목명 또는 코드 검색 (예: 삼성전자, 005930)" autocomplete="off" />
  <label class="fav-only-toggle"><input type="checkbox" id="fav-only"> ⭐ 즐겨찾기만</label>
  <span class="fav-count" id="fav-count"></span>
  <button id="search-clear" class="search-clear" style="display:none;">초기화</button>
</div>

<div class="mode-subtitle">{mode_subtitle}</div>
{data_freshness_note}
<div class="meta">기준일자: <b>{trade_date}</b> &nbsp;|&nbsp; 생성: {generated_at} &nbsp;|&nbsp; 출처: KRX 공개 데이터</div>

<div class="layer-toggles">
  <span class="lt-label">표시:</span>
  <label data-sec="overview"><input type="checkbox" checked> 시장수급+Top5</label>
  <label data-sec="weekly7"><input type="checkbox" checked> 최근 7거래일</label>
  <label data-sec="today-top20"><input type="checkbox" checked> 오늘 Top 20</label>
  <label data-sec="today-top50"><input type="checkbox"> 오늘 Top 50</label>
  <label data-sec="cap-top30"><input type="checkbox" checked> 매수비율 Top 30</label>
  <label data-sec="weekly-top30"><input type="checkbox" checked> 주간 누적</label>
  <label data-sec="cumul-top30"><input type="checkbox"> 전체 누적 (시행 이후)</label>
  <label data-sec="comments"><input type="checkbox" checked> 💬 커뮤니티</label>
</div>
{no_data_banner}

{('''<div class="ai-page ai-page-split">
  <div class="ai-table-pane">
    <h2>🤖 종목별 AI 점수 + 변수 기여도</h2>
    <div class="filter-hint">행 클릭 → 우측에 상세 카드 노출 · 컬럼 헤더 클릭 → 정렬</div>
    <div class="ai-table-scroll">
      <table class="sortable ai-table">
        <thead><tr>
          <th>코드</th><th>종목명</th>
          <th class="num" data-sort="num">총점</th>
          <th class="num" data-sort="num" title="RSI 14일 (≥70 과매수 / ≤30 과매도)">RSI</th>
          <th class="num" data-sort="num">순매수</th>
          <th class="num" data-sort="num">등락률</th>
          <th>시장</th><th>주문</th>
        </tr></thead>
        <tbody>''' + ai_html + '''</tbody>
      </table>
    </div>
  </div>

  <div class="ai-card-pane">
    <div class="ai-card-pane-inner" id="ai-card-content">
      <div class="ai-card-empty">좌측 표에서 종목을 선택하세요</div>
    </div>
  </div>
</div>

<div class="ai-footnote">
  ⚠ AI 점수는 머신러닝이 아닌 가중합 휴리스틱입니다. 백테스팅 안 됨.
  투자 판단 보조 지표로만 활용. 모든 매매 결정은 본인 책임.
</div>''') if mode == 'ai' else ''}

{('''<div class="bulk-page">

<div class="bulk-main-tabs">
  <button class="bulk-main-tab active" data-target="bulk-main-daily">📅 10만주↑ (일일, 30일)</button>
  <button class="bulk-main-tab" data-target="bulk-main-cumul">📊 10만주↑ (주간, 7거래일)</button>
  <button class="bulk-main-tab" data-target="bulk-main-fivepct">📋 5% 보유</button>
</div>

<div class="bulk-main-content" id="bulk-main-daily">
  <h2>🔢 일일 10만주↑ 매매 (최근 30일)</h2>
  <div class="filter-hint">연기금 일일 매매 중 |순매수주식수| ≥ 100,000 인 행만 표시. 컬럼 헤더 클릭 → 정렬</div>
  <table class="sortable">
    <thead><tr>
      <th>날짜</th>
      <th>코드</th>
      <th>종목명</th>
      <th>구분</th>
      <th class="num" data-sort="num">주식수</th>
      <th class="num" data-sort="num">매매금액</th>
      <th class="num" data-sort="num" title="시총 대비 매매금액 비율">시총比</th>
      <th>시장</th>
      <th>주문</th>
    </tr></thead>
    <tbody>''' + bulk_daily_html + '''</tbody>
  </table>
</div>

<div class="bulk-main-content" id="bulk-main-cumul" style="display:none;">
  <h2>🔢 주간 10만주↑ 누적 매매 (최근 7거래일)</h2>
  <div class="filter-hint">최근 7거래일 종목별 |Σ 순매수주식수| ≥ 100,000 인 종목만 표시</div>
  <table class="sortable">
    <thead><tr>
      <th>코드</th>
      <th>종목명</th>
      <th>누적 구분</th>
      <th class="num" data-sort="num">누적 주식수</th>
      <th class="num" data-sort="num">누적 금액</th>
      <th class="num" data-sort="num">거래일수</th>
      <th>시장</th>
      <th>주문</th>
    </tr></thead>
    <tbody>''' + bulk_cumul_html + '''</tbody>
  </table>
</div>

<div class="bulk-main-content" id="bulk-main-fivepct" style="display:none;">
  <h2>📋 5%룰 (대량보유상황보고서)</h2>
  <div class="filter-hint">DART OpenAPI 기준. 연기금이 매매한 종목만 노출 (DB 90일 매매 기록 보유)</div>
  <div class="bulk-sub-tabs">
    <button class="bulk-tab-btn active" data-target="bulk-nps">👁 국민연금공단만</button>
    <button class="bulk-tab-btn" data-target="bulk-all">📚 전체 5%룰 (모든 보고자)</button>
  </div>

  <div class="bulk-tab-content" id="bulk-nps">
    <table class="sortable">
      <thead><tr>
        <th>보고일</th>
        <th>코드</th>
        <th>종목명</th>
        <th>구분</th>
        <th>보고자</th>
        <th class="num" data-sort="num">보유주수</th>
        <th class="num" data-sort="num">보유율(%)</th>
        <th>주문</th>
      </tr></thead>
      <tbody>''' + nps_html_rows + '''</tbody>
    </table>
  </div>

  <div class="bulk-tab-content" id="bulk-all" style="display:none;">
    <table class="sortable">
      <thead><tr>
        <th>보고일</th>
        <th>코드</th>
        <th>종목명</th>
        <th>구분</th>
        <th>보고자</th>
        <th class="num" data-sort="num">보유주수</th>
        <th class="num" data-sort="num">보유율(%)</th>
        <th>주문</th>
      </tr></thead>
      <tbody>''' + all_html_rows + '''</tbody>
    </table>
  </div>
</div>

<div class="ai-footnote">
  📌 데이터 출처: DART OpenAPI 대량보유상황보고서 (5%룰). 캐시 TTL 12시간.
  보고일 = DART 접수일이며 실제 보유 시점과 다를 수 있음 (신규 5일·변동 5일 내 보고 의무).
</div>
</div>''') if mode == 'bulk' else ''}

{('''<div class="themes-page">

<div class="themes-grid">
  <section class="theme-chart-section">
    <h2>테마별 순매수/순매도</h2>
    <p class="theme-note">막대 또는 우측 표 행 클릭 → 하단 종목.</p>
    <div class="theme-period-tabs">
      <button class="theme-period-tab active" data-period="today">📅 오늘</button>
      <button class="theme-period-tab" data-period="yesterday">📆 어제</button>
      <button class="theme-period-tab" data-period="week">📊 최근 7거래일</button>
    </div>
    <div class="theme-chart-stack">
      <div class="theme-chart-sub buy">
        <h3>📈 순매수 Top 10</h3>
        <div class="chart-wrap"><canvas id="theme-chart-buy"></canvas></div>
      </div>
      <div class="theme-chart-sub sell">
        <h3>📉 순매도 Top 10</h3>
        <div class="chart-wrap"><canvas id="theme-chart-sell"></canvas></div>
      </div>
    </div>
  </section>
  <section class="theme-table-section">
    <h2>테마별 수급 요약</h2>
    <div class="filter-hint">컬럼 헤더 클릭 → 정렬 · 행 클릭 → 하단 종목 노출</div>
    <table class="sortable theme-summary-table">
      <thead><tr>
        <th>#</th>
        <th>테마</th>
        <th class="num" data-sort="num">종목수</th>
        <th class="num" data-sort="num">매수대금</th>
        <th class="num" data-sort="num">매도대금</th>
        <th class="num" data-sort="num">순매수대금</th>
        <th class="num" data-sort="num">순매수 종목수</th>
        <th class="num" data-sort="num">순매도 종목수</th>
      </tr></thead>
      <tbody id="theme-summary-body"></tbody>
    </table>
  </section>
</div>

<section class="theme-stocks-section">
  <h2>선택 테마 구성 종목 - <span id="selected-theme-name">(테마를 선택하세요)</span></h2>
  <table class="theme-stocks-table">
    <thead><tr>
      <th>#</th>
      <th>코드</th>
      <th>종목명</th>
      <th class="num">순매수대금</th>
      <th class="num" title="시총 대비 순매수 비율">시총대비</th>
      <th class="num">오늘 매수평단</th>
      <th class="num">기간 매수평단 / 순매수 기간</th>
      <th>주문</th>
    </tr></thead>
    <tbody id="theme-stocks-body">
      <tr><td colspan="8" class="empty">위에서 테마를 선택해주세요</td></tr>
    </tbody>
  </table>
</section>

</div>''') if mode == 'themes' else ''}

{('''<div class="continuity-page">

<section class="continuity-chart-section">
  <h2>🔥 구간 누적 순매수 — Top 20 (가로 막대)</h2>
  <div class="filter-hint">색 = 🔥 불타기 (오늘 평단↑) / 💧 물타기 (오늘 평단↓) / ━ 보합 / ❓ 데이터 부족. 호버 시 상세 정보</div>
  <div class="continuity-chart-wrap"><canvas id="continuity-chart"></canvas></div>
</section>

<section class="continuity-table-section">
  <h2>전체 연속 매수 종목 (구간 10일↑)</h2>
  <div class="filter-hint">컬럼 헤더 클릭 → 정렬</div>
  <table class="sortable continuity-table">
    <thead><tr>
      <th>#</th>
      <th>코드</th>
      <th>종목명</th>
      <th class="num" data-sort="num">연속(거래일)</th>
      <th>연속 구간</th>
      <th>불타기·물타기 / 누적평단</th>
      <th class="num" data-sort="num">오늘 평단</th>
      <th class="num" data-sort="num">구간 누적 순매수</th>
      <th>주문</th>
    </tr></thead>
    <tbody id="continuity-table-body">
      <tr><td colspan="9" class="empty">연속 매수 종목 데이터 로딩 중...</td></tr>
    </tbody>
  </table>
</section>

</div>''') if mode == 'continuity' else ''}

<div class="layout-header" {'style="display:none"' if mode in ('ai', 'bulk') else ''}>
  <div class="layout-left" data-section="overview">
    <h2>오늘 시장 수급 (연기금)</h2>
    <div class="summary">
      <div class="card"><div class="label">매수 총합</div><div class="value">{_fmt_won(total_buy)}</div></div>
      <div class="card"><div class="label">매도 총합</div><div class="value">{_fmt_won(total_sell)}</div></div>
      <div class="card"><div class="label">순매수</div><div class="value {'pos' if total_net >= 0 else 'neg'}">{_fmt_won(total_net)}</div></div>
    </div>

    <h2>🏆 Top 5 순매수 (오늘)</h2>
    <div class="top5-wrap">{top5_buy_html}</div>

    <h2>🏆 Top 5 순매도 (오늘)</h2>
    <div class="top5-wrap">{top5_sell_html}</div>
  </div>

  <div class="layout-right" data-section="weekly7">
    <h2>📅 최근 7거래일 (KOSPI)</h2>
    <table>
      <thead><tr><th>일자</th><th class="num">매수</th><th class="num">매도</th><th class="num">순매수</th></tr></thead>
      <tbody>{recent_html}</tbody>
    </table>
  </div>
</div>

<div class="grid2" data-section="today-top20">
  <div>
    <h2>오늘 연기금 순매수 Top 20</h2>
    <div class="filter-hint">컬럼 헤더 클릭 → 정렬 · AI 점수: 매수비율×100 + 등락률×2 + 연속일×5 + DART공시 종합</div>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">순매수</th>
        <th class="num" data-sort="num">매수</th>
        <th class="num" data-sort="num">매도</th>
        <th class="num" data-sort="num">등락률</th>
        <th class="num" data-sort="num">현재가</th>
        <th class="num" data-sort="num" title="시총 대비 순매수 비율 (큰 자금 유입 시그널)">매수비율</th>
        <th class="num" data-sort="num">AI</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{top_buy_html}</tbody>
    </table>
  </div>
  <div>
    <h2>오늘 연기금 순매도 Top 20</h2>
    <div class="filter-hint">컬럼 헤더 클릭 → 정렬 · AI 점수 음수 = 매도 강세</div>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">순매도</th>
        <th class="num" data-sort="num">매수</th>
        <th class="num" data-sort="num">매도</th>
        <th class="num" data-sort="num">등락률</th>
        <th class="num" data-sort="num">현재가</th>
        <th class="num" data-sort="num" title="시총 대비 순매수 비율 (큰 자금 유입 시그널)">매수비율</th>
        <th class="num" data-sort="num">AI</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{top_sell_html}</tbody>
    </table>
  </div>
</div>

<div class="grid2" data-section="cap-top30">
  <div>
    <h2>매수비율 Top 30 (시총 대비 순매수 큰 순)</h2>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">순매수</th>
        <th class="num" data-sort="num">매수</th>
        <th class="num" data-sort="num">매도</th>
        <th class="num" data-sort="num">등락률</th>
        <th class="num" data-sort="num">현재가</th>
        <th class="num" data-sort="num" title="시총 대비 순매수 비율 (큰 자금 유입 시그널)">매수비율</th>
        <th class="num" data-sort="num">AI</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{top_cap_buy_html}</tbody>
    </table>
  </div>
  <div>
    <h2>매수비율 Top 30 (시총 대비 순매도 큰 순)</h2>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">순매도</th>
        <th class="num" data-sort="num">매수</th>
        <th class="num" data-sort="num">매도</th>
        <th class="num" data-sort="num">등락률</th>
        <th class="num" data-sort="num">현재가</th>
        <th class="num" data-sort="num" title="시총 대비 순매수 비율 (큰 자금 유입 시그널)">매수비율</th>
        <th class="num" data-sort="num">AI</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{top_cap_sell_html}</tbody>
    </table>
  </div>
</div>

<div class="grid2" data-section="weekly-top30">
  <div>
    <h2>📅 주간 누적 순매수 Top 30 (최근 7거래일)</h2>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">누적순매수</th>
        <th class="num" data-sort="num">누적매수</th>
        <th class="num" data-sort="num">누적매도</th>
        <th class="num" data-sort="num">거래일수</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{weekly_buy_html}</tbody>
    </table>
  </div>
  <div>
    <h2>📅 주간 누적 순매도 Top 30 (최근 7거래일)</h2>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">누적순매도</th>
        <th class="num" data-sort="num">누적매수</th>
        <th class="num" data-sort="num">누적매도</th>
        <th class="num" data-sort="num">거래일수</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{weekly_sell_html}</tbody>
    </table>
  </div>
</div>

<section class="chart-section" data-section="weekly7">
  <h2>📈 최근 7거래일 시장 수급 (KOSPI, 단위: 억원)</h2>
  <div class="chart-wrap"><canvas id="weekly-chart"></canvas></div>
</section>

<div class="grid2" data-section="today-top50" style="display:none;">
  <div>
    <h2>📊 오늘 연기금 순매수 Top 50</h2>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">순매수</th>
        <th class="num" data-sort="num">매수</th>
        <th class="num" data-sort="num">매도</th>
        <th class="num" data-sort="num">등락률</th>
        <th class="num" data-sort="num">현재가</th>
        <th class="num" data-sort="num" title="시총 대비 순매수 비율">매수비율</th>
        <th class="num" data-sort="num">AI</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{top_buy50_html}</tbody>
    </table>
  </div>
  <div>
    <h2>📊 오늘 연기금 순매도 Top 50</h2>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">순매도</th>
        <th class="num" data-sort="num">매수</th>
        <th class="num" data-sort="num">매도</th>
        <th class="num" data-sort="num">등락률</th>
        <th class="num" data-sort="num">현재가</th>
        <th class="num" data-sort="num" title="시총 대비 순매수 비율">매수비율</th>
        <th class="num" data-sort="num">AI</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{top_sell50_html}</tbody>
    </table>
  </div>
</div>

<div class="grid2" data-section="cumul-top30" style="display:none;">
  <div>
    <h2>🏛 전체 누적 순매수 Top 30 <small style="font-size:11px;color:#7f8c8d;">(REPORT-YGG 시행 이후 합산)</small></h2>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">누적순매수</th>
        <th class="num" data-sort="num">누적매수</th>
        <th class="num" data-sort="num">누적매도</th>
        <th class="num" data-sort="num">거래일수</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{cumul_buy_html}</tbody>
    </table>
  </div>
  <div>
    <h2>🏛 전체 누적 순매도 Top 30 <small style="font-size:11px;color:#7f8c8d;">(REPORT-YGG 시행 이후 합산)</small></h2>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">누적순매도</th>
        <th class="num" data-sort="num">누적매수</th>
        <th class="num" data-sort="num">누적매도</th>
        <th class="num" data-sort="num">거래일수</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{cumul_sell_html}</tbody>
    </table>
  </div>
</div>

<aside class="community-panel" data-section="comments" id="community">
  <h2 class="community-title">💬 커뮤니티</h2>
  <div class="comments-hint">닉네임 입력 후 글 작성 · @종목명으로 종목 토론</div>

  <div class="nick-row">
    <span class="nick-label">닉네임:</span>
    <input id="nick-input" type="text" maxlength="20" placeholder="익명">
    <button id="nick-save" class="nick-save">저장</button>
  </div>

  <div class="thread-compose">
    <div id="thread-text" class="thread-editor" contenteditable="true"
         data-placeholder="@종목명을 통해 종목에 대한 의견을 남길 수 있습니다"></div>
    <button id="thread-submit" class="thread-submit">게시</button>
  </div>

  <div id="stock-filter-container"></div>

  <div id="thread-list" class="thread-list">
    <div class="thread-loading">불러오는 중...</div>
  </div>
</aside>

<div class="footer">
  ⚠ 투자 권유가 아닙니다. KRX 공개 데이터 가공물. &nbsp;|&nbsp;
  데이터 출처: <a href="http://data.krx.co.kr/" target="_blank">data.krx.co.kr</a> &nbsp;|&nbsp;
  매수/매도 버튼 → 토스증권 호가창 (외부 링크)
</div>

<script>
// 섹션 표시 토글 (localStorage 저장)
const LS_KEY = 'report-ygg-sections-v1';
const sectionState = JSON.parse(localStorage.getItem(LS_KEY) || '{{}}');

function applySectionState() {{
  document.querySelectorAll('.layer-toggles label').forEach(label => {{
    const sec = label.dataset.sec;
    const checkbox = label.querySelector('input');
    const visible = sectionState[sec] !== false;   // 기본 true
    checkbox.checked = visible;
    label.classList.toggle('active', visible);
    document.querySelectorAll('[data-section="' + sec + '"]').forEach(el => {{
      el.style.display = visible ? '' : 'none';
    }});
  }});
}}

document.querySelectorAll('.layer-toggles input').forEach(checkbox => {{
  checkbox.addEventListener('change', () => {{
    const label = checkbox.closest('label');
    const sec = label.dataset.sec;
    sectionState[sec] = checkbox.checked;
    localStorage.setItem(LS_KEY, JSON.stringify(sectionState));
    applySectionState();
  }});
}});

applySectionState();

// AI / bulk / themes / continuity 모드일 때 다른 섹션 숨김 (URL 기반)
const _isSpecialPage = ['ai.html', 'bulk.html', 'themes.html', 'continuity.html'].some(
  p => window.location.pathname.endsWith(p)
);
if (_isSpecialPage) {{
  document.querySelectorAll('.grid2, .layer-toggles, .chart-section').forEach(el => {{
    el.style.display = 'none';
  }});
  // layout-header 숨김
  const lh = document.querySelector('.layout-header');
  if (lh) lh.style.display = 'none';
  // bulk/themes/continuity 에서는 커뮤니티 사이드바도 숨김
  const cp = document.querySelector('.community-panel');
  if (cp && ['bulk.html', 'themes.html', 'continuity.html'].some(p => window.location.pathname.endsWith(p))) {{
    cp.style.display = 'none';
  }}
}}

// bulk 메인 탭 전환 (10만주 일일 / 주간 / 5%보유)
document.querySelectorAll('.bulk-main-tab').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.bulk-main-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const target = btn.dataset.target;
    document.querySelectorAll('.bulk-main-content').forEach(c => {{
      c.style.display = c.id === target ? '' : 'none';
    }});
  }});
}});

// bulk 5%룰 내부 보조 탭 (국민연금만 ↔ 전체)
document.querySelectorAll('.bulk-tab-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.bulk-tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const target = btn.dataset.target;
    document.querySelectorAll('.bulk-tab-content').forEach(c => {{
      c.style.display = c.id === target ? '' : 'none';
    }});
  }});
}});

// 정렬 가능한 표
document.querySelectorAll('table.sortable th[data-sort]').forEach(th => {{
  th.addEventListener('click', () => {{
    const table = th.closest('table');
    const tbody = table.querySelector('tbody');
    const rows = Array.from(tbody.rows).filter(r => !r.querySelector('.empty'));
    if (rows.length === 0) return;
    const colIdx = Array.from(th.parentNode.children).indexOf(th);
    const currentDir = th.dataset.dir || '';
    const newDir = currentDir === 'desc' ? 'asc' : 'desc';
    // reset all
    table.querySelectorAll('th').forEach(t => t.dataset.dir = '');
    th.dataset.dir = newDir;
    rows.sort((a, b) => {{
      const ac = a.cells[colIdx], bc = b.cells[colIdx];
      const av = parseFloat((ac.dataset.value !== undefined ? ac.dataset.value : ac.textContent).replace(/,/g, '')) || 0;
      const bv = parseFloat((bc.dataset.value !== undefined ? bc.dataset.value : bc.textContent).replace(/,/g, '')) || 0;
      return newDir === 'asc' ? av - bv : bv - av;
    }});
    rows.forEach(r => tbody.appendChild(r));
  }});
}});

// ============================================================
// 종목 즐겨찾기 (localStorage)
// ============================================================
const FAV_KEY = 'report-ygg-favs-v1';
let favSet = new Set(JSON.parse(localStorage.getItem(FAV_KEY) || '[]'));

// 첫 방문 시 보유 종목 자동 즐겨찾기 (사용자 알림용; 토글로 해제 가능)
const DEFAULT_FAVS = [
  '080220',  // 제주반도체 (KOSDAQ)
  '469160',  // TIGER 일본반도체 FACTSET (KOSPI)
  '0015B0',  // KoAct 미국나스닥성장기업 액티브 (KOSPI)
  '001440',  // 대한전선 (KOSPI)
];
const DEFAULT_FAVS_KEY = 'report-ygg-default-favs-v1';
if (!localStorage.getItem(DEFAULT_FAVS_KEY)) {{
  DEFAULT_FAVS.forEach(c => favSet.add(c));
  localStorage.setItem(FAV_KEY, JSON.stringify([...favSet]));
  localStorage.setItem(DEFAULT_FAVS_KEY, '1');
  console.log('보유 종목 4개 자동 즐겨찾기 등록:', DEFAULT_FAVS);
}}

function saveFavs() {{
  localStorage.setItem(FAV_KEY, JSON.stringify([...favSet]));
  updateFavCount();
}}

function updateFavCount() {{
  const el = document.getElementById('fav-count');
  if (el) el.textContent = favSet.size > 0 ? `⭐ ${{favSet.size}}개` : '';
}}

function applyFavStars() {{
  document.querySelectorAll('.fav-star').forEach(star => {{
    const code = star.dataset.stock;
    if (favSet.has(code)) {{
      star.classList.add('fav-on');
      star.textContent = '★';
    }} else {{
      star.classList.remove('fav-on');
      star.textContent = '☆';
    }}
  }});
}}

document.addEventListener('click', e => {{
  const star = e.target.closest('.fav-star');
  if (!star) return;
  e.stopPropagation();
  e.preventDefault();
  const code = star.dataset.stock;
  if (!code) return;
  if (favSet.has(code)) favSet.delete(code);
  else favSet.add(code);
  saveFavs();
  applyFavStars();
  applyStockFilter();   // 즐겨찾기만 모드면 즉시 반영
}});

applyFavStars();
updateFavCount();

// ============================================================
// 종목 검색 + 즐겨찾기 필터 (전 종목 row/card에 적용)
// ============================================================
const searchInput = document.getElementById('stock-search');
const favOnly = document.getElementById('fav-only');
const searchClear = document.getElementById('search-clear');

function applyStockFilter() {{
  const q = (searchInput.value || '').trim().toLowerCase();
  const favMode = favOnly.checked;
  searchClear.style.display = (q || favMode) ? '' : 'none';

  document.querySelectorAll('.stock-row').forEach(el => {{
    const code = (el.dataset.stockCode || '').toLowerCase();
    const name = (el.dataset.stockName || '').toLowerCase();
    let visible = true;
    if (q && !(code.includes(q) || name.includes(q))) visible = false;
    if (favMode && !favSet.has(el.dataset.stockCode)) visible = false;
    el.classList.toggle('hidden-search', !visible);
  }});
}}

searchInput.addEventListener('input', applyStockFilter);
favOnly.addEventListener('change', applyStockFilter);
searchClear.addEventListener('click', () => {{
  searchInput.value = '';
  favOnly.checked = false;
  applyStockFilter();
}});

// ============================================================
// 7거래일 시장 수급 차트 (Chart.js)
// ============================================================
(function() {{
  const ctx = document.getElementById('weekly-chart');
  if (!ctx || typeof Chart === 'undefined') return;
  const data = {chart_data_json};
  if (!data || data.length === 0) return;
  new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: data.map(d => d.date),
      datasets: [
        {{
          label: '매수',
          data: data.map(d => d.buy),
          backgroundColor: 'rgba(39, 174, 96, 0.6)',
          borderColor: '#27ae60', borderWidth: 1, order: 2,
        }},
        {{
          label: '매도',
          data: data.map(d => d.sell),
          backgroundColor: 'rgba(192, 57, 43, 0.6)',
          borderColor: '#c0392b', borderWidth: 1, order: 2,
        }},
        {{
          label: '순매수',
          type: 'line',
          data: data.map(d => d.net),
          borderColor: '#2980b9', backgroundColor: 'rgba(41, 128, 185, 0.1)',
          borderWidth: 2, tension: 0.25, pointRadius: 4, fill: false, order: 1,
        }},
      ],
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ position: 'bottom', labels: {{ font: {{ size: 11 }} }} }},
        tooltip: {{
          callbacks: {{
            label: c => `${{c.dataset.label}}: ${{c.parsed.y.toLocaleString()}} 억원`,
          }},
        }},
      }},
      scales: {{
        y: {{
          ticks: {{ callback: v => v.toLocaleString() + '억', font: {{ size: 10 }} }},
          grid: {{ color: 'rgba(0,0,0,0.06)' }},
        }},
        x: {{ ticks: {{ font: {{ size: 10 }} }}, grid: {{ display: false }} }},
      }},
    }},
  }});
}})();

// ============================================================
// 테마 페이지 (themes.html) — sector 별 차트 + 표 + 클릭시 종목 노출
// ============================================================
(function() {{
  if (!window.location.pathname.endsWith('themes.html')) return;
  const themeDataMulti = {theme_data_json};
  if (!themeDataMulti || !themeDataMulti.today) return;

  // 현재 활성 기간 (오늘 default)
  let currentPeriod = 'today';
  let currentData = themeDataMulti.today;

  const fmtBillion = (n) => {{
    const abs = Math.abs(n);
    if (abs >= 1e12) return (n/1e12).toFixed(1) + '조';
    if (abs >= 1e8) return (n/1e8).toFixed(1) + '억';
    if (abs >= 1e4) return (n/1e4).toFixed(0) + '만';
    return n.toLocaleString() + '원';
  }};
  const fmtNum = n => (n || 0).toLocaleString();

  // ---- 매수/매도 차트 두 개 분리 ----
  let chartBuy = null;
  let chartSell = null;

  function makeOneChart(canvasId, data, color, isBuy) {{
    const ctx = document.getElementById(canvasId);
    if (!ctx || typeof Chart === 'undefined' || data.length === 0) return null;
    return new Chart(ctx, {{
      type: 'bar',
      data: {{
        labels: data.map(s => s.sector),
        datasets: [{{
          label: isBuy ? '순매수 (억원)' : '순매도 (억원)',
          data: data.map(s => {{
            const eok = Math.round(Math.abs(s.net || 0) / 1e8 * 10) / 10;
            return isBuy ? eok : -eok;   // 매도는 음수로 → 막대 아래로
          }}),
          backgroundColor: color.bg,
          borderColor: color.border,
          borderWidth: 1,
          barPercentage: 0.55,
          categoryPercentage: 0.85,
          borderRadius: 4,
        }}],
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        onClick: (e, items) => {{
          if (items && items.length > 0) {{
            selectTheme(data[items[0].index].sector);
          }}
        }},
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            callbacks: {{
              label: c => `${{isBuy ? '순매수' : '순매도'}} ${{Math.abs(c.parsed.y).toLocaleString()}} 억원`,
            }},
          }},
        }},
        scales: {{
          y: {{
            ticks: {{ callback: v => v.toLocaleString() + '억', font: {{ size: 10 }} }},
            grid: {{ color: 'rgba(0,0,0,0.06)' }},
            beginAtZero: true,
            ...(isBuy ? {{ min: 0 }} : {{ max: 0 }}),
          }},
          x: {{
            position: isBuy ? 'bottom' : 'top',   // 매도는 라벨이 위
            ticks: {{ font: {{ size: 10 }}, maxRotation: 35, minRotation: 25 }},
            grid: {{ display: false }},
          }},
        }},
      }},
    }});
  }}

  function renderCharts(sectors) {{
    // 기존 차트 destroy
    if (chartBuy)  {{ chartBuy.destroy();  chartBuy = null; }}
    if (chartSell) {{ chartSell.destroy(); chartSell = null; }}
    const buyData = sectors.filter(s => (s.net || 0) > 0).slice(0, 10);
    const sellData = sectors.filter(s => (s.net || 0) < 0).slice(-10).reverse();
    chartBuy  = makeOneChart('theme-chart-buy',  buyData,
      {{ bg: 'rgba(231, 76, 60, 0.75)', border: '#c0392b' }}, true);
    chartSell = makeOneChart('theme-chart-sell', sellData,
      {{ bg: 'rgba(52, 152, 219, 0.75)', border: '#2980b9' }}, false);
  }}

  // ---- 요약 표 ----
  const summaryBody = document.getElementById('theme-summary-body');
  function renderSummaryTable(sectors) {{
    if (!summaryBody) return;
    if (!sectors || sectors.length === 0) {{
      summaryBody.innerHTML = '<tr><td colspan="8" class="empty">이 기간 데이터 없음 (DB 누적 후 표시)</td></tr>';
      return;
    }}
    summaryBody.innerHTML = sectors.map((s, i) => `
      <tr data-sector="${{s.sector}}">
        <td>${{i + 1}}</td>
        <td>${{s.sector}}</td>
        <td class="num" data-value="${{s.count || 0}}">${{s.count || 0}}</td>
        <td class="num" data-value="${{s.buy || 0}}">${{fmtBillion(s.buy || 0)}}</td>
        <td class="num" data-value="${{s.sell || 0}}">${{fmtBillion(s.sell || 0)}}</td>
        <td class="num ${{(s.net || 0) >= 0 ? 'pos' : 'neg'}}" data-value="${{s.net || 0}}">${{fmtBillion(s.net || 0)}}</td>
        <td class="num pos" data-value="${{s.buy_stocks || 0}}">${{s.buy_stocks || 0}}</td>
        <td class="num neg" data-value="${{s.sell_stocks || 0}}">${{s.sell_stocks || 0}}</td>
      </tr>
    `).join('');
  }}
  if (summaryBody) {{
    summaryBody.addEventListener('click', e => {{
      const tr = e.target.closest('tr[data-sector]');
      if (!tr) return;
      selectTheme(tr.dataset.sector);
    }});
  }}

  // ---- 기간 변경 ----
  function switchPeriod(period) {{
    currentPeriod = period;
    currentData = themeDataMulti[period] || {{sectors: [], stocks_by_sector: {{}}}};
    const sectors = currentData.sectors || [];

    // 미분류 비중 계산 (어제/7일은 sector 누적 전이라 미분류 위주일 수 있음)
    const totalAbs = sectors.reduce((s, x) => s + Math.abs(x.net || 0), 0);
    const uncatRow = sectors.find(x => x.sector === '기타');
    const uncatShare = (uncatRow && totalAbs > 0)
      ? (Math.abs(uncatRow.net || 0) / totalAbs * 100).toFixed(0)
      : 0;

    // 안내 텍스트 동적 갱신
    const note = document.querySelector('.theme-note');
    if (note) {{
      if (period === 'today') {{
        note.innerHTML = '0 라인 위 = 매수(빨강), 아래 = 매도(파랑). 막대 또는 우측 표 행 클릭 → 하단 종목.';
      }} else if (period === 'yesterday') {{
        if (sectors.length <= 1 || uncatShare >= 80) {{
          note.innerHTML = `⚠ <b>어제</b> — sector 정보는 오늘 빌드부터 DB 저장 시작. 어제 데이터는 sector 정보가 없어 <b>"기타" 위주</b> (${{uncatShare}}%) 로 표시됨. <b>다음 영업일부터 정상</b>.`;
        }} else {{
          note.innerHTML = '📆 어제 sector 별 집계 (기타 비중: ' + uncatShare + '%)';
        }}
      }} else if (period === 'week') {{
        if (uncatShare >= 50) {{
          note.innerHTML = `⚠ <b>최근 7거래일</b> — sector 정보 누적 시작 중. 현재 기타 비중 ${{uncatShare}}%. <b>약 1주 후</b> 모든 종목 정상 분류.`;
        }} else {{
          note.innerHTML = '📊 최근 7거래일 sector 별 누적 (기타: ' + uncatShare + '%)';
        }}
      }}
    }}

    // 차트 두 개 갱신 (매수/매도)
    renderCharts(sectors);
    // 표 갱신
    renderSummaryTable(sectors);
    // 종목 표: 1위 sector 자동 선택 (어제/7일도 stocks 데이터 있음)
    if (sectors.length > 0) {{
      selectTheme(sectors[0].sector);
    }} else {{
      document.getElementById('selected-theme-name').textContent = '데이터 없음';
      document.getElementById('theme-stocks-body').innerHTML = '<tr><td colspan="8" class="empty">데이터 없음</td></tr>';
    }}
  }}

  // 탭 클릭 핸들러
  document.querySelectorAll('.theme-period-tab').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.theme-period-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      switchPeriod(btn.dataset.period);
    }});
  }});

  // 초기 변수
  const sectors = currentData.sectors || [];
  const stocksBy = currentData.stocks_by_sector || {{}};

  // ---- 종목 표 ----
  function selectTheme(sec) {{
    document.getElementById('selected-theme-name').textContent = sec;
    document.querySelectorAll('.theme-summary-table tr[data-sector]').forEach(tr => {{
      tr.classList.toggle('selected', tr.dataset.sector === sec);
    }});
    // currentData 의 stocks_by_sector 사용 (기간 토글 반영)
    const stocks = (currentData.stocks_by_sector || {{}})[sec] || [];
    const body = document.getElementById('theme-stocks-body');
    if (!body) return;
    if (stocks.length === 0) {{
      body.innerHTML = '<tr><td colspan="8" class="empty">이 테마에 해당하는 종목이 없습니다</td></tr>';
      return;
    }}
    body.innerHTML = stocks.map((s, i) => {{
      const netCls = s.net >= 0 ? 'pos' : 'neg';
      const code = String(s.code).replace(/[<>"']/g, '');
      const name = String(s.name).replace(/[<>]/g, '');
      const period = (s.period_start && s.period_end)
        ? `${{s.period_start.slice(5, 10).replace('-', '.')}}~${{s.period_end.slice(5, 10).replace('-', '.')}}`
        : '';
      const periodAvg = s.period_buy_avg > 0
        ? `${{fmtNum(s.period_buy_avg)}} <span style="font-size:11px;color:#7f8c8d;">(${{period}})</span>`
        : '-';
      return `
        <tr class="stock-row" data-stock-code="${{code}}" data-stock-name="${{name}}">
          <td>${{i + 1}}</td>
          <td class="code">
            <span class="fav-star" data-stock="${{code}}">☆</span>${{code}}
          </td>
          <td class="name">${{name}}</td>
          <td class="num ${{netCls}}" data-value="${{s.net}}">${{fmtBillion(s.net)}}</td>
          <td class="num" data-value="${{s.net_to_cap}}">${{s.net_to_cap.toFixed(3)}}%</td>
          <td class="num">${{s.today_buy_avg > 0 ? fmtNum(s.today_buy_avg) : '-'}}</td>
          <td class="num">${{periodAvg}}</td>
          <td class="actions">
            <a href="https://tossinvest.com/stocks/A${{code}}/order" target="_blank" class="btn-trade" title="토스증권 주문창">매매</a>
          </td>
        </tr>
      `;
    }}).join('');
    // 즐겨찾기 별 상태 다시 적용
    if (typeof applyFavStars === 'function') applyFavStars();
    // 검색 필터도 다시 적용
    if (typeof applyStockFilter === 'function') applyStockFilter();
  }}

  // 초기 렌더: 오늘 데이터로 차트/표/종목 모두 그림
  switchPeriod('today');
}})();

// ============================================================
// AI 페이지 좌우 분할 — 행 클릭 → 우측 카드 렌더
// ============================================================
(function() {{
  if (!window.location.pathname.endsWith('ai.html')) return;
  const aiCards = {ai_cards_data_json};
  if (!aiCards || Object.keys(aiCards).length === 0) return;

  const cardContent = document.getElementById('ai-card-content');
  if (!cardContent) return;

  const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const fmtWon = n => {{
    const abs = Math.abs(n || 0);
    const sign = n < 0 ? '-' : '';
    if (abs >= 1e12) return sign + (abs/1e12).toFixed(1) + '조';
    if (abs >= 1e8) return sign + (abs/1e8).toFixed(1) + '억';
    if (abs >= 1e4) return sign + (abs/1e4).toFixed(0) + '만';
    return sign + abs.toLocaleString() + '원';
  }};
  const scoreLabel = (s) => {{
    const v = `${{s >= 0 ? '+' : ''}}${{s.toFixed(1)}}`;
    if (s >= 50)  return `<span class="score-grade s-aplus">★★★ ${{v}}</span>`;
    if (s >= 20)  return `<span class="score-grade s-a">★★ ${{v}}</span>`;
    if (s >= 5)   return `<span class="score-grade s-b">★ ${{v}}</span>`;
    if (s <= -50) return `<span class="score-grade s-fminus">▼▼▼ ${{v}}</span>`;
    if (s <= -20) return `<span class="score-grade s-f">▼▼ ${{v}}</span>`;
    if (s <= -5)  return `<span class="score-grade s-d">▼ ${{v}}</span>`;
    return `<span class="score-grade s-c">${{v}}</span>`;
  }};

  function renderCard(code) {{
    const d = aiCards[code];
    if (!d) {{
      cardContent.innerHTML = '<div class="ai-card-empty">데이터 없음</div>';
      return;
    }}
    const reasonsHtml = (d.reasons || []).map(r => `<li>${{esc(r)}}</li>`).join('');
    const chgCls = d.change_rate > 0 ? 'pos' : (d.change_rate < 0 ? 'neg' : '');
    const chgSign = d.change_rate >= 0 ? '+' : '';
    const rsiText = (d.rsi == null) ? '' : ` · RSI ${{d.rsi.toFixed(1)}}`;
    // 변수 기여도 그리드
    const bd = d.breakdown || {{}};
    const bdHtml = Object.entries(bd).map(([k, v]) => {{
      const num = (typeof v === 'number') ? v : 0;
      const cls = num > 0 ? 'pos' : (num < 0 ? 'neg' : 'zero');
      const sign = num > 0 ? '+' : '';
      const txt = num === 0 ? '0' : `${{sign}}${{num.toFixed(1)}}`;
      return `<div><span>${{esc(k)}}</span><b class="${{cls}}">${{txt}}</b></div>`;
    }}).join('');
    cardContent.innerHTML = `
      <div class="ai-card ai-${{d.level}}">
        <div class="ai-card-head">
          <div class="ai-card-name">${{esc(d.name)}}
            <span class="ai-card-code">${{esc(d.code)}} · ${{esc(d.market)}}</span>
          </div>
          <div class="ai-card-score">${{scoreLabel(d.score)}}</div>
        </div>
        <div class="ai-card-meta">
          현재가 ${{(d.close || 0).toLocaleString()}}
          · <span class="${{chgCls}}">${{chgSign}}${{(d.change_rate || 0).toFixed(2)}}%</span>
          · 순매수 ${{fmtWon(d.net_amount)}}${{rsiText}}
        </div>
        <div class="ai-card-recommend">${{esc(d.recommend)}}</div>
        <div class="ai-card-reasons">
          <b>점수 산출 근거</b>
          <ul>${{reasonsHtml}}</ul>
        </div>
        <div class="ai-card-breakdown">
          <b>변수별 기여도 (총 ${{d.score >= 0 ? '+' : ''}}${{d.score.toFixed(1)}})</b>
          <div class="bd-grid">${{bdHtml}}</div>
        </div>
        <div class="ai-card-actions">
          <a href="https://tossinvest.com/stocks/A${{encodeURIComponent(d.code)}}/order" target="_blank" class="btn-trade" style="flex:1;text-align:center;padding:8px;">토스 주문창 (매수/매도)</a>
        </div>
      </div>
    `;
  }}

  // 행 클릭 핸들러 (event delegation)
  const tbody = document.querySelector('.ai-table tbody');
  if (tbody) {{
    tbody.addEventListener('click', e => {{
      // 별표/링크 클릭이면 카드 갱신 건너뜀
      if (e.target.closest('.fav-star')) return;
      if (e.target.closest('a')) return;
      const tr = e.target.closest('tr.stock-row');
      if (!tr) return;
      const code = tr.dataset.stockCode;
      if (!code) return;
      tbody.querySelectorAll('tr.selected').forEach(x => x.classList.remove('selected'));
      tr.classList.add('selected');
      renderCard(code);
    }});
  }}

  // 초기 선택: 첫 번째 행 (총점 desc 정렬되어있어 1위 종목)
  const firstRow = document.querySelector('.ai-table tbody tr.stock-row');
  if (firstRow) {{
    firstRow.classList.add('selected');
    renderCard(firstRow.dataset.stockCode);
  }}
}})();

// ============================================================
// 연속 누적 매수 페이지 (continuity.html)
// ============================================================
(function() {{
  if (!window.location.pathname.endsWith('continuity.html')) return;
  const items = {continuity_data_json};
  if (!items || items.length === 0) {{
    const tbody = document.getElementById('continuity-table-body');
    if (tbody) tbody.innerHTML = '<tr><td colspan="9" class="empty">연속 매수 종목 없음 (구간 10일↑ 종목 없음)</td></tr>';
    return;
  }}

  const BURN_INFO = {{
    burning:  {{ icon: '🔥', label: '불타기', color: 'rgba(231, 76, 60, 0.8)',  border: '#c0392b' }},
    watering: {{ icon: '💧', label: '물타기', color: 'rgba(52, 152, 219, 0.8)', border: '#2980b9' }},
    neutral:  {{ icon: '━',  label: '보합',   color: 'rgba(149, 165, 166, 0.8)', border: '#7f8c8d' }},
    unknown:  {{ icon: '❓', label: '판단불가', color: 'rgba(189, 195, 199, 0.8)', border: '#95a5a6' }},
  }};

  const fmtWon = n => {{
    const abs = Math.abs(n || 0);
    if (abs >= 1e8) return (abs/1e8).toFixed(1) + '억';
    if (abs >= 1e4) return (abs/1e4).toFixed(0) + '만';
    return abs.toLocaleString() + '원';
  }};
  const fmtPeriod = (s, e) => {{
    if (!s || !e) return '';
    const f = d => d.length >= 10 ? d.slice(5, 10).replace('-', '.') : d;
    return `${{f(s)}}~${{f(e)}}`;
  }};

  // ---- 차트: Top 20 가로 막대 ----
  const ctx = document.getElementById('continuity-chart');
  if (ctx && typeof Chart !== 'undefined') {{
    const top = items.slice(0, 20);
    const labels = top.map(d => `${{BURN_INFO[d.burn].icon}} ${{d.name}}`);
    const bgColors = top.map(d => BURN_INFO[d.burn].color);
    const borderColors = top.map(d => BURN_INFO[d.burn].border);
    new Chart(ctx, {{
      type: 'bar',
      data: {{
        labels: labels,
        datasets: [{{
          label: '구간 누적 순매수 (원)',
          data: top.map(d => d.cumul),
          backgroundColor: bgColors,
          borderColor: borderColors,
          borderWidth: 1,
          borderRadius: 4,
        }}],
      }},
      options: {{
        indexAxis: 'y',
        responsive: true, maintainAspectRatio: false,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{
            callbacks: {{
              title: items => {{
                const idx = items[0].dataIndex;
                const d = top[idx];
                return `${{BURN_INFO[d.burn].icon}} ${{d.name}} (${{d.code}})`;
              }},
              label: c => {{
                const d = top[c.dataIndex];
                const avgInfo = (d.today_avg > 0 && d.period_avg > 0)
                  ? ` · ${{BURN_INFO[d.burn].label}} (오늘 ${{d.today_avg.toLocaleString()}} / 구간 ${{d.period_avg.toLocaleString()}})`
                  : '';
                return [
                  `구간 누적 순매수: ${{c.parsed.x.toLocaleString()}} 원`,
                  `연속(거래일): ${{d.days}}일`,
                  avgInfo,
                ].filter(s => s);
              }},
            }},
          }},
        }},
        scales: {{
          x: {{
            ticks: {{ callback: v => fmtWon(v), font: {{ size: 10 }} }},
            grid: {{ color: 'rgba(0,0,0,0.06)' }},
            beginAtZero: true,
          }},
          y: {{
            ticks: {{ font: {{ size: 11 }} }},
            grid: {{ display: false }},
          }},
        }},
      }},
    }});
  }}

  // ---- 표: 전체 종목 ----
  const tbody = document.getElementById('continuity-table-body');
  if (tbody) {{
    tbody.innerHTML = items.map((d, i) => {{
      const info = BURN_INFO[d.burn];
      const code = String(d.code).replace(/[<>"']/g, '');
      const name = String(d.name).replace(/[<>]/g, '');
      const avgInfo = (d.period_avg > 0)
        ? `<span class="avg-info">· 누적평단 ${{d.period_avg.toLocaleString()}}</span>`
        : '';
      const todayAvgCell = d.today_avg > 0 ? d.today_avg.toLocaleString() : '-';
      return `
        <tr class="stock-row" data-stock-code="${{code}}" data-stock-name="${{name}}">
          <td>${{i + 1}}</td>
          <td class="code">
            <span class="fav-star" data-stock="${{code}}">☆</span>${{code}}
          </td>
          <td class="name">${{name}}</td>
          <td class="num" data-value="${{d.days}}"><b style="color:#c0392b;">${{d.days}}</b></td>
          <td>${{fmtPeriod(d.period_start, d.period_end)}}</td>
          <td class="burn-cell">
            <span class="burn-icon burn-${{d.burn}}">${{info.icon}}</span>
            <span class="burn-${{d.burn}}"><b>${{info.label}}</b></span>
            ${{avgInfo}}
          </td>
          <td class="num">${{todayAvgCell}}</td>
          <td class="num pos" data-value="${{d.cumul}}"><b>${{fmtWon(d.cumul)}}</b></td>
          <td class="actions">
            <a href="https://tossinvest.com/stocks/A${{code}}/order" target="_blank" class="btn-trade">매매</a>
          </td>
        </tr>
      `;
    }}).join('');
    // 즐겨찾기 별 상태 적용
    if (typeof applyFavStars === 'function') applyFavStars();
    if (typeof applyStockFilter === 'function') applyStockFilter();
  }}
}})();
</script>

<script type="module">
// ============================================================
// Firebase 익명 커뮤니티 (닉네임 + 댓글 + @멘션)
// ============================================================
import {{ initializeApp }} from "https://www.gstatic.com/firebasejs/10.7.1/firebase-app.js";
import {{ getAuth, signInAnonymously }} from "https://www.gstatic.com/firebasejs/10.7.1/firebase-auth.js";
import {{
  getFirestore, collection, addDoc, doc, deleteDoc, updateDoc,
  query, orderBy, limit, onSnapshot, serverTimestamp
}} from "https://www.gstatic.com/firebasejs/10.7.1/firebase-firestore.js";

const firebaseConfig = {{
  apiKey: "AIzaSyCRsyngKyS36agwoiEy9720WbEVq0qCT1g",
  authDomain: "report-ygg-d53f1.firebaseapp.com",
  projectId: "report-ygg-d53f1",
  storageBucket: "report-ygg-d53f1.firebasestorage.app",
  messagingSenderId: "183908458182",
  appId: "1:183908458182:web:63b671cb6be9371dac81c7"
}};

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const db = getFirestore(app);

let currentUid = null;
signInAnonymously(auth).then(c => {{ currentUid = c.user.uid; }}).catch(e => console.warn("anon auth failed", e));

// ---------- 닉네임 ----------
const NICK_KEY = 'report-ygg-nick';
const nickInput = document.getElementById('nick-input');
const nickSave = document.getElementById('nick-save');
nickInput.value = localStorage.getItem(NICK_KEY) || '';
if (nickInput.value) {{ nickSave.textContent = '변경'; nickSave.classList.add('nick-saved'); }}
nickSave.addEventListener('click', () => {{
  const v = (nickInput.value || '').trim().slice(0, 20);
  if (!v) {{ alert('닉네임을 입력하세요'); return; }}
  localStorage.setItem(NICK_KEY, v);
  nickSave.textContent = '변경';
  nickSave.classList.add('nick-saved');
}});
function getNick() {{
  return localStorage.getItem(NICK_KEY) || '익명';
}}

// ---------- 멘션 추출 + 렌더 ----------
function extractMentions(text) {{
  const matches = text.match(/@[\\uac00-\\ud7a3A-Za-z0-9]+/g);
  return matches ? [...new Set(matches.map(m => m.slice(1)))] : [];
}}
function renderText(text) {{
  // @멘션을 .mention span 으로 감쌈, 그 외엔 escape
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  return esc(text).replace(/@([\\uac00-\\ud7a3A-Za-z0-9]+)/g,
    '<span class="mention" data-stock="$1">@$1</span>');
}}
function fmtTime(ts) {{
  if (!ts || !ts.toDate) return '';
  const d = ts.toDate();
  const now = new Date();
  const diff = Math.floor((now - d) / 1000);
  if (diff < 60) return '방금';
  if (diff < 3600) return Math.floor(diff/60) + '분 전';
  if (diff < 86400) return Math.floor(diff/3600) + '시간 전';
  return d.toLocaleDateString('ko-KR', {{ month: 'numeric', day: 'numeric' }});
}}

// ---------- 새 글 작성 (contenteditable 실시간 멘션) ----------
const threadText = document.getElementById('thread-text');
const threadSubmit = document.getElementById('thread-submit');

// caret 위치(텍스트 오프셋) 읽기/쓰기
function getCaretCharOffset(el) {{
  const sel = window.getSelection();
  if (!sel.rangeCount) return 0;
  const range = sel.getRangeAt(0);
  const pre = range.cloneRange();
  pre.selectNodeContents(el);
  pre.setEnd(range.endContainer, range.endOffset);
  return pre.toString().length;
}}
function setCaretCharOffset(el, offset) {{
  const range = document.createRange();
  const sel = window.getSelection();
  let charCount = 0, found = false;
  function walk(node) {{
    if (found) return;
    if (node.nodeType === Node.TEXT_NODE) {{
      const next = charCount + node.length;
      if (offset <= next) {{
        range.setStart(node, Math.max(0, offset - charCount));
        range.collapse(true);
        found = true;
        return;
      }}
      charCount = next;
    }} else {{
      for (const child of node.childNodes) walk(child);
    }}
  }}
  walk(el);
  if (!found) {{
    range.selectNodeContents(el);
    range.collapse(false);
  }}
  sel.removeAllRanges();
  sel.addRange(range);
}}

function renderMentionsHTML(text) {{
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  return esc(text).replace(/@([\\uac00-\\ud7a3A-Za-z0-9]+)/g,
    '<span class="mention" data-stock="$1">@$1</span>');
}}

let imeComposing = false;
function updateMentionHighlight() {{
  if (imeComposing) return;  // 한글 IME 조합 중엔 갱신 안 함 (입력 깨짐 방지)
  const text = threadText.innerText;
  if (text.length > 1000) {{
    threadText.innerText = text.slice(0, 1000);
  }}
  const caret = getCaretCharOffset(threadText);
  const newHTML = renderMentionsHTML(text);
  if (threadText.innerHTML !== newHTML) {{
    threadText.innerHTML = newHTML;
    setCaretCharOffset(threadText, caret);
  }}
}}

threadText.addEventListener('compositionstart', () => {{ imeComposing = true; }});
threadText.addEventListener('compositionend', () => {{
  imeComposing = false;
  updateMentionHighlight();
}});
threadText.addEventListener('input', e => {{
  if (e.isComposing || imeComposing) return;
  updateMentionHighlight();
}});

// Enter는 줄바꿈, Ctrl/Cmd+Enter = 게시
threadText.addEventListener('keydown', e => {{
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {{
    e.preventDefault();
    threadSubmit.click();
  }}
}});

threadSubmit.addEventListener('click', async () => {{
  const text = (threadText.innerText || '').trim();
  if (!text) return;
  if (!currentUid) {{ alert('인증 중... 잠시 후 다시 시도'); return; }}
  threadSubmit.disabled = true;
  try {{
    await addDoc(collection(db, 'threads'), {{
      nickname: getNick(),
      text: text.slice(0, 1000),
      mentions: extractMentions(text),
      uid: currentUid,
      createdAt: serverTimestamp()
    }});
    threadText.innerHTML = '';
  }} catch (e) {{
    console.error(e);
    alert('게시 실패: ' + e.message);
  }} finally {{
    threadSubmit.disabled = false;
  }}
}});

// ---------- 글 목록 실시간 listen ----------
const threadList = document.getElementById('thread-list');
const repliesCache = {{}};   // threadId → unsubscribe fn

function renderThread(id, data) {{
  const div = document.createElement('div');
  div.className = 'thread-item';
  div.id = 'th-' + id;
  div.dataset.rawText = data.text || '';
  div.dataset.uid = data.uid || '';
  const isMine = currentUid && data.uid === currentUid;
  const editBtns = isMine
    ? `<span class="thread-actions"><a class="th-edit" data-id="${{id}}">✏</a><a class="th-del" data-id="${{id}}">🗑</a></span>`
    : '';
  div.innerHTML = `
    <div class="thread-head">
      <span class="thread-nick">${{(data.nickname || '익명').replace(/[<>]/g,'')}}</span>
      <span class="thread-meta">
        <span class="thread-time">${{fmtTime(data.createdAt)}}${{data.editedAt ? ' (수정)' : ''}}</span>
        ${{editBtns}}
      </span>
    </div>
    <div class="thread-text">${{renderText(data.text || '')}}</div>
    <span class="reply-toggle" data-id="${{id}}">↳ 답글</span>
    <div class="reply-section" style="display:none;">
      <div class="reply-list" id="rl-${{id}}"></div>
      <div class="reply-compose">
        <input type="text" maxlength="500" placeholder="답글" />
        <button>등록</button>
      </div>
    </div>
  `;
  return div;
}}

function attachReplyHandlers(threadEl, threadId) {{
  const toggle = threadEl.querySelector('.reply-toggle');
  const section = threadEl.querySelector('.reply-section');
  toggle.addEventListener('click', () => {{
    const opened = section.style.display !== 'none';
    section.style.display = opened ? 'none' : 'block';
    if (!opened && !repliesCache[threadId]) {{
      // 답글 listen 시작
      const rq = query(collection(db, 'threads', threadId, 'replies'), orderBy('createdAt', 'asc'), limit(50));
      repliesCache[threadId] = onSnapshot(rq, snap => {{
        const rl = document.getElementById('rl-' + threadId);
        if (!rl) return;
        rl.innerHTML = '';
        snap.forEach(d => {{
          const r = d.data();
          const ri = document.createElement('div');
          ri.className = 'reply-item';
          ri.dataset.rawText = r.text || '';
          ri.dataset.replyId = d.id;
          ri.dataset.threadId = threadId;
          const isMine = currentUid && r.uid === currentUid;
          const actions = isMine
            ? `<span class="reply-actions"><a class="rp-edit">✏</a><a class="rp-del">🗑</a></span>`
            : '';
          ri.innerHTML = `<span class="reply-nick">${{(r.nickname || '익명').replace(/[<>]/g,'')}}</span><span class="reply-text">${{renderText(r.text || '')}}</span>${{actions}}`;
          rl.appendChild(ri);
        }});
      }});
    }}
  }});
  const replyBtn = threadEl.querySelector('.reply-compose button');
  const replyInput = threadEl.querySelector('.reply-compose input');
  replyBtn.addEventListener('click', async () => {{
    const text = (replyInput.value || '').trim();
    if (!text || !currentUid) return;
    replyBtn.disabled = true;
    try {{
      await addDoc(collection(db, 'threads', threadId, 'replies'), {{
        nickname: getNick(),
        text: text.slice(0, 500),
        uid: currentUid,
        createdAt: serverTimestamp()
      }});
      replyInput.value = '';
    }} catch (e) {{
      alert('답글 실패: ' + e.message);
    }} finally {{
      replyBtn.disabled = false;
    }}
  }});
}}

// ---------- 필터링 + 글 목록 ----------
let allThreads = [];
let currentFilter = null;  // 활성 종목 멘션 필터

const filterContainer = document.getElementById('stock-filter-container');

function setStockFilter(stockName) {{
  currentFilter = stockName;
  if (stockName) {{
    filterContainer.innerHTML = `
      <div class="stock-filter">
        <span>📌 @${{stockName.replace(/[<>]/g,'')}} 종목 글만 표시</span>
        <button class="clear-btn" id="filter-clear">전체 보기</button>
      </div>
    `;
    document.getElementById('filter-clear').addEventListener('click', () => setStockFilter(null));
  }} else {{
    filterContainer.innerHTML = '';
  }}
  renderThreadList();
}}

function renderThreadList() {{
  threadList.innerHTML = '';
  const items = currentFilter
    ? allThreads.filter(t => (t.mentions || []).includes(currentFilter))
    : allThreads;
  if (items.length === 0) {{
    threadList.innerHTML = '<div class="thread-loading">'
      + (currentFilter ? `@${{currentFilter}} 관련 글이 없습니다.` : '아직 글이 없습니다. 첫 글을 남겨보세요.')
      + '</div>';
    return;
  }}
  items.forEach(t => {{
    const el = renderThread(t.id, t);
    threadList.appendChild(el);
    attachReplyHandlers(el, t.id);
  }});
}}

// @멘션 클릭 → 필터 설정 (이벤트 위임)
threadList.addEventListener('click', async e => {{
  // 멘션 필터
  const m = e.target.closest('.mention');
  if (m && m.dataset.stock) {{
    setStockFilter(m.dataset.stock);
    return;
  }}
  // 글 수정
  const edit = e.target.closest('.th-edit');
  if (edit) {{
    const id = edit.dataset.id;
    const item = document.getElementById('th-' + id);
    const oldText = item.dataset.rawText || '';
    const newText = prompt('글 수정:', oldText);
    if (newText === null || newText.trim() === oldText) return;
    try {{
      await updateDoc(doc(db, 'threads', id), {{
        text: newText.trim().slice(0, 1000),
        mentions: extractMentions(newText),
        editedAt: serverTimestamp()
      }});
    }} catch (err) {{ alert('수정 실패: ' + err.message); }}
    return;
  }}
  // 글 삭제
  const del = e.target.closest('.th-del');
  if (del) {{
    if (!confirm('이 글을 삭제하시겠습니까? (답글 포함)')) return;
    try {{
      await deleteDoc(doc(db, 'threads', del.dataset.id));
    }} catch (err) {{ alert('삭제 실패: ' + err.message); }}
    return;
  }}
  // 답글 수정
  const redit = e.target.closest('.rp-edit');
  if (redit) {{
    const ri = redit.closest('.reply-item');
    const oldText = ri.dataset.rawText || '';
    const newText = prompt('답글 수정:', oldText);
    if (newText === null || newText.trim() === oldText) return;
    try {{
      await updateDoc(doc(db, 'threads', ri.dataset.threadId, 'replies', ri.dataset.replyId), {{
        text: newText.trim().slice(0, 500),
        editedAt: serverTimestamp()
      }});
    }} catch (err) {{ alert('수정 실패: ' + err.message); }}
    return;
  }}
  // 답글 삭제
  const rdel = e.target.closest('.rp-del');
  if (rdel) {{
    if (!confirm('이 답글을 삭제하시겠습니까?')) return;
    const ri = rdel.closest('.reply-item');
    try {{
      await deleteDoc(doc(db, 'threads', ri.dataset.threadId, 'replies', ri.dataset.replyId));
    }} catch (err) {{ alert('삭제 실패: ' + err.message); }}
    return;
  }}
}});

const tq = query(collection(db, 'threads'), orderBy('createdAt', 'desc'), limit(100));
onSnapshot(tq, snap => {{
  allThreads = [];
  snap.forEach(d => allThreads.push({{ id: d.id, ...d.data() }}));
  renderThreadList();
}}, err => {{
  threadList.innerHTML = '<div class="thread-loading">로드 실패: ' + err.message + '</div>';
}});
</script>

</body>
</html>
"""


# ==========================================================================
# 엔트리포인트
# ==========================================================================

def _should_skip_build() -> tuple:
    """
    한국 영업시간 (평일 09:00 ~ 17:59 KST) + 공휴일 체크.
    return: (skip: bool, reason: str)
    환경변수 FORCE_BUILD=1 이면 항상 False (긴급 빌드용).
    """
    if os.environ.get("FORCE_BUILD") == "1":
        return False, ""
    now = dt.datetime.now()   # workflow.yml 에서 TZ=Asia/Seoul 설정
    weekday = now.weekday()   # 0=Mon, 6=Sun
    hour = now.hour
    dow_names = ['월', '화', '수', '목', '금', '토', '일']
    if weekday >= 5:
        return True, f"주말 ({dow_names[weekday]}요일)"
    if hour < 9 or hour >= 18:
        return True, f"영업시간 외 (KST {hour:02d}시 — 09~17시만 빌드)"
    try:
        import holidays
        kr = holidays.KR(years=now.year)
        if now.date() in kr:
            holiday_name = kr.get(now.date(), '공휴일')
            return True, f"한국 공휴일: {holiday_name}"
    except ImportError:
        log.warning("holidays 패키지 미설치 → 공휴일 체크 skip")
    return False, ""


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # ────────────────────────────────────────────────────────────────────────
    # 영업일/시간 체크: 평일 09 ~ 17 KST + 공휴일 제외. (FORCE_BUILD=1 우회)
    # ────────────────────────────────────────────────────────────────────────
    skip, reason = _should_skip_build()
    if skip:
        log.info("=== 빌드 SKIP: %s ===", reason)
        print(f"[SKIP] {reason}")
        return

    parser = argparse.ArgumentParser()
    parser.add_argument("date", nargs="?", help="YYYYMMDD (기본: 직전 영업일 — auto fetch 모드에선 todayygg가 정한 날짜로 덮어씀)")
    parser.add_argument("--markets", default="KOSPI,KOSDAQ", help="콤마 구분, 예: KOSPI,KOSDAQ")
    parser.add_argument("--no-auto", action="store_true",
                        help="자동 fetch(todayygg+judal) 끄고 CSV input 만 사용")
    parser.add_argument("--mode", choices=["realtime", "closing"], default="realtime",
                        help="realtime=hourly 갱신(realtime.html+index.html), closing=15:30 마감 1회(closing.html)")
    args = parser.parse_args()

    if args.date:
        trade_date = args.date
    else:
        trade_date = krx_http.latest_business_date().strftime("%Y%m%d")

    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    use_auto = not args.no_auto
    mode = args.mode

    log.info("=== 리포트 빌드 시작: %s, mode=%s, markets=%s, auto=%s ===",
             trade_date, mode, markets, use_auto)
    payload = collect_one_day(trade_date, markets=markets, use_auto=use_auto, mode=mode)
    log.info("collected: %d rows, %d markets, source=%s",
             len(payload["rows"]), len(payload["markets"]), payload.get("source", "?"))
    # RSI 14일 attach (DB 시계열 + 오늘 close 사용)
    try:
        attach_rsi_to_rows(payload["rows"], period=14)
        rsi_n = sum(1 for r in payload["rows"] if r.get("rsi") is not None)
        log.info("RSI 계산: %d/%d 종목 (시계열 충분)", rsi_n, len(payload["rows"]))
    except Exception as e:
        log.warning("RSI attach 실패: %s", e)

    # HTML (모드별 파일명)
    # 정책: realtime 빌드는 realtime.html + index.html.
    #       closing.html은 trading-trend 제외한 별도 fetch로 생성 (마감 기준 데이터 정확성).
    if mode == "realtime":
        realtime_html = render_html(payload, mode="realtime")
        for fname in ("realtime.html", "index.html"):
            (OUTPUT_DIR / fname).write_text(realtime_html, encoding="utf-8")
            log.info("HTML written: %s", OUTPUT_DIR / fname)
        # ai.html — realtime payload 그대로 사용해서 ai 모드로 렌더
        ai_html_out = render_html(payload, mode="ai")
        (OUTPUT_DIR / "ai.html").write_text(ai_html_out, encoding="utf-8")
        log.info("HTML written (ai): %s", OUTPUT_DIR / "ai.html")

        # bulk.html — 5%룰 (DART) + 10만주↑ 매매 (DB)
        # DART majorstock 은 종목 수십~수백 종목 × 1API호출 → 디스크 캐시로 부담 줄임
        try:
            from report import dart
            traded_codes = sorted(query_traded_stock_codes(days=90))
            log.info("bulk 빌드: DB 90일 매매 종목 %d개 대상 DART 5%%룰 fetch...", len(traded_codes))
            majorstock_cache = str((ROOT / "data" / "dart_majorstock_cache.json").resolve())
            majorstock_all = dart.fetch_major_holdings_bulk(
                traded_codes, cache_path=majorstock_cache, cache_ttl_hours=12,
            )
            nps_holdings = dart.filter_nps_holdings(majorstock_all)
            log.info("bulk: 5%%룰 종목 %d개, 국민연금 보고 종목 %d개",
                     len(majorstock_all), len(nps_holdings))
            payload_bulk = {**payload, "nps_holdings": nps_holdings, "majorstock_all": majorstock_all}
        except Exception as e:
            log.exception("bulk 데이터 fetch 실패 — 빈 데이터로 진행: %s", e)
            payload_bulk = {**payload, "nps_holdings": {}, "majorstock_all": {}}
        bulk_html_out = render_html(payload_bulk, mode="bulk")
        (OUTPUT_DIR / "bulk.html").write_text(bulk_html_out, encoding="utf-8")
        log.info("HTML written (bulk): %s", OUTPUT_DIR / "bulk.html")

        # themes.html — sector 별 수급 (realtime payload 그대로 사용)
        themes_html_out = render_html(payload, mode="themes")
        (OUTPUT_DIR / "themes.html").write_text(themes_html_out, encoding="utf-8")
        log.info("HTML written (themes): %s", OUTPUT_DIR / "themes.html")

        # continuity.html — 연속 누적 매수 (구간 10일↑ 종목, 누적 desc)
        cont_html_out = render_html(payload, mode="continuity")
        (OUTPUT_DIR / "continuity.html").write_text(cont_html_out, encoding="utf-8")
        log.info("HTML written (continuity): %s", OUTPUT_DIR / "continuity.html")

        # closing.html — 별도 fetch (trading-trend 머지 X) 로 직전 영업일 마감 데이터
        log.info("closing 데이터 별도 fetch (trading-trend 제외)...")
        payload_closing = collect_one_day(trade_date, markets=markets, use_auto=use_auto, mode="closing")
        closing_html = render_html(payload_closing, mode="closing")
        (OUTPUT_DIR / "closing.html").write_text(closing_html, encoding="utf-8")
        log.info("HTML written (closing): %s [%d rows]",
                 OUTPUT_DIR / "closing.html", len(payload_closing.get("rows", [])))
    else:
        closing_html = render_html(payload, mode="closing")
        (OUTPUT_DIR / "closing.html").write_text(closing_html, encoding="utf-8")
        log.info("HTML written: %s", OUTPUT_DIR / "closing.html")
    out_path = OUTPUT_DIR / ("closing.html" if mode == "closing" else "index.html")

    # JSON 요약
    summary_path = OUTPUT_DIR / f"summary_latest_{mode}.json"
    summary_path.write_text(
        json.dumps({
            "trade_date": payload["trade_date"],
            "markets": payload["markets"],
            "row_count": len(payload["rows"]),
            "summaries": payload["summaries"],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # CSV 전체
    csv_path = OUTPUT_DIR / "pension_latest.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["trade_date", "market", "code", "name", "buy", "sell", "net", "market_cap", "net_to_cap_%"])
        for r in payload["rows"]:
            w.writerow([
                payload["trade_date"], r.get("market", ""),
                r["stock_code"], r["stock_name"],
                r["buy_amount"], r["sell_amount"], r["net_amount"],
                r.get("market_cap", 0), f"{r.get('net_to_cap', 0):.4f}",
            ])

    log.info("=== 완료: %s ===", out_path)
    print(f"\n[OK] {out_path}")


if __name__ == "__main__":
    main()
