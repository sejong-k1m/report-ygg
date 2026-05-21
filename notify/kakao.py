"""
카카오톡 "나에게 메시지 보내기" 알림 (talk/memo API).

토큰 라이프사이클:
- access_token: 6시간 (코드에서 만료 5분전 자동 refresh)
- refresh_token: 60일 (만료시 사용자가 tools/kakao_auth.py 재실행 필요)

토큰은 data/kakao_tokens.json 에 저장 (gitignore).
첫 발급은 tools/kakao_auth.py 로 1회 OAuth 인증.

발송 실패는 매매 로직을 막지 않음 (백그라운드 큐 + 예외 무시).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from queue import Queue, Empty
from typing import Optional

import requests

import app_secrets

log = logging.getLogger(__name__)

TOKEN_PATH = Path("data/kakao_tokens.json")
KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

_queue: "Queue[str]" = Queue()
_worker_started = False
_worker_lock = threading.Lock()
_token_lock = threading.Lock()


# ============================================================
# 토큰 저장/로드/갱신
# ============================================================

def _load_tokens() -> Optional[dict]:
    if not TOKEN_PATH.exists():
        return None
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.exception("kakao token file corrupted")
        return None


def _save_tokens(data: dict):
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _refresh_access_token(tok: dict) -> Optional[str]:
    """refresh_token으로 access_token 갱신."""
    try:
        r = requests.post(KAKAO_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "client_id": app_secrets.KAKAO_REST_API_KEY,
            "refresh_token": tok["refresh_token"],
        }, timeout=10)
    except Exception:
        log.exception("kakao refresh request failed")
        return None
    if r.status_code != 200:
        log.error("kakao refresh failed: %s %s", r.status_code, r.text[:300])
        return None
    new = r.json()
    now = int(time.time())
    tok["access_token"] = new["access_token"]
    tok["access_expires_at"] = now + int(new.get("expires_in", 21600)) - 60
    if "refresh_token" in new:
        tok["refresh_token"] = new["refresh_token"]
        tok["refresh_expires_at"] = now + int(new.get("refresh_token_expires_in", 60 * 24 * 3600)) - 60
    _save_tokens(tok)
    log.info("kakao access_token refreshed")
    return tok["access_token"]


def _get_access_token() -> Optional[str]:
    """유효한 access_token 반환. 만료 5분전이면 자동 refresh."""
    with _token_lock:
        tok = _load_tokens()
        if not tok:
            return None
        now = int(time.time())
        # refresh token 만료 임박 경고 (7일 이내)
        if tok.get("refresh_expires_at", 0) - now < 7 * 24 * 3600:
            remaining_days = (tok.get("refresh_expires_at", 0) - now) // 86400
            log.warning("⚠️ kakao refresh_token 만료 %s일 남음. tools/kakao_auth.py 재실행 권장", remaining_days)
        # access token 만료 5분전이면 갱신
        if tok.get("access_expires_at", 0) - now < 300:
            return _refresh_access_token(tok)
        return tok.get("access_token")


def init():
    """앱 시작시 1회 호출. 토큰 상태 점검."""
    tok = _load_tokens()
    if not tok:
        log.warning("⚠️ 카카오 토큰 없음. tools/kakao_auth.py 실행해서 발급 필요. 카카오 알림은 비활성화됨.")
        return False
    now = int(time.time())
    refresh_left_days = (tok.get("refresh_expires_at", 0) - now) // 86400
    log.info("kakao token loaded. refresh_token 만료까지 %s일", refresh_left_days)
    # 시작 즉시 access_token 갱신 시도 (만료 임박이면)
    _get_access_token()
    return True


# ============================================================
# 발송 워커
# ============================================================

def _worker():
    while True:
        try:
            text = _queue.get(timeout=1)
        except Empty:
            continue
        try:
            token = _get_access_token()
            if not token:
                log.warning("kakao token unavailable; drop msg: %s", text[:60])
                continue
            template = {
                "object_type": "text",
                "text": text[:400],   # 카카오 본문 길이 제한
                "link": {
                    "web_url": "https://www.kiwoom.com",
                    "mobile_web_url": "https://www.kiwoom.com",
                },
            }
            r = requests.post(
                KAKAO_MEMO_URL,
                headers={"Authorization": f"Bearer {token}"},
                data={"template_object": json.dumps(template, ensure_ascii=False)},
                timeout=5,
            )
            if r.status_code >= 300:
                log.error("kakao send failed: %s %s", r.status_code, r.text[:200])
        except Exception:
            log.exception("kakao worker exception")


def _ensure_worker():
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            t = threading.Thread(target=_worker, name="kakao-worker", daemon=True)
            t.start()
            _worker_started = True


def _send(text: str):
    _ensure_worker()
    _queue.put(text)


def _won(amount: int) -> str:
    if abs(amount) >= 100_000_000:
        return f"{amount / 100_000_000:.1f}억"
    if abs(amount) >= 10_000:
        return f"{amount / 10_000:.0f}만"
    return f"{amount:,}원"


# ============================================================
# 공개 API (디스코드와 동일 시그니처)
# ============================================================

def notify_new_buy(name: str, code: str, amount: int, price: int):
    _send(f"[🟢신규매수] {name}({code})\n연기금 매수 {_won(amount)} | 현재가 {price:,}원")


def notify_consecutive_buy(name: str, code: str, days: int, total: int):
    _send(f"[🔵연일매수 {days}일차] {name}({code})\n누적 순매수 {_won(total)}")


def notify_hold_warn(name: str, code: str, sell: int, qty: int, price: int):
    _send(f"[⚠️20억경고] {name}({code})\n연기금 매도 {_won(sell)} | 보유 {qty:,}주 | 현재 {price:,}원")


def notify_auto_sell_triggered(name: str, code: str, sell: int, sell_price: int, sell_qty: int, dry_run: bool, alert_only: bool):
    if alert_only:
        prefix = "[🔔40억감지(알림전용시간)]"
        suffix = " (10:30 이후 자동매도)"
    elif dry_run:
        prefix = "[🧪자동매도DRY]"
        suffix = ""
    else:
        prefix = "[🔴자동매도]"
        suffix = ""
    _send(
        f"{prefix} {name}({code}){suffix}\n"
        f"연기금 매도 {_won(sell)}\n"
        f"매도가 {sell_price:,}원 x {sell_qty:,}주"
    )


def notify_auto_buy(name: str, code: str, source: str, pension_amount: int, buy_price: int, buy_qty: int):
    label = "🟢자동매수(60억)" if source == "NEW_BUY" else "🔵자동매수(연일매수)"
    _send(
        f"[{label}] {name}({code})\n"
        f"트리거: {source} | 연기금 {_won(pension_amount)}\n"
        f"매수가 +0.3%: {buy_price:,}원 x {buy_qty:,}주\n"
        f"총액: {_won(buy_price * buy_qty)}"
    )


def notify_failsafe_sell(name: str, code: str, sell_price: int, sell_qty: int):
    _send(
        f"[🚨페일세이프 -2.5%] {name}({code})\n"
        f"추가 매도 {sell_price:,}원 x {sell_qty:,}주"
    )


def notify_unfilled(name: str, code: str, order_no: str, sell_price: int, remaining_qty: int):
    _send(f"[⏳미체결] {name}({code})\n주문 {order_no} | {sell_price:,}원 | 잔량 {remaining_qty:,}주")


def notify_exec(message: str, fields: Optional[list] = None):
    extra = ""
    if fields:
        extra = "\n" + " | ".join(f"{f['name']}: {f['value']}" for f in fields)
    _send(f"[📋체결] {message}{extra}")


def notify_system(message: str):
    _send(f"[⚙️시스템] {message}")
