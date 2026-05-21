"""
룰 엔진. 연기금 데이터 + 보유종목 + 가격 → 이벤트 리스트.

이벤트 타입:
- NEW_BUY        : 70억+ 매수 (전 종목)
- HOLD_WARN      : 20억+ 매도 (보유종목)
- AUTO_SELL      : 40억+ 매도 (보유종목)  ← 자동매도 트리거
- FAILSAFE_SELL  : 미체결 + 추가하락 -2.5% 도달 (별도 경로)
- CONSECUTIVE_BUY: 2일+ 연속 순매수

각 이벤트는 (stock_code, stock_name, trigger_type, pension_amount, extra) 형태.
멱등성은 safety.already_triggered() 로 확인.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import config
from core import safety
from storage import db

log = logging.getLogger(__name__)


@dataclass
class Event:
    stock_code: str
    stock_name: str
    trigger_type: str
    pension_amount: int          # 매수 또는 매도 금액 (절대값)
    current_price: int = 0
    holding_qty: int = 0
    extra: dict = field(default_factory=dict)


def evaluate_for_stock(
    stock_code: str,
    stock_name: str,
    pension_buy: int,
    pension_sell: int,
    current_price: int,
    holding_qty: int = 0,
) -> list:
    """
    한 종목의 현재 연기금 데이터로 이벤트 평가.
    반환: 발동해야 할 이벤트 리스트 (이미 멱등 통과된 것만).
    """
    events: list = []

    # 1) 70억+ 매수 (전 종목 대상, 보유여부 무관)
    if pension_buy >= config.THRESHOLD_NEW_BUY:
        if not safety.already_triggered(stock_code, "NEW_BUY"):
            events.append(Event(
                stock_code=stock_code, stock_name=stock_name,
                trigger_type="NEW_BUY",
                pension_amount=pension_buy,
                current_price=current_price,
                holding_qty=holding_qty,
            ))

    # 2/3) 보유종목 매도 트리거 (20억 / 40억)
    if holding_qty > 0 and pension_sell > 0:
        # 40억+ → AUTO_SELL (강한 트리거 우선)
        if pension_sell >= config.THRESHOLD_HOLD_AUTO_SELL:
            if not safety.already_triggered(stock_code, "AUTO_SELL"):
                events.append(Event(
                    stock_code=stock_code, stock_name=stock_name,
                    trigger_type="AUTO_SELL",
                    pension_amount=pension_sell,
                    current_price=current_price,
                    holding_qty=holding_qty,
                ))
        # 20억+ → HOLD_WARN (40억 트리거됐어도 별도 알림으로 남김)
        if pension_sell >= config.THRESHOLD_HOLD_WARN:
            if not safety.already_triggered(stock_code, "HOLD_WARN"):
                events.append(Event(
                    stock_code=stock_code, stock_name=stock_name,
                    trigger_type="HOLD_WARN",
                    pension_amount=pension_sell,
                    current_price=current_price,
                    holding_qty=holding_qty,
                ))

    return events


def evaluate_consecutive_buy(stock_code: str, stock_name: str, current_price: int) -> Optional[Event]:
    """
    오늘 포함 N일 이상 연속 순매수면 CONSECUTIVE_BUY 이벤트.
    오늘 분은 잠정치라도 양수면 카운트.
    """
    streak = db.get_consecutive_buy_streak(stock_code)
    if streak < config.CONSECUTIVE_BUY_DAYS:
        return None
    if safety.already_triggered(stock_code, "CONSECUTIVE_BUY"):
        return None
    today_row = db.get_pension_today(stock_code)
    total = today_row["net_amount"] if today_row else 0
    return Event(
        stock_code=stock_code, stock_name=stock_name,
        trigger_type="CONSECUTIVE_BUY",
        pension_amount=total,
        current_price=current_price,
        extra={"days": streak},
    )
