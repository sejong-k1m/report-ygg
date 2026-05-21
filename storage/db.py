"""
SQLite 래퍼.
- 단일 파일 DB
- 멀티스레드 안전 (check_same_thread=False + 락)
- 모든 시간은 'YYYY-MM-DD HH:MM:SS' KST 문자열로 저장
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

import config

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def init():
    """앱 시작시 1회 호출"""
    global _conn
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL;")
    schema_path = Path(__file__).parent / "schema.sql"
    with open(schema_path, "r", encoding="utf-8") as f:
        _conn.executescript(f.read())


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


def conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("DB not initialized. Call storage.db.init() first.")
    return _conn


# ============================================================
# pension_daily
# ============================================================

def upsert_pension_daily(stock_code: str, stock_name: str, buy: int, sell: int, is_confirmed: bool = False, trade_date: Optional[str] = None):
    trade_date = trade_date or _today()
    net = buy - sell
    with _lock:
        conn().execute(
            """
            INSERT INTO pension_daily(trade_date, stock_code, stock_name, buy_amount, sell_amount, net_amount, is_confirmed, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, stock_code) DO UPDATE SET
                stock_name = excluded.stock_name,
                buy_amount = excluded.buy_amount,
                sell_amount = excluded.sell_amount,
                net_amount = excluded.net_amount,
                is_confirmed = excluded.is_confirmed,
                updated_at = excluded.updated_at
            """,
            (trade_date, stock_code, stock_name, buy, sell, net, 1 if is_confirmed else 0, _now()),
        )


def get_pension_today(stock_code: str) -> Optional[sqlite3.Row]:
    cur = conn().execute(
        "SELECT * FROM pension_daily WHERE trade_date=? AND stock_code=?",
        (_today(), stock_code),
    )
    return cur.fetchone()


def get_recently_active_stocks(days_back: int = 5, min_abs_amount: int = 1_000_000_000) -> list:
    """
    최근 N일간 연기금이 1억+ 거래한 종목 코드 리스트.
    봇이 운영하면서 누적된 데이터 기반으로 워치리스트 자동 확장용.
    """
    today = dt.date.today()
    earliest = (today - dt.timedelta(days=days_back)).strftime("%Y-%m-%d")
    rows = conn().execute(
        """
        SELECT DISTINCT stock_code
        FROM pension_daily
        WHERE trade_date >= ?
          AND (buy_amount >= ? OR sell_amount >= ?)
        """,
        (earliest, min_abs_amount, min_abs_amount),
    ).fetchall()
    return [r["stock_code"] for r in rows]


def get_consecutive_buy_streak(stock_code: str, days_back: int = 10) -> int:
    """오늘 포함 며칠 연속 순매수인지 반환 (오늘이 순매수 아니면 0)"""
    today = dt.date.today()
    streak = 0
    for i in range(days_back):
        date_str = (today - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        row = conn().execute(
            "SELECT net_amount FROM pension_daily WHERE trade_date=? AND stock_code=?",
            (date_str, stock_code),
        ).fetchone()
        if row is None:
            # 데이터 없는 날은 거래일이 아닐 수도 있음 → 그날만 건너뛰지 말고 끊는 게 안전
            break
        if row["net_amount"] > 0:
            streak += 1
        else:
            break
    return streak


# ============================================================
# triggers (멱등성)
# ============================================================

def has_triggered_today(stock_code: str, trigger_type: str) -> bool:
    row = conn().execute(
        "SELECT 1 FROM triggers WHERE trade_date=? AND stock_code=? AND trigger_type=? LIMIT 1",
        (_today(), stock_code, trigger_type),
    ).fetchone()
    return row is not None


def record_trigger(stock_code: str, stock_name: str, trigger_type: str, pension_amount: int, payload: Optional[dict] = None):
    with _lock:
        conn().execute(
            """
            INSERT INTO triggers(trade_date, stock_code, stock_name, trigger_type, pension_amount, triggered_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_today(), stock_code, stock_name, trigger_type, pension_amount, _now(), json.dumps(payload or {}, ensure_ascii=False)),
        )


# ============================================================
# orders
# ============================================================

