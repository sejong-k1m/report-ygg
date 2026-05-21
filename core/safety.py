"""
안전장치 모듈.

- 시간 가드 (09:00~10:30 = 알림만, 이후 자동매도 활성)
- 일일/종목당/1회 매도 한도
- 같은 종목 같은 트리거 당일 1회만 (멱등성)
- 가격 sanity check (현재가 대비 -2% 미만 거부)
- 킬스위치 (파일 존재 + 메모리 플래그)
- 호가 단위 정규화

모든 자동매도 경로는 반드시 SafetyGuard.evaluate_sell()을 통과해야 함.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

import config
from storage import db

log = logging.getLogger(__name__)


# ============================================================
# 시간 가드
# ============================================================

def _hhmm_to_time(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def now_within_market() -> bool:
    """09:00 ~ 15:30 사이인지"""
    n = dt.datetime.now().time()
    return _hhmm_to_time(config.MARKET_OPEN) <= n <= _hhmm_to_time(config.MARKET_CLOSE)


def is_alert_only_window() -> bool:
    """09:00 ~ 10:30 = 알림만 (자동매도 불가)"""
    n = dt.datetime.now().time()
    return _hhmm_to_time(config.MARKET_OPEN) <= n < _hhmm_to_time(config.ALERT_ONLY_UNTIL)


def is_auto_sell_active() -> bool:
    """10:30 ~ 15:30 자동매도 활성 시간대"""
    n = dt.datetime.now().time()
    return _hhmm_to_time(config.ALERT_ONLY_UNTIL) <= n <= _hhmm_to_time(config.MARKET_CLOSE)


# ============================================================
# 킬스위치
# ============================================================

_kill_lock = threading.Lock()
_kill_flag = False


def kill_switch_engaged() -> bool:
    """메모리 플래그 OR 파일 존재"""
    global _kill_flag
    if _kill_flag:
        return True
    if os.path.exists(config.KILL_SWITCH_FILE):
        return True
    return False


def engage_kill_switch(reason: str = "manual"):
    """킬스위치 활성화 (메모리 + 파일 둘 다)"""
    global _kill_flag
    with _kill_lock:
        _kill_flag = True
    try:
        with open(config.KILL_SWITCH_FILE, "w", encoding="utf-8") as f:
            f.write(f"engaged_at={dt.datetime.now().isoformat()}\nreason={reason}\n")
    except Exception:
        log.exception("Failed to write kill switch file")
    log.warning("KILL SWITCH ENGAGED: %s", reason)


def release_kill_switch():
    """킬스위치 해제 (사용자 명시적 액션 시에만)"""
    global _kill_flag
    with _kill_lock:
        _kill_flag = False
    if os.path.exists(config.KILL_SWITCH_FILE):
        os.remove(config.KILL_SWITCH_FILE)


# ============================================================
# 호가 단위 (KOSPI/KOSDAQ 공통, 2023년 개정 기준)
# ============================================================

def tick_size(price: int) -> int:
    if price < 2000:
        return 1
    if price < 5000:
        return 5
    if price < 20000:
        return 10
    if price < 50000:
        return 50
    if price < 200000:
        return 100
    if price < 500000:
        return 500
    return 1000


def round_to_tick(price: int, *, direction: str = "down") -> int:
    """호가 단위로 정규화. direction='down'은 매도 시 보수적(낮춤), 'up'은 매수 시(높임)."""
    t = tick_size(price)
    if direction == "down":
        return (price // t) * t
    else:
        return ((price + t - 1) // t) * t


# ============================================================
# 매도 평가 결과
# ============================================================

@dataclass
class SellDecision:
    allowed: bool
    reason: str = ""
    final_qty: int = 0
    final_price: int = 0


# ============================================================
# 핵심: 매도 가능 여부 평가
# ============================================================

def evaluate_sell(
    stock_code: str,
    stock_name: str,
    desired_price: int,
    desired_qty: int,
    current_price: int,
    holding_qty: int,
) -> SellDecision:
    """
    자동매도 시도 시 반드시 이 함수 통과.
    실패 사유:
    - 킬스위치 활성
    - 알림전용 시간대 (09:00~10:30)
    - 장외 시간
    - 가격 비정상 (현재가 대비 -2% 미만)
    - 한도 초과 (1회/종목당/전체)
    - 잔고 부족
    """
    if kill_switch_engaged():
        return SellDecision(False, "kill switch engaged")

    if not now_within_market():
        return SellDecision(False, "outside market hours")

    if is_alert_only_window():
        return SellDecision(False, "alert-only window (09:00~10:30)")

    if desired_qty <= 0 or holding_qty <= 0:
        return SellDecision(False, "no holding")

    # 가격 sanity: 현재가 대비 desired_price가 너무 낮으면 거부
    if current_price > 0:
        deviation = (desired_price - current_price) / current_price
        if deviation < config.PRICE_SANITY_MAX_DROP:
            return SellDecision(
                False,
                f"price sanity fail: {deviation:.2%} below current",
            )

    # 호가 단위 정규화
    price = round_to_tick(desired_price, direction="down")
    qty = min(desired_qty, holding_qty)

    # 1회 한도 (금액 기준)
    order_amount = price * qty
    if order_amount > config.MAX_SELL_PER_ORDER:
        # 한도 내로 수량 축소
        qty = config.MAX_SELL_PER_ORDER // price
        if qty <= 0:
            return SellDecision(False, "single-order cap below 1 share")

    # 종목당 일일 한도
    stock_used = db.get_sell_total(stock_code)
    stock_remaining = config.MAX_SELL_PER_STOCK_DAILY - stock_used
    if stock_remaining <= 0:
        return SellDecision(False, f"stock daily cap reached ({stock_used:,})")
    max_qty_by_stock_cap = stock_remaining // price
    if max_qty_by_stock_cap <= 0:
        return SellDecision(False, "stock daily cap leaves <1 share")
    qty = min(qty, max_qty_by_stock_cap)

    # 전체 일일 한도
    total_used = db.get_total_sell_today()
    total_remaining = config.MAX_SELL_TOTAL_DAILY - total_used
    if total_remaining <= 0:
        return SellDecision(False, f"global daily cap reached ({total_used:,})")
    max_qty_by_total = total_remaining // price
    if max_qty_by_total <= 0:
        return SellDecision(False, "global daily cap leaves <1 share")
    qty = min(qty, max_qty_by_total)

    if qty <= 0:
        return SellDecision(False, "final qty <= 0")

    return SellDecision(True, "ok", final_qty=qty, final_price=price)


# ============================================================
# 매수 평가 (자동매수)
# ============================================================

@dataclass
class BuyDecision:
    allowed: bool
    reason: str = ""
    final_qty: int = 0
    final_price: int = 0


def is_auto_buy_window() -> bool:
    """10:30 ~ 15:00 = 자동매수 활성. 이전엔 알림만, 이후엔 단일가 회피."""
    n = dt.datetime.now().time()
    return _hhmm_to_time(config.ALERT_ONLY_UNTIL) <= n < _hhmm_to_time(config.AUTO_BUY_END)


def evaluate_buy(
    stock_code: str,
    stock_name: str,
    desired_price: int,
    desired_amount: int,
    current_price: int,
    available_cash: int,
    today_open_price: int = 0,
) -> BuyDecision:
    """
    자동매수 시도 시 반드시 통과해야 함.
    실패 사유:
    - 자동매수 비활성 (config)
    - 킬스위치
    - 알림전용 시간대 (09:00~10:30) 또는 매수 종료 시간대 (15:00 이후)
    - 갭상승 +3% 이상
    - 현재가 0 (조회 실패)
    - 한도 초과
    - 예수금 부족
    """
    if not config.AUTO_BUY_ENABLED:
        return BuyDecision(False, "AUTO_BUY_ENABLED=False")

    if kill_switch_engaged():
        return BuyDecision(False, "kill switch engaged")

    if not now_within_market():
        return BuyDecision(False, "outside market hours")

    if is_alert_only_window():
        return BuyDecision(False, "alert-only window (09:00~10:30)")

    n = dt.datetime.now().time()
    if n >= _hhmm_to_time(config.AUTO_BUY_END):
        return BuyDecision(False, f"after AUTO_BUY_END ({config.AUTO_BUY_END})")

    if current_price <= 0:
        return BuyDecision(False, "current_price unavailable")

    # 갭상승 회피 (시초가 대비 +N% 이상이면 추격매수 차단)
    if today_open_price > 0:
        gap = (current_price - today_open_price) / today_open_price
        if gap > config.BUY_GAP_UP_SKIP_PCT:
            return BuyDecision(False, f"gap-up too high ({gap:.2%})")

    # 호가 단위 정규화 (매수는 살짝 높게 → up)
    price = round_to_tick(desired_price, direction="up")
    if price <= 0:
        return BuyDecision(False, "price <= 0")

    # 1회 한도
    order_amount = min(desired_amount, config.MAX_BUY_PER_ORDER)
    qty = order_amount // price
    if qty <= 0:
        return BuyDecision(False, "single-order cap below 1 share")

    # 종목당 일일 한도
    stock_used = db.get_buy_total(stock_code)
    stock_remaining = config.MAX_BUY_PER_STOCK_DAILY - stock_used
    if stock_remaining <= 0:
        return BuyDecision(False, f"stock daily buy cap reached ({stock_used:,})")
    max_qty_by_stock_cap = stock_remaining // price
    if max_qty_by_stock_cap <= 0:
        return BuyDecision(False, "stock daily cap leaves <1 share")
    qty = min(qty, max_qty_by_stock_cap)

    # 전체 일일 한도
    total_used = db.get_total_buy_today()
    total_remaining = config.MAX_BUY_TOTAL_DAILY - total_used
    if total_remaining <= 0:
        return BuyDecision(False, f"global daily buy cap reached ({total_used:,})")
    max_qty_by_total = total_remaining // price
    if max_qty_by_total <= 0:
        return BuyDecision(False, "global daily cap leaves <1 share")
    qty = min(qty, max_qty_by_total)

    # 예수금 체크
    needed = price * qty
    if needed > available_cash:
        # 예수금 안에서 살 수 있는 만큼 축소
        qty = available_cash // price
        if qty <= 0:
            return BuyDecision(False, f"insufficient cash ({available_cash:,} < {price:,})")

    if qty <= 0:
        return BuyDecision(False, "final qty <= 0")

    return BuyDecision(True, "ok", final_qty=qty, final_price=price)


# ============================================================
# 트리거 멱등성 (같은 종목/같은 트리거 하루 1회)
# ============================================================

def already_triggered(stock_code: str, trigger_type: str) -> bool:
    return db.has_triggered_today(stock_code, trigger_type)


def mark_triggered(stock_code: str, stock_name: str, trigger_type: str, pension_amount: int, payload: Optional[dict] = None):
    db.record_trigger(stock_code, stock_name, trigger_type, pension_amount, payload)
