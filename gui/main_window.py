"""
PyQt5 메인 윈도우.

탭 구성:
1. 신규매수    : 연기금 70억+ 매수 종목
2. 연일매수    : 2일+ 연속 순매수
3. 보유종목 경고 : 20억+ 매도 또는 자동매도 발생

상단 툴바:
- 모드 표시 (LIVE / DRY_RUN)
- 시간대 표시 (알림전용 / 자동매도활성)
- 일일 매도 누적 / 한도
- 🛑 킬스위치 버튼 (Ctrl+Shift+K)

하단:
- 실행 로그 스트림
"""
from __future__ import annotations

import datetime as dt
import logging

from PyQt5.QtCore import Qt, QTimer, pyqtSlot
from PyQt5.QtGui import QColor, QKeySequence, QFont
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QTableWidget,
    QTableWidgetItem, QPushButton, QLabel, QPlainTextEdit, QShortcut, QMessageBox,
    QHeaderView,
)

import config
import notify
from core import safety
from core.rule_engine import Event
from storage import db


def _fmt_won(amount: int) -> str:
    if abs(amount) >= 100_000_000:
        return f"{amount / 100_000_000:.2f}억"
    if abs(amount) >= 10_000:
        return f"{amount / 10_000:.0f}만"
    return f"{amount:,}원"


