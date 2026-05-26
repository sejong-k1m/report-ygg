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


def _ai_score(r: dict) -> float:
    """
    종합 점수 (가중 합산, 8개 변수):

    매매 강도 (큰 영향):
    - 시총비 (%)              × 100   ← 시총 대비 비중
    - 시총비 vs 전일거래대금  × 5     ← 평소 거래량 대비 강도

    모멘텀:
    - 등락률 (%)              × 1.5
    - 거래량 강도 (100 기준)  × 0.05  ← 토스 tradingStrength

    매수 누적 / 지속성:
    - 활발 기간 (일)          × 4    ← 연속 매수 보너스
    - 누적 순매수 (억)        × 0.005
    - 전일 대비 순매수 증가(억) × 0.02

    음수 페널티:
    - 연속 매도일수           × -8   ← 강한 매도세

    return: float — 양수=매수 강세, 음수=매도 강세
    """
    score = 0.0
    # 시총비 (가장 큼)
    score += (r.get("net_to_cap", 0) or 0) * 100
    # 전일 거래대금 대비 순매수 비율 (활발도 시그널)
    score += (r.get("net_vs_prev_val_ratio") or 0) * 5
    # 등락률
    score += (r.get("change_rate", 0) or 0) * 1.5
    # 거래량 강도 (100 = 평소, 200 = 2배 활발)
    ts = r.get("trading_strength", 0) or 0
    if ts > 0:
        score += (ts - 100) * 0.05
    # 활발 매수 기간 (양수면 가산)
    if r.get("net_amount", 0) > 0:
        score += _period_days(r) * 4
    # 누적 순매수 (억 단위)
    cumul = (r.get("cumulative_net_amount") or 0) / 100_000_000
    score += cumul * 0.005
    # 전일 대비 순매수 변화 (모멘텀 변화)
    delta = (r.get("delta_net_amount") or 0) / 100_000_000
    score += delta * 0.02
    # 연속 매도일수 페널티
    score -= (r.get("consecutive_sell_days", 0) or 0) * 8
    # DART 공시 점수 (호재/악재 키워드 매칭)
    score += (r.get("dart_score", 0) or 0)
    return round(score, 1)


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
        # 첫 컬럼은 항상 net_amount(순매수액) 표시. key 인자는 호출자가 정렬용으로만 사용.
        net = r.get("net_amount", 0)
        chg = r.get("change_rate", 0)
        chg_class = "pos" if chg > 0 else ("neg" if chg < 0 else "")
        score = _ai_score(r)
        consec = _consecutive_label(r)
        consec_cls = "consec-buy" if "연속매수" in consec else ("consec-sell" if "연속매도" in consec else "")
        consec_html = f"<span class='consec-badge {consec_cls}'>{consec}</span>" if consec else ""
        return (
            f"<tr>"
            f"<td class='code'>{_esc(r['stock_code'])}</td>"
            f"<td class='name'>{_esc(r['stock_name'])}{consec_html}</td>"
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

    empty_today = "<tr><td colspan='11' class='empty'>데이터 없음</td></tr>"
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
            f"<div class='top5-card'>"
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
    .nav-tabs, .layer-toggles, .pdf-btn, .comments-section,
    .footer, td.actions, th:last-child {{ display: none !important; }}
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
</style>
</head>
<body>

<h1>연기금 매매 리포트</h1>
<div class="nav-tabs">
  <a href="realtime.html" class="{mode_active_rt}">⏱ 실시간 업데이트</a>
  <a href="closing.html" class="{mode_active_cl}">📊 마감 기준</a>
  <button class="pdf-btn" onclick="window.print()">📥 PDF 저장</button>
</div>
<div class="mode-subtitle">{mode_subtitle}</div>
{data_freshness_note}
<div class="meta">기준일자: <b>{trade_date}</b> &nbsp;|&nbsp; 생성: {generated_at} &nbsp;|&nbsp; 출처: KRX 공개 데이터</div>

<div class="layer-toggles">
  <span class="lt-label">표시:</span>
  <label data-sec="overview"><input type="checkbox" checked> 시장수급+Top5</label>
  <label data-sec="weekly7"><input type="checkbox" checked> 최근 7거래일</label>
  <label data-sec="today-top30"><input type="checkbox" checked> 오늘 Top 30</label>
  <label data-sec="cap-top30"><input type="checkbox" checked> 시총比 Top 30</label>
  <label data-sec="weekly-top30"><input type="checkbox" checked> 주간 누적</label>
  <label data-sec="all-stocks"><input type="checkbox"> 📜 전체 종목</label>
  <label data-sec="comments"><input type="checkbox" checked> 💬 커뮤니티</label>
</div>
{no_data_banner}
<div class="layout-header">
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

<div class="grid2" data-section="today-top30">
  <div>
    <h2>오늘 연기금 순매수 Top 30</h2>
    <div class="filter-hint">컬럼 헤더 클릭 → 정렬 · AI 점수: 시총比×100 + 등락률×2 + 연속일×5 종합</div>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">순매수</th>
        <th class="num" data-sort="num">매수</th>
        <th class="num" data-sort="num">매도</th>
        <th class="num" data-sort="num">등락률</th>
        <th class="num" data-sort="num">현재가</th>
        <th class="num" data-sort="num">시총比</th>
        <th class="num" data-sort="num">AI</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{top_buy_html}</tbody>
    </table>
  </div>
  <div>
    <h2>오늘 연기금 순매도 Top 30</h2>
    <div class="filter-hint">컬럼 헤더 클릭 → 정렬 · AI 점수 음수 = 매도 강세</div>
    <table class="sortable">
      <thead><tr>
        <th>코드</th><th>종목명</th>
        <th class="num" data-sort="num">순매도</th>
        <th class="num" data-sort="num">매수</th>
        <th class="num" data-sort="num">매도</th>
        <th class="num" data-sort="num">등락률</th>
        <th class="num" data-sort="num">현재가</th>
        <th class="num" data-sort="num">시총比</th>
        <th class="num" data-sort="num">AI</th>
        <th>시장</th><th>주문</th>
      </tr></thead>
      <tbody>{top_sell_html}</tbody>
    </table>
  </div>
</div>

<div class="grid2" data-section="cap-top30">
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
        <th class="num" data-sort="num">AI</th>
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