def insert_order(stock_code: str, stock_name: str, side: str, order_type: str, price: int, qty: int, parent_order_id: Optional[int] = None, note: str = "") -> int:
    with _lock:
        cur = conn().execute(
            """
            INSERT INTO orders(trade_date, stock_code, stock_name, side, order_type, price, qty, status, placed_at, last_updated_at, parent_order_id, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
            """,
            (_today(), stock_code, stock_name, side, order_type, price, qty, _now(), _now(), parent_order_id, note),
        )
        return cur.lastrowid


def update_order_no(order_id: int, order_no: str):
    with _lock:
        conn().execute(
            "UPDATE orders SET order_no=?, last_updated_at=? WHERE id=?",
            (order_no, _now(), order_id),
        )


def update_order_status_by_id(order_id: int, status: str):
    with _lock:
        conn().execute(
            "UPDATE orders SET status=?, last_updated_at=? WHERE id=?",
            (status, _now(), order_id),
        )


def update_order_status(order_no: str, status: str, filled_qty: Optional[int] = None):
    with _lock:
        if filled_qty is not None:
            conn().execute(
                "UPDATE orders SET status=?, filled_qty=?, last_updated_at=? WHERE order_no=?",
                (status, filled_qty, _now(), order_no),
            )
        else:
            conn().execute(
                "UPDATE orders SET status=?, last_updated_at=? WHERE order_no=?",
                (status, _now(), order_no),
            )


def get_pending_orders():
    return conn().execute(
        "SELECT * FROM orders WHERE trade_date=? AND status IN ('PENDING','PARTIAL') ORDER BY id",
        (_today(),),
    ).fetchall()


def get_order_by_no(order_no: str):
    return conn().execute(
        "SELECT * FROM orders WHERE order_no=? LIMIT 1",
        (order_no,),
    ).fetchone()


# ============================================================
# daily_sell_totals (한도)
# ============================================================

def add_sell_total(stock_code: str, amount: int):
    with _lock:
        for code in (stock_code, "_TOTAL_"):
            conn().execute(
                """
                INSERT INTO daily_sell_totals(trade_date, stock_code, total_amount)
                VALUES (?, ?, ?)
                ON CONFLICT(trade_date, stock_code) DO UPDATE SET total_amount = total_amount + excluded.total_amount
                """,
                (_today(), code, amount),
            )


def get_sell_total(stock_code: str) -> int:
    row = conn().execute(
        "SELECT total_amount FROM daily_sell_totals WHERE trade_date=? AND stock_code=?",
        (_today(), stock_code),
    ).fetchone()
    return row["total_amount"] if row else 0


def get_total_sell_today() -> int:
    return get_sell_total("_TOTAL_")


# ============================================================
# daily_buy_totals (한도)
# ============================================================

def add_buy_total(stock_code: str, amount: int):
    with _lock:
        for code in (stock_code, "_TOTAL_"):
            conn().execute(
                """
                INSERT INTO daily_buy_totals(trade_date, stock_code, total_amount)
                VALUES (?, ?, ?)
                ON CONFLICT(trade_date, stock_code) DO UPDATE SET total_amount = total_amount + excluded.total_amount
                """,
                (_today(), code, amount),
            )


def get_buy_total(stock_code: str) -> int:
    row = conn().execute(
        "SELECT total_amount FROM daily_buy_totals WHERE trade_date=? AND stock_code=?",
        (_today(), stock_code),
    ).fetchone()
    return row["total_amount"] if row else 0


def get_total_buy_today() -> int:
    return get_buy_total("_TOTAL_")


# ============================================================
# holdings
# ============================================================

def replace_holdings(holdings: list):
    """holdings: list[dict(stock_code, stock_name, qty, avg_price)]"""
    today = _today()
    with _lock:
        conn().execute("DELETE FROM holdings WHERE snapshot_date=?", (today,))
        for h in holdings:
            conn().execute(
                "INSERT INTO holdings(snapshot_date, stock_code, stock_name, qty, avg_price) VALUES (?, ?, ?, ?, ?)",
                (today, h["stock_code"], h["stock_name"], h["qty"], h["avg_price"]),
            )


def get_holdings() -> list:
    rows = conn().execute(
        "SELECT * FROM holdings WHERE snapshot_date=?",
        (_today(),),
    ).fetchall()
    return [dict(r) for r in rows]


def get_holding(stock_code: str):
    row = conn().execute(
        "SELECT * FROM holdings WHERE snapshot_date=? AND stock_code=?",
        (_today(), stock_code),
    ).fetchone()
    return dict(row) if row else None