class _Table(QTableWidget):
    def __init__(self, headers):
        super().__init__(0, len(headers))
        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        h = self.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Stretch)
        self._index = {}  # 종목코드 → row index

    def upsert(self, key: str, cells: list, color: QColor = None):
        if key in self._index:
            row = self._index[key]
        else:
            row = self.rowCount()
            self.insertRow(row)
            self._index[key] = row
        for c, val in enumerate(cells):
            item = QTableWidgetItem(val)
            if color:
                item.setForeground(color)
            self.setItem(row, c, item)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("연기금 자동매매 봇")
        self.resize(1200, 800)

        # 상단 상태 표시줄
        top = QWidget()
        top_layout = QHBoxLayout(top)
        self.lbl_mode = QLabel(self._mode_text())
        self.lbl_mode.setStyleSheet("font-weight: bold; padding: 4px 8px; border-radius: 4px;")
        self._update_mode_style()
        self.lbl_window = QLabel("--")
        self.lbl_total = QLabel("매도누적: 0원 / 한도: " + _fmt_won(config.MAX_SELL_TOTAL_DAILY))
        self.btn_kill = QPushButton("🛑 전체 정지 (Ctrl+Shift+K)")
        self.btn_kill.setStyleSheet("background-color: #c0392b; color: white; font-weight: bold; padding: 8px 16px;")
        self.btn_kill.clicked.connect(self._engage_kill)
        self.btn_release = QPushButton("정지 해제")
        self.btn_release.setEnabled(False)
        self.btn_release.clicked.connect(self._release_kill)
        top_layout.addWidget(self.lbl_mode)
        top_layout.addWidget(self.lbl_window)
        top_layout.addStretch(1)
        top_layout.addWidget(self.lbl_total)
        top_layout.addWidget(self.btn_kill)
        top_layout.addWidget(self.btn_release)

        # 탭
        self.tabs = QTabWidget()
        self.tab_hot = _Table(["갱신", "종목명", "코드", "연기금 매수", "연기금 매도", "순매수", "현재가", "보유"])
        self.tab_new_buy = _Table(["시각", "종목명", "코드", "연기금 매수", "현재가"])
        self.tab_consecutive = _Table(["시각", "종목명", "코드", "연속일수", "누적 순매수"])
        self.tab_hold = _Table(["시각", "종목명", "코드", "구분", "연기금 매도", "보유수량", "현재가"])
        self.tab_monitor = _Table(["갱신", "종목명", "코드", "연기금 매수", "연기금 매도", "순매수", "현재가", "보유"])
        self.tabs.addTab(self.tab_hot, "🔥 실시간 활발")
        self.tabs.addTab(self.tab_new_buy, "신규매수 (60억+)")
        self.tabs.addTab(self.tab_consecutive, "연일매수")
        self.tabs.addTab(self.tab_hold, "보유종목 경고")
        self.tabs.addTab(self.tab_monitor, "모니터링 (전체 워치리스트)")
        # 🔥 탭은 HOT 종목만 표시. on_data_updated에서 is_hot=True인 것만 upsert.
        self._hot_codes: set = set()           # 현재 HOT 종목 (sig_hot_changed로 갱신)

        # 로그
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFont(QFont("Consolas", 9))

        central = QWidget()
        v = QVBoxLayout(central)
        v.addWidget(top)
        v.addWidget(self.tabs, 5)
        v.addWidget(QLabel("실행 로그"))
        v.addWidget(self.log_view, 2)
        self.setCentralWidget(central)

        # 단축키
        QShortcut(QKeySequence("Ctrl+Shift+K"), self, activated=self._engage_kill)

        # 1초마다 상태 갱신
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._refresh_status)
        self._tick_timer.start()
        self._refresh_status()

    # ------------------------------------------------------
    # 상태/모드
    # ------------------------------------------------------
    def _mode_text(self):
        if safety.kill_switch_engaged():
            return "🛑 STOPPED"
        if config.DRY_RUN:
            return "🧪 DRY_RUN"
        if config.KIWOOM_MOCK_TRADING:
            return "🧪 모의투자"
        return "🟢 LIVE (실계좌)"

    def _update_mode_style(self):
        if safety.kill_switch_engaged():
            self.lbl_mode.setStyleSheet("background-color:#c0392b;color:white;font-weight:bold;padding:4px 8px;")
        elif config.DRY_RUN or config.KIWOOM_MOCK_TRADING:
            self.lbl_mode.setStyleSheet("background-color:#8e44ad;color:white;font-weight:bold;padding:4px 8px;")
        else:
            self.lbl_mode.setStyleSheet("background-color:#27ae60;color:white;font-weight:bold;padding:4px 8px;")

    def _refresh_status(self):
        self.lbl_mode.setText(self._mode_text())
        self._update_mode_style()
        if not safety.now_within_market():
            win = "장외시간"
        elif safety.is_alert_only_window():
            win = "🔔 알림전용 (09:00~10:30)"
        else:
            win = "✅ 자동매도 활성 (10:30~15:30)"
        self.lbl_window.setText(f"  |  {win}  |  {dt.datetime.now().strftime('%H:%M:%S')}  |")
        try:
            total = db.get_total_sell_today()
            self.lbl_total.setText(f"매도누적: {_fmt_won(total)} / 한도: {_fmt_won(config.MAX_SELL_TOTAL_DAILY)}")
        except Exception:
            pass
        self.btn_release.setEnabled(safety.kill_switch_engaged())

    # ------------------------------------------------------
    # 킬스위치
    # ------------------------------------------------------
    def _engage_kill(self):
        reply = QMessageBox.question(
            self, "전체 정지",
            "자동매매를 즉시 정지하시겠습니까?\n(미체결 주문은 별도 취소 필요)",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        safety.engage_kill_switch("manual GUI button")
        self.append_log("[KILL] 전체 정지 활성화")
        notify.notify_system("🛑 킬스위치 활성화 (사용자 액션)", color=notify.COLOR_RED)
        self._refresh_status()

    def _release_kill(self):
        reply = QMessageBox.question(self, "정지 해제", "정지를 해제하시겠습니까?", QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        safety.release_kill_switch()
        self.append_log("[KILL] 정지 해제")
        notify.notify_system("🟢 킬스위치 해제", color=notify.COLOR_GREEN)
        self._refresh_status()

    # ------------------------------------------------------
    # 이벤트 슬롯
    # ------------------------------------------------------
    @pyqtSlot(object)
    def on_event(self, ev: Event):
        ts = dt.datetime.now().strftime("%H:%M:%S")
        if ev.trigger_type == "NEW_BUY":
            self.tab_new_buy.upsert(ev.stock_code, [
                ts, ev.stock_name, ev.stock_code,
                _fmt_won(ev.pension_amount), f"{ev.current_price:,}",
            ], color=QColor("#27ae60"))
            self.append_log(f"[NEW_BUY] {ev.stock_name} ({ev.stock_code}) 연기금매수 {_fmt_won(ev.pension_amount)}")
        elif ev.trigger_type == "CONSECUTIVE_BUY":
            days = ev.extra.get("days", 0)
            self.tab_consecutive.upsert(ev.stock_code, [
                ts, ev.stock_name, ev.stock_code,
                f"{days}일", _fmt_won(ev.pension_amount),
            ], color=QColor("#2980b9"))
            self.append_log(f"[CONSEC] {ev.stock_name} ({ev.stock_code}) {days}일 연속 순매수")
        elif ev.trigger_type in ("HOLD_WARN", "AUTO_SELL"):
            kind = "20억경고" if ev.trigger_type == "HOLD_WARN" else "40억자동매도"
            color = QColor("#f39c12") if ev.trigger_type == "HOLD_WARN" else QColor("#c0392b")
            self.tab_hold.upsert(f"{ev.stock_code}_{ev.trigger_type}", [
                ts, ev.stock_name, ev.stock_code, kind,
                _fmt_won(ev.pension_amount), f"{ev.holding_qty:,}",
                f"{ev.current_price:,}",
            ], color=color)
            self.append_log(f"[{kind}] {ev.stock_name} ({ev.stock_code}) 연기금매도 {_fmt_won(ev.pension_amount)}")

    @pyqtSlot(str)
    def on_status(self, msg: str):
        self.append_log(f"[SYS] {msg}")

    @pyqtSlot(str, dict)
    def on_data_updated(self, code: str, snapshot: dict):
        """폴링/실시간 결과를 모니터링 + HOT 탭에 갱신."""
        ts = dt.datetime.now().strftime("%H:%M:%S")
        name = snapshot.get("name", code)
        buy = snapshot.get("buy", 0)
        sell = snapshot.get("sell", 0)
        net = snapshot.get("net", 0)
        cur = snapshot.get("current_price", 0)
        held = snapshot.get("holding_qty", 0)
        is_hot = snapshot.get("is_hot", False) or (code in self._hot_codes)
        held_str = f"{held:,}주" if held > 0 else "-"
        # 색상: 순매수 양수는 초록, 음수는 빨강
        color = QColor("#27ae60") if net > 0 else (QColor("#c0392b") if net < 0 else QColor("#7f8c8d"))
        cells = [
            ts, name, code,
            _fmt_won(buy), _fmt_won(sell), _fmt_won(net),
            f"{cur:,}", held_str,
        ]
        # 전체 모니터링 탭
        self.tab_monitor.upsert(code, cells, color=color)
        # HOT 탭 (활발 종목만)
        if is_hot:
            self.tab_hot.upsert(code, cells, color=color)
        elif code in self.tab_hot._index:
            # HOT에서 빠진 종목은 HOT 탭에서도 제거
            self._remove_from_hot_tab(code)
        # |순매수| 가 임계값 50% 넘는 종목은 로그에도 (트리거 임박 알림)
        if abs(net) >= config.THRESHOLD_NEW_BUY * 0.5:
            self.append_log(f"[활발] {name}({code}) 순매수 {_fmt_won(net)} (현재가 {cur:,})")

    @pyqtSlot(set)
    def on_hot_changed(self, hot_codes: set):
        """tracker의 HOT 집합이 바뀔 때 호출. 강등된 종목은 HOT 탭에서 제거."""
        removed = self._hot_codes - hot_codes
        self._hot_codes = set(hot_codes)
        for code in removed:
            self._remove_from_hot_tab(code)
        self.append_log(f"[HOT] 활발 종목 {len(self._hot_codes)}개")

    def _remove_from_hot_tab(self, code: str):
        idx = self.tab_hot._index.pop(code, None)
        if idx is None:
            return
        self.tab_hot.removeRow(idx)
        # 행 인덱스 재정렬
        for k, v in list(self.tab_hot._index.items()):
            if v > idx:
                self.tab_hot._index[k] = v - 1

    def append_log(self, msg: str):
        ts = dt.datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{ts}  {msg}")
