"""
디스코드 웹훅 알림. 4개 채널 분리 발송.

채널 구분:
- new_buy        : 70억+ 매수 종목
- consecutive    : 연일매수 종목
- hold_alert     : 보유종목 경고 (20억/40억/미체결)
- exec_log       : 주문/체결/취소 실행 로그

웹훅 실패는 절대 매매 로직을 막지 않음 (예외 잡고 로그만 남김).
"""
from __future__ import annotations

import logging
import threading
from queue import Queue, Empty
from typing import Optional

import requests

import app_secrets

log = logging.getLogger(__name__)


_CHANNEL_URL = {
    "new_buy": app_secrets.DISCORD_WEBHOOK_NEW_BUY,
    "consecutive": app_secrets.DISCORD_WEBHOOK_CONSECUTIVE_BUY,
    "hold_alert": app_secrets.DISCORD_WEBHOOK_HOLD_ALERT,
    "exec_log": app_secrets.DISCORD_WEBHOOK_EXEC_LOG,
}

# 색상 코드 (디스코드 임베드)
COLOR_GREEN = 0x2ECC71   # 매수
COLOR_BLUE = 0x3498DB    # 정보
COLOR_YELLOW = 0xF1C40F  # 경고
COLOR_RED = 0xE74C3C     # 자동매도/위험
COLOR_GRAY = 0x95A5A6    # 로그

# 백그라운드 발송 큐 (블로킹 방지)
_queue: "Queue[tuple]" = Queue()
_worker_started = False
_worker_lock = threading.Lock()


def _worker():
    while True:
        try:
            channel, payload = _queue.get(timeout=1)
        except Empty:
            continue
        url = _CHANNEL_URL.get(channel)
        if not url or "..." in url:
            log.warning("Discord webhook for '%s' not configured, skipping", channel)
            continue
        try:
            r = requests.post(url, json=payload, timeout=5)
            if r.status_code >= 300:
                log.error("Discord webhook %s failed: %s %s", channel, r.status_code, r.text[:200])
        except Exception as e:
            log.exception("Discord webhook %s exception: %s", channel, e)


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            t = threading.Thread(target=_worker, name="discord-worker", daemon=True)
            t.start()
            _worker_started = True


def _send(channel: str, embed: dict):
    _ensure_worker()
    _queue.put((channel, {"embeds": [embed]}))


def _fmt_won(amount: int) -> str:
    """1234567890 → '12.3억'"""
    if abs(amount) >= 100_000_000:
        return f"{amount / 100_000_000:.1f}억"
    if abs(amount) >= 10_000:
        return f"{amount / 10_000:.0f}만"
    return f"{amount:,}원"


# ============================================================
# 공개 API
# ============================================================

def notify_new_buy(stock_name: str, stock_code: str, pension_buy_amount: int, current_price: int):
    """연기금 70억+ 신규 매수 종목"""
    embed = {
        "title": f"🟢 연기금 신규 매수: {stock_name} ({stock_code})",
        "color": COLOR_GREEN,
        "fields": [
            {"name": "연기금 순매수", "value": _fmt_won(pension_buy_amount), "inline": True},
            {"name": "현재가", "value": f"{current_price:,}원", "inline": True},
        ],
    }
    _send("new_buy", embed)


def notify_consecutive_buy(stock_name: str, stock_code: str, days: int, total_amount: int):
    """연일매수 (N일 연속 순매수)"""
    embed = {
        "title": f"🔵 연일매수 {days}일차: {stock_name} ({stock_code})",
        "color": COLOR_BLUE,
        "fields": [
            {"name": "연속일수", "value": f"{days}일", "inline": True},
            {"name": "누적 순매수", "value": _fmt_won(total_amount), "inline": True},
        ],
    }
    _send("consecutive", embed)


def notify_hold_warn(stock_name: str, stock_code: str, pension_sell_amount: int, holding_qty: int, current_price: int):
    """보유종목 20억 매도 경고"""
    embed = {
        "title": f"⚠️ 보유종목 경고 (20억+ 매도): {stock_name} ({stock_code})",
        "color": COLOR_YELLOW,
        "fields": [
            {"name": "연기금 매도", "value": _fmt_won(pension_sell_amount), "inline": True},
            {"name": "보유수량", "value": f"{holding_qty:,}주", "inline": True},
            {"name": "현재가", "value": f"{current_price:,}원", "inline": True},
        ],
    }
    _send("hold_alert", embed)


