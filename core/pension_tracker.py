"""
연기금 데이터 추적 + 룰 엔진 트리거.

폴링 전략:
- 30초마다 watchlist의 각 종목에 대해 OPT10059 호출 → 연기금 순매수액 갱신
- watchlist = 보유종목 ∪ 사전 등록한 관심종목 (※ 매수 상위 N 추적은 OPT10009 등 별도 TR 필요, 본 스켈레톤에선 미구현)
- 갱신 후 룰 엔진 평가 → 트리거 이벤트 → 알림 + 자동매도

OPT10059 한계:
- 순매수만 반환 (매수/매도 분리 X) → "20억+ 매도" 판정은 순매수가 음수일 때 |값| 기준
- 잠정치 (장중 갱신)
- 호출 단가 큼 → 1초 5건 제한 고려 (kiwoom_client에서 자동 sleep)
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

import config
import notify
from core import rule_engine, safety
from core.kiwoom_client import KiwoomClient
from core.rule_engine import Event
from storage import db

log = logging.getLogger(__name__)


class PensionTracker(QObject):
    """
    주기적으로 연기금 데이터를 폴링하고 룰 엔진을 통해 트리거 이벤트를 emit.

    폴링 전략 (HOT/COLD 2-tier):
    - HOT  : 최근 연기금 거래가 발견된 활발 종목 (config.HOT_MAX_COUNT 개)
             → 매 tick마다 우선 폴링 (60초 cycle)
             → 실시간 시세(FID 10 현재가) 구독으로 가격은 진짜 라이브
    - COLD : 나머지 KOSPI/KOSDAQ 종목
             → 남는 batch 슬롯으로 라운드로빈
    - 자동 승격 : poll 결과 |연기금 매수-매도| >= HOT_PROMOTION_MIN_AMOUNT면 HOT으로
    - 자동 강등 : HOT_DEMOTE_AFTER_SEC 동안 활동 없으면 HOT에서 제거
    """

    sig_event = pyqtSignal(object)             # rule_engine.Event 인스턴스
    sig_data_updated = pyqtSignal(str, dict)   # (stock_code, snapshot)
    sig_status = pyqtSignal(str)               # 진행 상태 메시지
    sig_hot_changed = pyqtSignal(set)          # 현재 HOT 종목 set 변경 시 emit

    def __init__(self, kiwoom: KiwoomClient, account: str):
        super().__init__()
        self.kiwoom = kiwoom
        self.account = account
        self.watchlist: set = set()           # 종목코드 set (전체)
        self.holdings_map: dict = {}           # code → holding dict
        # HOT/COLD tier 상태
        self._hot_set: set = set()             # 활발한 종목 (우선 폴링 대상)
        self._hot_last_activity: dict = {}     # code → unix timestamp
        self._latest_snapshot: dict = {}       # code → 최근 snapshot (실시간 시세 머지용)
        self._realtime_subscribed: set = set() # 실시간 시세 구독 중인 코드
        self._timer = QTimer()
        self._timer.setInterval(config.PENSION_POLL_INTERVAL_SEC * 1000)
        self._timer.timeout.connect(self._tick)
        # 보유종목 주기적 새로고침 (5분) — 자동매수 체결 후 holdings 반영
        self._holdings_refresh_timer = QTimer()
        self._holdings_refresh_timer.setInterval(5 * 60 * 1000)
        self._holdings_refresh_timer.timeout.connect(self._refresh_holdings)
        # HOT demote 검사 (1분 간격)
        self._demote_timer = QTimer()
        self._demote_timer.setInterval(60 * 1000)
        self._demote_timer.timeout.connect(self._demote_stale_hot)
        self._cursor = 0  # COLD 라운드로빈 인덱스 (rate limit)
        # 실시간 시세 콜백 연결
        self.kiwoom.sig_real_data.connect(self._on_real_data)
        # KRX 직접 발견 매니저 (별도 스레드)
        from core.krx_discovery import KrxDiscoveryManager
        self.krx_discovery = KrxDiscoveryManager()
        self.krx_discovery.sig_discovered.connect(self._on_krx_discovered)
        self.krx_discovery.sig_status.connect(self.sig_status)

    # --------------------------------------------------------
    # 시작 / 정지
    # --------------------------------------------------------
    def start(self):
        self._refresh_holdings()
        self._load_watchlist_file()
        self._load_market_universe()
        self._load_watchlist_from_db()
        # 보유종목은 무조건 HOT (자동매도 트리거 즉시성)
        for code in self.holdings_map.keys():
            self._promote_to_hot(code, reason="holding")
        self._timer.start()
        self._holdings_refresh_timer.start()
        self._demote_timer.start()
        # KRX 직접 발견 모듈 시작 (별도 스레드)
        self.krx_discovery.start()
        # 시작 즉시 1회
        QTimer.singleShot(500, self._tick)

    def _load_market_universe(self):
        """
        config.UNIVERSE_MODE 에 따라 KOSPI/KOSDAQ 전 종목을 watchlist에 추가.
        이렇게 해야 "사전 등록한 종목"이 아닌 시장 전체에서 연기금 매수/매도 활동을
        자동 발견할 수 있음.
        """
        try:
            from core import universe
            codes = universe.load_universe(self.kiwoom)
            if not codes:
                return
            before = len(self.watchlist)
            self.watchlist.update(codes)
            added = len(self.watchlist) - before
            mode = getattr(config, "UNIVERSE_MODE", "watchlist")
            log.info("Market universe(%s): %d new codes (total watchlist=%d)",
                     mode, added, len(self.watchlist))
            self.sig_status.emit(
                f"유니버스 로드({mode}): {added}개 종목 추가 (총 {len(self.watchlist)}개 추적)"
            )
        except Exception:
            log.exception("_load_market_universe failed")

    def _load_watchlist_from_db(self):
        """최근 5일간 연기금이 활발히 거래한 종목을 자동으로 워치리스트에 추가 (자체 학습)"""
        try:
            codes = db.get_recently_active_stocks(days_back=5, min_abs_amount=1_000_000_000)
            if codes:
                before = len(self.watchlist)
                self.watchlist.update(codes)
                added = len(self.watchlist) - before
                log.info("DB-based watchlist: %d new (recent active pension stocks)", added)
                self.sig_status.emit(f"DB 자체 학습: 최근 5일 연기금 활발 종목 {added}개 추가")
        except Exception:
            log.exception("_load_watchlist_from_db failed")

    def _load_watchlist_file(self):
        """watchlist.txt 읽어서 워치리스트에 추가"""
        from pathlib import Path
        path = Path("watchlist.txt")
        if not path.exists():
            return
        added = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 주석 부분 제거 (코드  # 종목명)
            code = line.split("#", 1)[0].split()[0].strip()
            if code and code.isdigit() and len(code) == 6:
                self.watchlist.add(code)
                added += 1
        log.info("watchlist.txt: %d stocks loaded", added)
        self.sig_status.emit(f"워치리스트 {added}개 종목 추가 (보유종목 별도)")

    def stop(self):
        self._timer.stop()
        self._holdings_refresh_timer.stop()
        self._demote_timer.stop()
        # KRX 발견 매니저 정지
        try:
            self.krx_discovery.stop()
        except Exception:
            pass
        # 실시간 구독 해제
        if self._realtime_subscribed:
            try:
                self.kiwoom.unsubscribe_real(list(self._realtime_subscribed))
            except Exception:
                pass
            self._realtime_subscribed.clear()

    @pyqtSlot()
    def request_holdings_refresh(self):
        """외부(OrderManager 체결콜백)에서 즉시 갱신 요청. 체결 후 잔고 반영 대기 위해 2초 지연."""
        QTimer.singleShot(2000, self._refresh_holdings)

    def add_to_watchlist(self, codes):
        if isinstance(codes, str):
            codes = [codes]
        self.watchlist.update(codes)

    # --------------------------------------------------------
    # 보유종목 동기화
    # --------------------------------------------------------
    def _refresh_holdings(self):
        try:
            holdings = self.kiwoom.fetch_holdings(self.account)
        except Exception:
            log.exception("fetch_holdings failed")
            return
        self.holdings_map = {h["stock_code"]: h for h in holdings}
        db.replace_holdings(holdings)
        for h in holdings:
            self.watchlist.add(h["stock_code"])
        self.sig_status.emit(f"보유종목 {len(holdings)}건 동기화 완료")

    # --------------------------------------------------------
    # 폴링 1회 (HOT 우선 → COLD 라운드로빈으로 batch 채움)
    # --------------------------------------------------------
    @pyqtSlot()
    def _tick(self):
        if not self.watchlist:
            return
        bs = config.WATCHLIST_BATCH_SIZE

        # 1단계: HOT 종목 전부 (batch_size 한도 내)
        hot_codes = list(self._hot_set)
        batch = hot_codes[:bs]
        remaining = bs - len(batch)

        # 2단계: 남는 슬롯에 COLD 라운드로빈
        if remaining > 0:
            cold_codes = sorted(self.watchlist - self._hot_set)
            if cold_codes:
                cold_batch = cold_codes[self._cursor:self._cursor + remaining]
                if not cold_batch:
                    self._cursor = 0
                    cold_batch = cold_codes[:remaining]
                else:
                    self._cursor += remaining
                batch.extend(cold_batch)

        for code in batch:
            self._poll_one(code)

    # --------------------------------------------------------
    # HOT tier 관리
    # --------------------------------------------------------
    def _promote_to_hot(self, code: str, reason: str = "activity"):
        """종목을 HOT으로 승격. 만석이면 가장 오래된 종목 강등."""
        now = time.time()
        if code in self._hot_set:
            self._hot_last_activity[code] = now
            return
        # 용량 초과 시 LRU 강등 (보유종목은 절대 강등 X)
        if len(self._hot_set) >= config.HOT_MAX_COUNT:
            candidates = [c for c in self._hot_set if c not in self.holdings_map]
            if not candidates:
                return  # 보유종목만 가득찬 경우 추가 불가
            oldest = min(candidates, key=lambda c: self._hot_last_activity.get(c, 0))
            self._hot_set.discard(oldest)
            self._hot_last_activity.pop(oldest, None)
            self._unsubscribe_realtime(oldest)
            log.info("HOT demote (LRU): %s", oldest)
        self._hot_set.add(code)
        self._hot_last_activity[code] = now
        if config.ENABLE_REALTIME_PRICE:
            self._subscribe_realtime(code)
        log.info("HOT promote (%s): %s [size=%d]", reason, code, len(self._hot_set))
        self.sig_hot_changed.emit(set(self._hot_set))

    @pyqtSlot()
    def _demote_stale_hot(self):
        """일정 시간 활동 없는 HOT 종목 강등 (보유종목은 제외)."""
        now = time.time()
        threshold = now - config.HOT_DEMOTE_AFTER_SEC
        to_remove = [
            c for c in self._hot_set
            if c not in self.holdings_map
            and self._hot_last_activity.get(c, 0) < threshold
        ]
        if not to_remove:
            return
        for c in to_remove:
            self._hot_set.discard(c)
            self._hot_last_activity.pop(c, None)
            self._unsubscribe_realtime(c)
        log.info("HOT demote (stale): %d codes [size=%d]", len(to_remove), len(self._hot_set))
        self.sig_hot_changed.emit(set(self._hot_set))

    def _subscribe_realtime(self, code: str):
        """실시간 시세 구독 (FID 10 현재가, 13 누적거래량, 15 거래량)."""
        if code in self._realtime_subscribed:
            return
        try:
            self.kiwoom.subscribe_real([code], ["10", "13", "15"], screen="9999", append=True)
            self._realtime_subscribed.add(code)
        except Exception:
            log.exception("subscribe_real failed: %s", code)

    def _unsubscribe_realtime(self, code: str):
        if code not in self._realtime_subscribed:
            return
        try:
            self.kiwoom.unsubscribe_real([code])
        except Exception:
            pass
        self._realtime_subscribed.discard(code)

    # --------------------------------------------------------
    # KRX 발견 신호 처리 (krx_discovery 스레드 → 메인 스레드)
    # --------------------------------------------------------
    @pyqtSlot(str, str, int, int)
    def _on_krx_discovered(self, code: str, name: str, krx_buy: int, krx_sell: int):
        """
        KRX 직접 조회로 발견된 연기금 활발 종목.

        설계 방침:
        - KRX 데이터는 '잠정치 + 시장 단위 배치' 라 자동매수/매도 직접 트리거에는
          부적합 (재진입 위험 + 가격 미확보).
        - 여기선 '발견'만 하고 watchlist 추가 + HOT 승격까지.
        - 실제 trigger는 다음 _tick에서 OPT10059 로 정밀 폴링하면서 발생.
        - 즉, KRX 는 "어떤 종목을 자세히 볼지" 알려주는 역할만 함.
        """
        try:
            net = krx_buy - krx_sell
            self.watchlist.add(code)
            self._promote_to_hot(code, reason=f"KRX net={net:,}")

            # DB 잠정치 기록 (KRX 출처, is_confirmed=False)
            db.upsert_pension_daily(code, name, buy=krx_buy, sell=krx_sell, is_confirmed=False)

            holding_qty = self.holdings_map.get(code, {}).get("qty", 0)
            cur_price = self.holdings_map.get(code, {}).get("current_price", 0)

            snapshot = {
                "name": name, "buy": krx_buy, "sell": krx_sell,
                "net": net, "current_price": cur_price, "holding_qty": holding_qty,
                "is_hot": True, "source": "KRX",
            }
            self._latest_snapshot[code] = snapshot
            self.sig_data_updated.emit(code, snapshot)

            log.info("KRX discovered: %s(%s) buy=%s sell=%s net=%s (next _tick 에서 OPT10059 폴링)",
                     name, code, krx_buy, krx_sell, net)
        except Exception:
            log.exception("_on_krx_discovered failed: %s", code)

    @pyqtSlot(str, str, dict)
    def _on_real_data(self, code: str, real_type: str, fid_dict: dict):
        """실시간 시세 콜백 → 현재가 갱신 후 GUI에 즉시 emit."""
        if code not in self._latest_snapshot:
            return
        try:
            raw = self.kiwoom.get_real_data(code, "10") or "0"
            cur = abs(int(raw.replace(",", "").replace("+", "").strip() or 0))
            if cur <= 0:
                return
            snap = self._latest_snapshot[code]
            if snap.get("current_price") == cur:
                return  # 변동 없으면 패스 (GUI 부하 회피)
            snap["current_price"] = cur
            self.sig_data_updated.emit(code, snap)
        except Exception:
            log.exception("_on_real_data failed: %s", code)

    def _poll_one(self, code: str):
        try:
            data = self.kiwoom.fetch_pension_today(code)
            # data 가 비어있어도 0 값으로 모니터 탭 갱신
            buy_amount = int(data.get("pension_buy", 0)) if data else 0
            sell_amount = int(data.get("pension_sell", 0)) if data else 0
            net = buy_amount - sell_amount
            close = int(data.get("close", 0)) if data else 0
            name = self.kiwoom.get_master_code_name(code) or code

            db.upsert_pension_daily(code, name, buy=buy_amount, sell=sell_amount, is_confirmed=False)

            holding_qty = self.holdings_map.get(code, {}).get("qty", 0)
            cur_price = close or self.holdings_map.get(code, {}).get("current_price", 0)

            snapshot = {
                "name": name, "buy": buy_amount, "sell": sell_amount,
                "net": net, "current_price": cur_price, "holding_qty": holding_qty,
                "is_hot": code in self._hot_set,
            }
            self._latest_snapshot[code] = snapshot
            self.sig_data_updated.emit(code, snapshot)

            # HOT 자동 승격: |순매수| >= HOT_PROMOTION_MIN_AMOUNT
            if abs(net) >= config.HOT_PROMOTION_MIN_AMOUNT:
                self._promote_to_hot(code, reason=f"net={net:,}")
            elif code in self._hot_set and (buy_amount > 0 or sell_amount > 0):
                # 활동 있으면 last_activity 갱신 (강등 지연)
                self._hot_last_activity[code] = time.time()

            # 룰 평가
            events = rule_engine.evaluate_for_stock(
                code, name,
                pension_buy=buy_amount,
                pension_sell=sell_amount,
                current_price=cur_price,
                holding_qty=holding_qty,
            )

            # 연일매수도 평가 (오늘 잠정치가 양수일 때만)
            if buy_amount > 0:
                ce = rule_engine.evaluate_consecutive_buy(code, name, cur_price)
                if ce:
                    events.append(ce)

            for ev in events:
                self._handle_event(ev)
        except Exception:
            log.exception("_poll_one failed for %s", code)

    # --------------------------------------------------------
    # 이벤트 처리 (멱등 마킹 + 시그널 emit)
    # --------------------------------------------------------
    def _handle_event(self, ev: Event):
        # 멱등 마킹은 외부 핸들러가 주문/알림 처리 후 호출하는 게 정확하지만,
        # 알림성 이벤트는 여기서 즉시 마킹.
        if ev.trigger_type in ("NEW_BUY", "HOLD_WARN", "CONSECUTIVE_BUY"):
            safety.mark_triggered(ev.stock_code, ev.stock_name, ev.trigger_type, ev.pension_amount, payload={
                "current_price": ev.current_price,
                "extra": ev.extra,
            })
            self._dispatch_alert(ev)
        # AUTO_SELL 은 OrderManager가 처리 후 마킹
        self.sig_event.emit(ev)

    def _dispatch_alert(self, ev: Event):
        if ev.trigger_type == "NEW_BUY":
            notify.notify_new_buy(ev.stock_name, ev.stock_code, ev.pension_amount, ev.current_price)
        elif ev.trigger_type == "HOLD_WARN":
            notify.notify_hold_warn(ev.stock_name, ev.stock_code, ev.pension_amount, ev.holding_qty, ev.current_price)
        elif ev.trigger_type == "CONSECUTIVE_BUY":
            days = ev.extra.get("days", 0)
            notify.notify_consecutive_buy(ev.stock_name, ev.stock_code, days, ev.pension_amount)
