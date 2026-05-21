"""
주문 매니저.

- AUTO_SELL 이벤트 → 1차 -0.7% 지정가 매도 (안전장치 통과 시)
- 미체결 + 추가 -2.5% 도달 → FAILSAFE_SELL 2차 매도
- 체결콜백 수신 시 DB 상태 업데이트 + 디스코드 알림

미체결 모니터링 루프:
- PRICE_POLL_INTERVAL_SEC (5초) 마다 PENDING 주문들의 종목 현재가 조회
- 트리거 가격 대비 -2.5% 이하로 떨어졌고 아직 FAILSAFE_SELL 안 했으면 추가 주문
- 그 외엔 알림(미체결 통보)만 (사용자 룰: 지정가 그대로 두기)
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

import config
import notify
from core import safety
from core.kiwoom_client import KiwoomClient, FID_ORDER_NO, FID_ORDER_STATUS, FID_UNFILLED_QTY, FID_FILLED_QTY, FID_FILLED_PRICE, FID_STOCK_CODE
from core.rule_engine import Event
from storage import db

log = logging.getLogger(__name__)


class OrderManager(QObject):
    """AUTO_SELL/FAILSAFE_SELL/AUTO_BUY 처리 + 미체결 관리"""

    sig_holdings_dirty = pyqtSignal()  # 매수/매도 체결 시 보유종목 갱신 트리거

    def __init__(self, kiwoom: KiwoomClient, account: str):
        super().__init__()
        self.kiwoom = kiwoom
        self.account = account
        # rqname → order_id (SendOrder 직후 매핑, 체결콜백에서 order_no 받으면 DB 업데이트)
        self._pending_rqname_to_id: Dict[str, int] = {}
        # stock_code → trigger_price (1차 발동 당시 가격, FAILSAFE 판단용)
        self._trigger_prices: Dict[str, int] = {}
        # 5초마다 미체결 모니터링
        self._monitor = QTimer()
        self._monitor.setInterval(config.PRICE_POLL_INTERVAL_SEC * 1000)
        self._monitor.timeout.connect(self._monitor_unfilled)
        # 체결 시그널 연결
        self.kiwoom.sig_chejan.connect(self.on_chejan)

    def start(self):
        self._monitor.start()

    def stop(self):
        self._monitor.stop()

    # --------------------------------------------------------
    # 이벤트 디스패처 (PensionTracker.sig_event 에 연결)
    # --------------------------------------------------------
    @pyqtSlot(object)
    def on_event(self, ev: Event):
        if ev.trigger_type == "AUTO_SELL":
            self._handle_auto_sell(ev)
        elif ev.trigger_type == "NEW_BUY" and config.AUTO_BUY_ENABLED and config.AUTO_BUY_ON_NEW_BUY:
            self._handle_auto_buy(ev, source="NEW_BUY")
        elif ev.trigger_type == "CONSECUTIVE_BUY" and config.AUTO_BUY_ENABLED and config.AUTO_BUY_ON_CONSECUTIVE:
            self._handle_auto_buy(ev, source="CONSECUTIVE_BUY")

    # --------------------------------------------------------
    # AUTO_SELL 처리
    # --------------------------------------------------------
    def _handle_auto_sell(self, ev: Event):
        if safety.already_triggered(ev.stock_code, "AUTO_SELL"):
            return

        # 알림은 항상 발송 (시간/킬스위치/드라이런과 무관)
        cur = ev.current_price or self.kiwoom.fetch_current_price(ev.stock_code)
        primary_price = int(cur * (1 + config.SELL_PRICE_OFFSET_PRIMARY))
        primary_price = safety.round_to_tick(primary_price, direction="down")

        alert_only = safety.is_alert_only_window()
        if alert_only or config.DRY_RUN or safety.kill_switch_engaged():
            notify.notify_auto_sell_triggered(
                ev.stock_name, ev.stock_code, ev.pension_amount,
                primary_price, ev.holding_qty,
                dry_run=config.DRY_RUN, alert_only=alert_only,
            )
            # 알림전용 시간대에선 멱등 마킹 안 함 (10:30 이후 다시 평가되도록)
            if config.DRY_RUN or safety.kill_switch_engaged():
                safety.mark_triggered(ev.stock_code, ev.stock_name, "AUTO_SELL", ev.pension_amount, payload={
                    "reason": "dry_run_or_kill", "would_price": primary_price,
                })
            return

        # 안전장치 평가
        decision = safety.evaluate_sell(
            stock_code=ev.stock_code,
            stock_name=ev.stock_name,
            desired_price=primary_price,
            desired_qty=ev.holding_qty,
            current_price=cur,
            holding_qty=ev.holding_qty,
        )
        if not decision.allowed:
            log.warning("AUTO_SELL %s blocked: %s", ev.stock_code, decision.reason)
            notify.notify_system(f"⛔ 자동매도 차단: {ev.stock_name} ({ev.stock_code}) - {decision.reason}")
            return

        # 1차 매도 주문
        order_id = db.insert_order(
            ev.stock_code, ev.stock_name, "SELL", "PRIMARY",
            decision.final_price, decision.final_qty,
            note=f"trigger pension_sell={ev.pension_amount:,}",
        )
        rq = f"sell_pri_{order_id}"
        self._pending_rqname_to_id[rq] = order_id
        ret = self.kiwoom.send_order_sell_limit(self.account, ev.stock_code, decision.final_qty, decision.final_price, rq_name=rq)
        if ret != 0:
            db.update_order_status_by_id(order_id, "FAILED")
            notify.notify_system(f"❌ 주문 실패: {ev.stock_name} ({ev.stock_code}) ret={ret}")
            return

        # 트리거 마킹 + 한도 가산 + FAILSAFE 기준가 저장
        safety.mark_triggered(ev.stock_code, ev.stock_name, "AUTO_SELL", ev.pension_amount, payload={
            "primary_price": decision.final_price, "primary_qty": decision.final_qty,
            "trigger_current": cur,
        })
        db.add_sell_total(ev.stock_code, decision.final_price * decision.final_qty)
        self._trigger_prices[ev.stock_code] = cur   # 트리거 시점 가격 저장

        notify.notify_auto_sell_triggered(
            ev.stock_name, ev.stock_code, ev.pension_amount,
            decision.final_price, decision.final_qty,
            dry_run=False, alert_only=False,
        )

    # --------------------------------------------------------
    # AUTO_BUY 처리 (NEW_BUY / CONSECUTIVE_BUY 이벤트 기반)
    # --------------------------------------------------------
    def _handle_auto_buy(self, ev: Event, source: str = "NEW_BUY"):
        # 같은 종목 당일 1회만 (NEW_BUY든 CONSECUTIVE_BUY든 한 번만 매수)
        if safety.already_triggered(ev.stock_code, "AUTO_BUY"):
            return

        # 현재가 + 시가 조회 (갭상승 판단용)
        try:
            info = self.kiwoom.fetch_basic_info(ev.stock_code)
        except Exception:
            log.exception("fetch_basic_info failed: %s", ev.stock_code)
            return
        cur = int(info.get("current_price", 0))
        open_price = int(info.get("open_price", 0))
        if cur <= 0:
            log.warning("AUTO_BUY %s: current_price=0, skip", ev.stock_code)
            return

        # 매수 가격: 현재가 +0.3% 지정가 (체결률↑)
        target_price = int(cur * (1 + config.BUY_PRICE_OFFSET))
        target_price = safety.round_to_tick(target_price, direction="up")

        # 예수금 조회
        try:
            cash = self.kiwoom.fetch_cash(self.account)
        except Exception:
            log.exception("fetch_cash failed")
            cash = 0

        # 1회 매수 한도까지 시도
        decision = safety.evaluate_buy(
            stock_code=ev.stock_code,
            stock_name=ev.stock_name,
            desired_price=target_price,
            desired_amount=config.MAX_BUY_PER_ORDER,
            current_price=cur,
            available_cash=cash,
            today_open_price=open_price,
        )

        # 알림전용 시간대거나 거부됐을 때
        if not decision.allowed:
            # 알림전용 시간대면 "감지" 알림만
            if "alert-only window" in decision.reason:
                notify.notify_system(
                    f"🔔 [알림전용시간] 매수 신호 감지 ({source}): {ev.stock_name} ({ev.stock_code}) - 10:30 이후 자동매수 활성"
                )
            elif decision.reason in ("AUTO_BUY_ENABLED=False",):
                notify.notify_system(
                    f"🔔 매수 신호 ({source}): {ev.stock_name} ({ev.stock_code}) - 자동매수 비활성"
                )
            else:
                log.warning("AUTO_BUY %s blocked: %s", ev.stock_code, decision.reason)
                notify.notify_system(
                    f"⛔ 자동매수 차단: {ev.stock_name} ({ev.stock_code}) - {decision.reason}"
                )
            return

        # 주문 실행
        order_id = db.insert_order(
            ev.stock_code, ev.stock_name, "BUY", "PRIMARY",
            decision.final_price, decision.final_qty,
            note=f"auto_buy source={source} pension_amount={ev.pension_amount:,}",
        )
        rq = f"buy_{order_id}"
        self._pending_rqname_to_id[rq] = order_id
        ret = self.kiwoom.send_order_buy_limit(self.account, ev.stock_code, decision.final_qty, decision.final_price, rq_name=rq)
        if ret != 0:
            db.update_order_status_by_id(order_id, "FAILED")
            notify.notify_system(f"❌ 매수 주문 실패: {ev.stock_name} ({ev.stock_code}) ret={ret}")
            return

        # 트리거 마킹 + 한도 가산
        safety.mark_triggered(ev.stock_code, ev.stock_name, "AUTO_BUY", ev.pension_amount, payload={
            "source": source,
            "buy_price": decision.final_price,
            "buy_qty": decision.final_qty,
            "current_price": cur,
            "open_price": open_price,
        })
        db.add_buy_total(ev.stock_code, decision.final_price * decision.final_qty)

        # 알림
        notify.notify_auto_buy(
            ev.stock_name, ev.stock_code,
            source=source,
            pension_amount=ev.pension_amount,
            buy_price=decision.final_price,
            buy_qty=decision.final_qty,
        )

    # --------------------------------------------------------
    # 미체결 모니터링 → FAILSAFE
    # --------------------------------------------------------
    @pyqtSlot()
    def _monitor_unfilled(self):
        try:
            pendings = db.get_pending_orders()
        except Exception:
            log.exception("get_pending_orders failed")
            return
        for o in pendings:
            code = o["stock_code"]
            if o["order_type"] == "FAILSAFE":
                continue  # 이미 FAILSAFE인 건 모니터링 대상 X
            trigger_price = self._trigger_prices.get(code)
            if not trigger_price:
                continue
            try:
                cur = self.kiwoom.fetch_current_price(code)
            except Exception:
                log.exception("fetch_current_price failed: %s", code)
                continue
            if cur <= 0:
                continue
            # 트리거가 대비 -2.5% 이하인지
            drop_pct = (cur - trigger_price) / trigger_price
            if drop_pct > config.SELL_PRICE_OFFSET_FAILSAFE:
                continue
            if safety.already_triggered(code, "FAILSAFE_SELL"):
                continue
            self._fire_failsafe(o, cur, trigger_price)

    def _fire_failsafe(self, primary_order, current_price: int, trigger_price: int):
        code = primary_order["stock_code"]
        name = primary_order["stock_name"]
        # 잔여 미체결 수량
        remaining = primary_order["qty"] - primary_order["filled_qty"]
        if remaining <= 0:
            return

        # -2.5% 가격 = trigger_price * 0.975
        failsafe_price = int(trigger_price * (1 + config.SELL_PRICE_OFFSET_FAILSAFE))
        failsafe_price = safety.round_to_tick(failsafe_price, direction="down")

        decision = safety.evaluate_sell(
            stock_code=code, stock_name=name,
            desired_price=failsafe_price,
            desired_qty=remaining,
            current_price=current_price,
            holding_qty=remaining,
        )
        if not decision.allowed:
            log.warning("FAILSAFE %s blocked: %s", code, decision.reason)
            notify.notify_system(f"⛔ 페일세이프 차단: {name} ({code}) - {decision.reason}")
            return

        order_id = db.insert_order(
            code, name, "SELL", "FAILSAFE",
            decision.final_price, decision.final_qty,
            parent_order_id=primary_order["id"],
            note=f"failsafe trigger_price={trigger_price} cur={current_price}",
        )
        rq = f"sell_fs_{order_id}"
        self._pending_rqname_to_id[rq] = order_id
        ret = self.kiwoom.send_order_sell_limit(self.account, code, decision.final_qty, decision.final_price, rq_name=rq)
        if ret != 0:
            notify.notify_system(f"❌ 페일세이프 주문 실패: {name} ({code}) ret={ret}")
            return

        safety.mark_triggered(code, name, "FAILSAFE_SELL", 0, payload={
            "failsafe_price": decision.final_price,
            "trigger_price": trigger_price,
            "current": current_price,
        })
        db.add_sell_total(code, decision.final_price * decision.final_qty)
        notify.notify_failsafe_sell(name, code, decision.final_price, decision.final_qty)

    # --------------------------------------------------------
    # 체결콜백 → DB 상태 업데이트
    # --------------------------------------------------------
    @pyqtSlot(str, dict)
    def on_chejan(self, gubun: str, fids: dict):
        # gubun '0' = 주문체결, '1' = 잔고변경
        if gubun != "0":
            return
        order_no = fids.get(FID_ORDER_NO, "").strip()
        status = fids.get(FID_ORDER_STATUS, "")
        unfilled = self._safe_int(fids.get(FID_UNFILLED_QTY, "0"))
        filled = self._safe_int(fids.get(FID_FILLED_QTY, "0"))
        code = fids.get(FID_STOCK_CODE, "").lstrip("A")
        if not order_no:
            return

        # 주문번호 매핑이 아직 없으면 (rqname → order_id), 첫 콜백 시 가장 최근 PENDING 주문에 붙임
        existing = db.get_order_by_no(order_no)
        if not existing and self._pending_rqname_to_id:
            # 가장 최근 추가된 rq 매핑 하나 가져와서 연결
            rq, order_id = next(iter(self._pending_rqname_to_id.items()))
            db.update_order_no(order_id, order_no)
            self._pending_rqname_to_id.pop(rq, None)
            existing = db.get_order_by_no(order_no)

        if status == "체결":
            new_status = "FILLED" if unfilled == 0 else "PARTIAL"
        elif status == "확인" and unfilled == 0:
            new_status = "CANCELED" if filled == 0 else "FILLED"
        else:
            new_status = "PENDING"

        db.update_order_status(order_no, new_status, filled_qty=filled)

        if existing:
            stock_name = existing["stock_name"]
            side_label = "매수" if existing["side"] == "BUY" else "매도"
            if new_status == "FILLED":
                notify.notify_exec(f"✅ {side_label} 체결 완료: {stock_name} ({code})", fields=[
                    {"name": "체결수량", "value": f"{filled:,}주", "inline": True},
                    {"name": "주문번호", "value": order_no, "inline": True},
                ])
                # 매수 체결 시 보유종목 갱신 시그널 (PensionTracker가 받아서 holdings 새로고침)
                if existing["side"] == "BUY":
                    self.sig_holdings_dirty.emit()
            elif new_status == "PARTIAL":
                notify.notify_exec(f"🟡 {side_label} 부분체결: {stock_name} ({code})", fields=[
                    {"name": "체결/주문", "value": f"{filled:,}/{existing['qty']:,}주", "inline": True},
                ])
            elif new_status == "CANCELED":
                notify.notify_exec(f"❎ {side_label} 주문취소: {stock_name} ({code})")

    @staticmethod
    def _safe_int(s):
        try:
            return int(str(s).replace(",", "").replace("+", "").replace("-", "").strip() or 0)
        except Exception:
            return 0
