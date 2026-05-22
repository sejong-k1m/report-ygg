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
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (trade_date, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_pdr_date ON pension_daily_report(trade_date);
CREATE INDEX IF NOT EXISTS idx_pdr_net  ON pension_daily_report(trade_date, net_amount);

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


def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(DB_SCHEMA)
    return conn


def upsert_pension_daily(conn, trade_date: str, market: str, rows: list, cap_map: dict):
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.cursor()
    for r in rows:
        if not r["stock_code"]:
            continue
        mcap = cap_map.get(r["stock_code"], 0)
        cur.execute("""
            INSERT INTO pension_daily_report
              (trade_date, stock_code, stock_name, market, buy_amount, sell_amount, net_amount,
               buy_qty, sell_qty, net_qty, market_cap, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
              fetched_at = excluded.fetched_at
        """, (
            trade_date, r["stock_code"], r["stock_name"], market,
            r["buy_amount"], r["sell_amount"], r["net_amount"],
            r["buy_qty"], r["sell_qty"], r["net_qty"],
            mcap, now,
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
    top_buy = _table_top_rows(rows, "net_amount", desc=True, n=30)
    top_sell = _table_top_rows(rows, "net_amount", desc=False, n=30)
    top_cap_buy = _table_top_rows(rows, "net_to_cap", desc=True, n=30)
    top_cap_sell = _table_top_rows(rows, "net_to_cap", desc=False, n=30)

    # 7거래일 추이 + 주간 누적 Top
    recent = query_recent_summaries(7, "KOSPI")
    weekly_buy = query_weekly_top(days=7, top_n=30, direction="buy")
    weekly_sell = query_weekly_top(days=7, top_n=30, direction="sell")

    def _toss_btns(code):
        return (
            f"<a href='{_toss_buy_url(code)}' target='_blank' class='btn-buy' title='토스증권 매수 호가창'>매수</a>"
            f"<a href='{_toss_sell_url(code)}' target='_blank' class='btn-sell' title='토스증권 매도 호가창'>매도</a>"
        )

    def _row_today(r, key="net_amount"):
        net = r.get(key, 0)
        chg = r.get("change_rate", 0)
        chg_class = "pos" if chg > 0 else ("neg" if chg < 0 else "")
        return (
            f"<tr>"
            f"<td class='code'>{_esc(r['stock_code'])}</td>"
            f"<td class='name'>{_esc(r['stock_name'])}</td>"
            f"<td class='num {'pos' if net >= 0 else 'neg'}' data-value='{net}'>{_fmt_won(net)}</td>"
            f"<td class='num' data-value='{r.get('buy_amount', 0)}'>{_fmt_won(r.get('buy_amount', 0))}</td>"
            f"<td class='num' data-value='{r.get('sell_amount', 0)}'>{_fmt_won(r.get('sell_amount', 0))}</td>"
            f"<td class='num {chg_class}' data-value='{chg}'>{_fmt_pct(chg)}</td>"
            f"<td class='num' data-value='{r.get('close_price', 0)}'>{r.get('close_price', 0):,}</td>"
            f"<td class='num' data-value='{r.get('net_to_cap', 0)}'>{_fmt_pct(r.get('net_to_cap', 0))}</td>"
            f"<td class='market'>{_esc(r.get('market', ''))}</td>"
            f"<td class='actions'>{_toss_btns(r['stock_code'])}</td>"
            f"</tr>"
        )

    def _row_weekly(r):
        return (
            f"<tr>"
            f"<td class='code'>{_esc(r['stock_code'])}</td>"
            f"<td class='name'>{_esc(r['stock_name'])}</td>"
            f"<td class='num {'pos' if r['net_sum'] >= 0 else 'neg'}' data-value='{r['net_sum']}'>{_fmt_won(r['net_sum'])}</td>"
            f"<td class='num' data-value='{r['buy_sum']}'>{_fmt_won(r['buy_sum'])}</td>"
            f"<td class='num' data-value='{r['sell_sum']}'>{_fmt_won(r['sell_sum'])}</td>"
            f"<td class='num' data-value='{r['day_count']}'>{r['day_count']}일</td>"
            f"<td class='market'>{_esc(r.get('market', ''))}</td>"
            f"<td class='actions'>{_toss_btns(r['stock_code'])}</td>"
            f"</tr>"
        )

    empty_today = "<tr><td colspan='10' class='empty'>데이터 없음</td></tr>"
    empty_weekly = "<tr><td colspan='8' class='empty'>히스토리 누적 중 (며칠 빌드 후 표시)</td></tr>"

    top_buy_html = "\n".join(_row_today(r) for r in top_buy) or empty_today
    top_sell_html = "\n".join(_row_today(r) for r in top_sell) or empty_today
    top_cap_buy_html = "\n".join(_row_today(r, "net_to_cap") for r in top_cap_buy) or empty_today
    top_cap_sell_html = "\n".join(_row_today(r, "net_to_cap") for r in top_cap_sell) or empty_today
    weekly_buy_html = "\n".join(_row_weekly(r) for r in weekly_buy) or empty_weekly
    weekly_sell_html = "\n".join(_row_weekly(r) for r in weekly_sell) or empty_weekly

    recent_html = "".join(
        f"<tr><td>{_esc(r['trade_date'])}</td>"
        f"<td class='num'>{_fmt_won(r['buy_total'])}</td>"
        f"<td class='num'>{_fmt_won(r['sell_total'])}</td>"
        f"<td class='num {'pos' if r['net_total'] >= 0 else 'neg'}'>{_fmt_won(r['net_total'])}</td></tr>"
        for r in recent
    ) or "<tr><td colspan='4' class='empty'>히스토리 없음 (오늘이 첫 실행)</td></tr>"

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
            f"<div class='top5-price'>{close:,}원</div>"
            f"<div class='top5-change {chg_cls}'>{arrow} {abs(change_amt):,} ({chg_pct:+.2f}%)</div>"
        ) if close > 0 else "<div class='top5-price-na'>현재가 N/A</div>"

        return (
            f"<div class='top5-card'>"
            f"<div class='top5-rank'>{r.get('_rank', '')}</div>"
            f"<div class='top5-head'>"
            f"  <div class='top5-name'>{_esc(r['stock_name'])}</div>"
            f"  <div class='top5-code'>{_esc(code)} · {_esc(r.get('market', ''))}</div>"
            f"</div>"
            f"{price_html}"
            f"<div class='top5-amount {amount_cls}'>{amount_label} {_fmt_won(amount_val)}</div>"
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
    auto_refresh_meta = '<meta http-equiv="refresh" content="3600">' if mode == "realtime" else ''

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
        # 다음 15분 단위 시각 계산
        cur_min = now.minute
        next_min = ((cur_min // 15) + 1) * 15
        if next_min >= 60:
            next_update_at = (now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)).strftime("%H:%M")
        else:
            next_update_at = now.replace(minute=next_min, second=0, microsecond=0).strftime("%H:%M")
        if intraday_hhmm:
            mode_subtitle = (
                f"⏱ 실시간 — 토스 trading-trend <b>{intraday_hhmm}</b> KST 기준 "
                f"· 페이지 빌드: {last_update_hhmm} "
                f"· 다음 갱신 예정: {next_update_at} "
                f"· 업데이트 주기: 15분"
            )
        else:
            mode_subtitle = (
                f"⏱ 실시간 — 마지막 업데이트: <b>{last_update_hhmm}</b> "
                f"· 다음 갱신 예정: {next_update_at} "
                f"· 업데이트 주기: 15분"
            )
        data_freshness_note = ""
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
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
    margin: 0; padding: 20px; background: #f5f7fa; color: #2c3e50; line-height: 1.5;
  }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  h2 {{ font-size: 16px; margin: 24px 0 8px; padding-bottom: 4px; border-bottom: 2px solid #3498db; }}
  .meta {{ color: #7f8c8d; font-size: 12px; margin-bottom: 16px; }}
  .banner-warn {{ background:#fef3cd; border:2px solid #f0ad4e; border-radius:6px; padding:16px; margin-bottom:20px; }}
  .banner-warn h2 {{ margin-top:0; color:#8a6d3b; border:0; }}
  .banner-warn ol {{ line-height:1.8; }}
  .summary {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 16px; }}
  .card {{ background: white; border: 1px solid #e1e8ed; border-radius: 6px; padding: 16px; flex: 1; min-width: 200px; }}
  .card .label {{ font-size: 11px; color: #95a5a6; text-transform: uppercase; }}
  .card .value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
  .pos {{ color: #27ae60; }}
  .neg {{ color: #c0392b; }}
  .empty {{ text-align:center; color:#95a5a6; padding:12px !important; }}

  /* Top 5 카드 — 압축형 */
  .top5-wrap {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; margin-bottom: 4px; }}
  @media (max-width: 1100px) {{ .top5-wrap {{ grid-template-columns: repeat(3, 1fr); }} }}
  @media (max-width: 700px)  {{ .top5-wrap {{ grid-template-columns: repeat(2, 1fr); }} }}
  .top5-card {{
    background: white; border-radius: 6px; padding: 8px 10px;
    border-top: 3px solid #3498db; box-shadow: 0 1px 2px rgba(0,0,0,0.05); position: relative;
  }}
  .top5-card .top5-rank {{
    position: absolute; top: -6px; right: 8px; background: #3498db; color: white;
    font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 8px;
  }}
  .top5-head {{ margin-bottom: 5px; }}
  .top5-card .top5-name {{ font-size: 13px; font-weight: 600; line-height: 1.2; }}
  .top5-card .top5-code {{ font-size: 10px; color: #95a5a6; font-family: Consolas, monospace; margin-top: 1px; }}
  .top5-card .top5-price {{ font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .top5-card .top5-change {{ font-size: 11px; font-weight: 500; font-variant-numeric: tabular-nums; }}
  .top5-card .top5-change.pos {{ color: #c0392b; }}    /* 한국 관습: 상승=빨강 */
  .top5-card .top5-change.neg {{ color: #2980b9; }}    /* 하락=파랑 */
  .top5-card .top5-change.neutral {{ color: #7f8c8d; }}
  .top5-card .top5-price-na {{ font-size: 11px; color: #95a5a6; font-style: italic; }}
  .top5-card .top5-amount {{ font-size: 14px; font-weight: 700; margin: 5px 0 6px; }}
  .btn-buy-big, .btn-sell-big {{
    display: block; text-align: center; padding: 5px;
    border-radius: 3px; text-decoration: none; font-size: 11px; font-weight: 600;
  }}
  .btn-buy-big {{ background: #e74c3c; color: white; }}
  .btn-buy-big:hover {{ background: #c0392b; }}
  .btn-sell-big {{ background: #2980b9; color: white; }}
  .btn-sell-big:hover {{ background: #2471a3; }}

  /* 일반 표 */
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 6px; overflow: hidden; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }}
  th, td {{ padding: 6px 10px; font-size: 12px; border-bottom: 1px solid #ecf0f1; }}
  th {{ background: #34495e; color: white; text-align: left; font-weight: 500; font-size: 11px; }}
  table.sortable th[data-sort] {{ cursor: pointer; user-select: none; }}
  table.sortable th[data-sort]:hover {{ background: #2c3e50; }}
  table.sortable th[data-sort]::after {{ content: " ⇅"; opacity: 0.4; font-size: 9px; }}
  table.sortable th[data-dir="asc"]::after {{ content: " ▲"; opacity: 1; }}
  table.sortable th[data-dir="desc"]::after {{ content: " ▼"; opacity: 1; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.code {{ font-family: Consolas, monospace; color: #7f8c8d; }}
  td.name {{ font-weight: 500; }}
  td.market {{ color: #95a5a6; font-size: 11px; }}
  td.actions {{ white-space: nowrap; text-align: center; }}
  td.actions a {{ display:inline-block; padding:3px 8px; margin:0 2px; font-size:11px; border-radius:3px; text-decoration:none; font-weight:600; }}
  .btn-buy {{ background:#e74c3c; color:white; }}
  .btn-buy:hover {{ background:#c0392b; }}
  .btn-sell {{ background:#2980b9; color:white; }}
  .btn-sell:hover {{ background:#2471a3; }}
  tr:hover td {{ background: #f8f9fa; }}

  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  @media (max-width: 1000px) {{ .grid2 {{ grid-template-columns: 1fr; }} }}
  .filter-hint {{ font-size: 11px; color: #95a5a6; margin: 4px 0 8px; }}
  .footer {{ text-align: center; color: #95a5a6; font-size: 11px; margin-top: 24px; padding: 16px; }}

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
</style>
</head>
<body>

<h1>연기금 매매 리포트</h1>
<div class="nav-tabs">
  <a href="realtime.html" class="{mode_active_rt}">⏱ 실시간 업데이트</a>
  <a href="closing.html" class="{mode_active_cl}">📊 마감 기준</a>
</div>
<div class="mode-subtitle">{mode_subtitle}</div>
{data_freshness_note}
<div class="meta">기준일자: <b>{trade_date}</b> &nbsp;|&nbsp; 생성: {generated_at} &nbsp;|&nbsp; 출처: KRX 공개 데이터</div>
{no_data_banner}
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

<h2>최근 7거래일 연기금 일별 수급 (KOSPI)</h2>
<table>
  <thead><tr><th>일자</th><th class="num">매수</th><th class="num">매도</th><th class="num">순매수</th></tr></thead>
  <tbody>{recent_html}</tbody>
</table>

<div class="grid2">
  <div>
    <h2>오늘 연기금 순매수 Top 30</h2>
    <div class="filter-hint">컬럼 헤더 클릭 → 정렬</div>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">순매수</th>
        <th class="num" data-sort="num">매수</th>
        <th class="num" data-sort="num">매도</th>
        <th class="num" data-sort="num">등락률</th>
        <th class="num" data-sort="num">현재가</th>
        <th class="num" data-sort="num">시총比</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{top_buy_html}</tbody>
    </table>
  </div>
  <div>
    <h2>오늘 연기금 순매도 Top 30</h2>
    <div class="filter-hint">컬럼 헤더 클릭 → 정렬</div>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">순매도</th>
        <th class="num" data-sort="num">매수</th>
        <th class="num" data-sort="num">매도</th>
        <th class="num" data-sort="num">등락률</th>
        <th class="num" data-sort="num">현재가</th>
        <th class="num" data-sort="num">시총比</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{top_sell_html}</tbody>
    </table>
  </div>
</div>

<div class="grid2">
  <div>
    <h2>시총 대비 순매수 Top 30</h2>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">순매수</th>
        <th class="num" data-sort="num">매수</th>
        <th class="num" data-sort="num">매도</th>
        <th class="num" data-sort="num">등락률</th>
        <th class="num" data-sort="num">현재가</th>
        <th class="num" data-sort="num">시총比</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{top_cap_buy_html}</tbody>
    </table>
  </div>
  <div>
    <h2>시총 대비 순매도 Top 30</h2>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">순매도</th>
        <th class="num" data-sort="num">매수</th>
        <th class="num" data-sort="num">매도</th>
        <th class="num" data-sort="num">등락률</th>
        <th class="num" data-sort="num">현재가</th>
        <th class="num" data-sort="num">시총比</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{top_cap_sell_html}</tbody>
    </table>
  </div>
</div>

<div class="grid2">
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

<div class="footer">
  ⚠ 투자 권유가 아닙니다. KRX 공개 데이터 가공물. &nbsp;|&nbsp;
  데이터 출처: <a href="http://data.krx.co.kr/" target="_blank">data.krx.co.kr</a> &nbsp;|&nbsp;
  매수/매도 버튼 → 토스증권 호가창 (외부 링크)
</div>

<script>
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
</script>

</body>
</html>
"""


# ==========================================================================
# 엔트리포인트
# ==========================================================================

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

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

    # HTML (모드별 파일명)
    # 정책: realtime 빌드는 realtime.html + index.html.
    #       closing.html은 trading-trend 제외한 별도 fetch로 생성 (마감 기준 데이터 정확성).
    if mode == "realtime":
        realtime_html = render_html(payload, mode="realtime")
        for fname in ("realtime.html", "index.html"):
            (OUTPUT_DIR / fname).write_text(realtime_html, encoding="utf-8")
            log.info("HTML written: %s", OUTPUT_DIR / fname)
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