def notify_auto_sell_triggered(stock_name: str, stock_code: str, pension_sell_amount: int, sell_price: int, sell_qty: int, dry_run: bool, alert_only: bool):
    """40억 매도 → 자동매도 트리거"""
    if alert_only:
        title = f"🔔 [알림전용시간] 40억 매도 감지: {stock_name} ({stock_code})"
        color = COLOR_YELLOW
        suffix = " (10:30 이후 자동매도 활성)"
    elif dry_run:
        title = f"🧪 [DRY_RUN] 자동매도 시뮬레이션: {stock_name} ({stock_code})"
        color = COLOR_GRAY
        suffix = ""
    else:
        title = f"🔴 자동매도 트리거: {stock_name} ({stock_code})"
        color = COLOR_RED
        suffix = ""
    embed = {
        "title": title + suffix,
        "color": color,
        "fields": [
            {"name": "연기금 매도", "value": _fmt_won(pension_sell_amount), "inline": True},
            {"name": "매도 가격(-0.7%)", "value": f"{sell_price:,}원", "inline": True},
            {"name": "매도 수량", "value": f"{sell_qty:,}주", "inline": True},
        ],
    }
    _send("hold_alert", embed)


def notify_auto_buy(stock_name: str, stock_code: str, source: str, pension_amount: int, buy_price: int, buy_qty: int):
    """자동매수 실행 (NEW_BUY 또는 CONSECUTIVE_BUY 트리거)"""
    label = "🟢 자동매수 (60억+ 매수)" if source == "NEW_BUY" else "🔵 자동매수 (연일매수)"
    embed = {
        "title": f"{label}: {stock_name} ({stock_code})",
        "color": COLOR_GREEN,
        "fields": [
            {"name": "트리거", "value": source, "inline": True},
            {"name": "연기금", "value": _fmt_won(pension_amount), "inline": True},
            {"name": "매수가(+0.3%)", "value": f"{buy_price:,}원", "inline": True},
            {"name": "매수수량", "value": f"{buy_qty:,}주", "inline": True},
            {"name": "총액", "value": _fmt_won(buy_price * buy_qty), "inline": True},
        ],
    }
    # 신규매수와 연일매수 채널 둘 중 어디 보낼지 source로 결정
    channel = "new_buy" if source == "NEW_BUY" else "consecutive"
    _send(channel, embed)


def notify_failsafe_sell(stock_name: str, stock_code: str, sell_price: int, sell_qty: int):
    """미체결 + 추가하락 → -2.5% 2차 매도"""
    embed = {
        "title": f"🚨 추가하락 감지 (-2.5% 도달): {stock_name} ({stock_code})",
        "color": COLOR_RED,
        "description": "1차 매도 미체결 상태에서 추가하락 → -2.5% 지정가 추가 매도",
        "fields": [
            {"name": "매도 가격(-2.5%)", "value": f"{sell_price:,}원", "inline": True},
            {"name": "매도 수량", "value": f"{sell_qty:,}주", "inline": True},
        ],
    }
    _send("hold_alert", embed)


def notify_unfilled(stock_name: str, stock_code: str, order_no: str, sell_price: int, remaining_qty: int):
    """미체결 잔량 알림 (방치 + 통보)"""
    embed = {
        "title": f"⏳ 미체결 알림: {stock_name} ({stock_code})",
        "color": COLOR_YELLOW,
        "fields": [
            {"name": "주문번호", "value": order_no, "inline": True},
            {"name": "지정가", "value": f"{sell_price:,}원", "inline": True},
            {"name": "미체결 잔량", "value": f"{remaining_qty:,}주", "inline": True},
        ],
    }
    _send("hold_alert", embed)


def notify_exec(message: str, fields: Optional[list] = None):
    """일반 실행 로그 (체결, 취소, 주문결과 등)"""
    embed = {
        "title": message,
        "color": COLOR_GRAY,
    }
    if fields:
        embed["fields"] = fields
    _send("exec_log", embed)


def notify_system(message: str, color: int = COLOR_BLUE):
    """시스템 이벤트 (시작/종료/킬스위치/한도초과 등)"""
    embed = {"title": message, "color": color}
    _send("exec_log", embed)
