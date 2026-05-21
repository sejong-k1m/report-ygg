"""
알림 디스패처. 모든 알림 함수가 디스코드 + 카톡 두 채널로 동시 발송.

사용:
    import notify
    notify.notify_new_buy(...)
    notify.notify_system("...")

각 백엔드는 자체 큐+스레드로 비동기 발송 → 한쪽 실패가 다른 쪽 막지 않음.
"""
from __future__ import annotations

from typing import Optional

from notify import discord, kakao
# GUI 등에서 색상 상수 직접 참조하기 때문에 re-export
from notify.discord import COLOR_GREEN, COLOR_BLUE, COLOR_YELLOW, COLOR_RED, COLOR_GRAY


def init_kakao() -> bool:
    """앱 시작시 호출. 카카오 토큰 검증."""
    return kakao.init()


def notify_new_buy(name: str, code: str, amount: int, price: int):
    discord.notify_new_buy(name, code, amount, price)
    kakao.notify_new_buy(name, code, amount, price)


def notify_consecutive_buy(name: str, code: str, days: int, total: int):
    discord.notify_consecutive_buy(name, code, days, total)
    kakao.notify_consecutive_buy(name, code, days, total)


def notify_hold_warn(name: str, code: str, sell: int, qty: int, price: int):
    discord.notify_hold_warn(name, code, sell, qty, price)
    kakao.notify_hold_warn(name, code, sell, qty, price)


def notify_auto_sell_triggered(name: str, code: str, sell: int, sell_price: int, sell_qty: int, dry_run: bool, alert_only: bool):
    discord.notify_auto_sell_triggered(name, code, sell, sell_price, sell_qty, dry_run, alert_only)
    kakao.notify_auto_sell_triggered(name, code, sell, sell_price, sell_qty, dry_run, alert_only)


def notify_failsafe_sell(name: str, code: str, sell_price: int, sell_qty: int):
    discord.notify_failsafe_sell(name, code, sell_price, sell_qty)
    kakao.notify_failsafe_sell(name, code, sell_price, sell_qty)


def notify_auto_buy(name: str, code: str, source: str, pension_amount: int, buy_price: int, buy_qty: int):
    discord.notify_auto_buy(name, code, source, pension_amount, buy_price, buy_qty)
    kakao.notify_auto_buy(name, code, source, pension_amount, buy_price, buy_qty)


def notify_unfilled(name: str, code: str, order_no: str, sell_price: int, remaining_qty: int):
    discord.notify_unfilled(name, code, order_no, sell_price, remaining_qty)
    kakao.notify_unfilled(name, code, order_no, sell_price, remaining_qty)


def notify_exec(message: str, fields: Optional[list] = None):
    discord.notify_exec(message, fields)
    kakao.notify_exec(message, fields)


def notify_system(message: str, color: int = COLOR_BLUE):
    discord.notify_system(message, color)
    kakao.notify_system(message)
