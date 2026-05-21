"""
KRX 직접 조회 기반 연기금 매매 종목 발견 모듈.

키움 OPT10059는 종목코드 입력이 필수라 "전 종목 중 연기금 활발 종목"을 찾으려면
KOSPI ~900개를 일일이 호출해야 함. 이 한계 회피 위해 KRX 정보데이터시스템
(data.krx.co.kr)을 pykrx로 직접 호출해 **한 번에** 연기금 순매수/순매도
상위 종목을 받아온다.

장점:
- HTTP 호출 1~2회로 전 종목 랭킹 확보
- Kiwoom TR 한도와 완전 무관 (별도 채널)
- 무료, 인증 불필요

한계:
- KRX 잠정치 갱신 주기에 종속 (수 분 단위)
- pykrx가 KRX 응답 포맷 바뀌면 깨질 수 있음

설계:
- 별도 QThread에서 주기적 실행 (Qt 메인 스레드 안 막음)
- 발견된 종목을 PensionTracker에 sig_discovered(code, name, buy, sell)로 전달
- PensionTracker는 HOT으로 자동 승격 + 룰 엔진 평가
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Optional

from PyQt5.QtCore import QObject, QThread, pyqtSignal

import config

log = logging.getLogger(__name__)


class KrxDiscoveryWorker(QObject):
    """
    별도 스레드에서 KRX를 주기적으로 폴링.

    emit:
      sig_discovered(stock_code, stock_name, pension_buy, pension_sell)
      sig_status(message)
    """

    sig_discovered = pyqtSignal(str, str, int, int)
    sig_status = pyqtSignal(str)
    sig_finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        log.info("KrxDiscoveryWorker started")
        # 시작 시 즉시 1회 실행
        next_run = 0.0
        while self._running:
            now = time.time()
            if now >= next_run:
                try:
                    self._scan_once()
                except Exception:
                    log.exception("KRX scan failed")
                    self.sig_status.emit("KRX 스캔 실패 (다음 주기에 재시도)")
                next_run = now + config.KRX_DISCOVERY_INTERVAL_SEC
            # 1초 슬립 (응답성 유지)
            time.sleep(1)
        log.info("KrxDiscoveryWorker stopped")
        self.sig_finished.emit()

    # --------------------------------------------------------
    # 스캔 1회
    # --------------------------------------------------------
    def _scan_once(self):
        # 영업일 결정: 평일 장중/장후엔 오늘, 주말이면 직전 영업일
        target_date = self._latest_business_date()
        date_str = target_date.strftime("%Y%m%d")

        total_emitted = 0
        markets = []
        if "kospi" in config.UNIVERSE_MODE.lower():
            markets.append("KOSPI")
        if "kosdaq" in config.UNIVERSE_MODE.lower():
            markets.append("KOSDAQ")
        if not markets:
            markets = ["KOSPI"]

        # pykrx 대신 KRX 직접 HTTP 호출 (report/krx_http.py)
        try:
            from report import krx_http
        except ImportError:
            self.sig_status.emit("❌ report.krx_http 모듈 누락")
            log.error("report.krx_http not found")
            return

        for market in markets:
            try:
                rows = krx_http.fetch_investor_trading_by_stock(date_str, market, "연기금")
            except Exception as e:
                log.warning("KRX HTTP %s scan failed: %s", market, e)
                self.sig_status.emit(f"KRX {market} 조회 실패: {e}")
                continue

            if not rows:
                continue

            for r in rows:
                code = r.get("stock_code", "").strip()
                if not code:
                    continue
                name = r.get("stock_name", "")
                buy_amt = int(r.get("buy_amount", 0) or 0)
                sell_amt = int(r.get("sell_amount", 0) or 0)

                # 한쪽이라도 임계 이상이면 발견 신호
                if (buy_amt >= config.KRX_DISCOVERY_MIN_BUY
                        or sell_amt >= config.KRX_DISCOVERY_MIN_SELL):
                    self.sig_discovered.emit(code, name, buy_amt, sell_amt)
                    total_emitted += 1

        self.sig_status.emit(
            f"KRX 스캔 완료 [{date_str}]: 연기금 활발 종목 {total_emitted}개 발견"
        )

    @staticmethod
    def _latest_business_date() -> dt.date:
        """오늘이 영업일이면 오늘, 아니면 직전 평일 (간이 — 공휴일 미고려)."""
        today = dt.date.today()
        # 토(5), 일(6)이면 직전 금요일까지 거슬러 올라감
        d = today
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        return d


class KrxDiscoveryManager(QObject):
    """
    QThread + Worker 묶음 lifecycle 관리.
    PensionTracker에서 이 매니저를 시작/정지하고 sig_discovered 받아 처리.
    """

    sig_discovered = pyqtSignal(str, str, int, int)
    sig_status = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._thread: Optional[QThread] = None
        self._worker: Optional[KrxDiscoveryWorker] = None

    def start(self):
        if not getattr(config, "KRX_DISCOVERY_ENABLED", False):
            log.info("KRX discovery disabled by config")
            return
        if self._thread is not None:
            return
        self._thread = QThread()
        self._worker = KrxDiscoveryWorker()
        self._worker.moveToThread(self._thread)
        self._worker.sig_discovered.connect(self.sig_discovered)
        self._worker.sig_status.connect(self.sig_status)
        self._thread.started.connect(self._worker.run)
        self._worker.sig_finished.connect(self._thread.quit)
        self._thread.start()
        log.info("KrxDiscoveryManager started")

    def stop(self):
        if self._worker is not None:
            self._worker.stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        self._thread = None
        self._worker = None
        log.info("KrxDiscoveryManager stopped")
