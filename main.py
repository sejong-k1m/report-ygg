"""
연기금 자동매매 봇 - 진입점.

실행 흐름:
1. 로깅 + DB 초기화
2. PyQt5 QApplication 부팅
3. 키움 OpenAPI+ 로그인 (영웅문 로그인 창 뜸)
4. 보유종목 조회 → 워치리스트 등록
5. PensionTracker / OrderManager 시작
6. GUI 띄우고 이벤트 루프 진입

사용법:
    # 1. app_secrets.py.example 을 app_secrets.py 로 복사 후 값 채우기
    # 2. 32-bit Python 환경에서 실행
    py -3.9-32 main.py
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path


# ============================================================
# Windows 한글/특수문자 경로에서 PyQt5 플러그인 로드 실패 회피
# (E:\1.총무\★개인\... 같은 경로 → 8.3 단축경로로 변환 후 QT_PLUGIN_PATH 설정)
# ============================================================
def _fix_qt_plugin_path():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        import PyQt5
        plugin_path = os.path.join(os.path.dirname(PyQt5.__file__), "Qt5", "plugins")
        if not os.path.exists(plugin_path):
            # 일부 버전은 Qt5 폴더 없이 바로 plugins
            plugin_path = os.path.join(os.path.dirname(PyQt5.__file__), "plugins")
        # 8.3 단축경로로 변환
        GetShortPathName = ctypes.windll.kernel32.GetShortPathNameW
        GetShortPathName.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        GetShortPathName.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(520)
        n = GetShortPathName(plugin_path, buf, 520)
        short = buf.value if n else plugin_path
        os.environ["QT_PLUGIN_PATH"] = short
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = os.path.join(short, "platforms")
    except Exception:
        pass


_fix_qt_plugin_path()


import config


def _setup_logging():
    Path(config.LOG_DIR).mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.TimedRotatingFileHandler(
            os.path.join(config.LOG_DIR, "bot.log"),
            when="midnight", backupCount=30, encoding="utf-8",
        ),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def main():
    _setup_logging()
    log = logging.getLogger("main")

    # secrets 검증
    try:
        import app_secrets
    except ImportError:
        log.error("app_secrets.py 가 없습니다. app_secrets.py.example 을 복사해서 만들고 값을 채워주세요.")
        sys.exit(1)

    if not app_secrets.KIWOOM_ACCOUNT or app_secrets.KIWOOM_ACCOUNT == "0000000000":
        log.error("app_secrets.py 에 KIWOOM_ACCOUNT 가 설정되지 않았습니다.")
        sys.exit(1)

    # DB 초기화
    from storage import db
    db.init()

    # PyQt5
    from PyQt5.QtWidgets import QApplication
    app = QApplication(sys.argv)

    # GUI 먼저 띄우기 (로그인 진행 중에도 보이게)
    from gui.main_window import MainWindow
    win = MainWindow()
    win.show()
    win.append_log("부팅 시작")

    # 키움 로그인
    from core.kiwoom_client import KiwoomClient
    kiwoom = KiwoomClient()
    win.append_log("키움 로그인 창에서 로그인 해주세요...")
    ok = kiwoom.connect_block(timeout_ms=300000)  # 5분 대기
    if not ok:
        win.append_log("[ERROR] 키움 로그인 실패")
        log.error("Kiwoom login failed")
        # 로그인 실패해도 GUI는 띄워둔 채 사용자 확인 가능
    else:
        win.append_log(f"키움 로그인 성공: {kiwoom.get_user_id()}")

    # 계좌 검증 + 자동 보정
    accounts = kiwoom.get_account_list()
    win.append_log(f"키움 계좌목록: {accounts}")
    log.info("Kiwoom accounts: %s", accounts)
    account_to_use = app_secrets.KIWOOM_ACCOUNT
    if account_to_use not in accounts:
        # config 의 계좌번호가 키움 실제 형식과 다르면 첫 번째 계좌로 자동 대체
        if accounts:
            # 가장 비슷한 계좌 찾기 (앞자리 매칭) → 없으면 첫 번째
            matched = next((a for a in accounts if a.startswith(account_to_use[:6])), None)
            account_to_use = matched or accounts[0]
            win.append_log(f"[INFO] 설정값({app_secrets.KIWOOM_ACCOUNT})이 키움 계좌목록에 없어 자동 선택: {account_to_use}")
            win.append_log(f"[INFO] app_secrets.py 의 KIWOOM_ACCOUNT 를 '{account_to_use}' 로 업데이트하세요")
        else:
            win.append_log("[ERROR] 키움 계좌목록이 비어있음. 모의투자 신청 또는 영웅문 계좌 확인 필요")
            log.error("No accounts returned from Kiwoom")

    # 트래커 + 오더매니저
    from core.pension_tracker import PensionTracker
    from core.order_manager import OrderManager

    tracker = PensionTracker(kiwoom, account_to_use)
    order_mgr = OrderManager(kiwoom, account_to_use)

    # 시그널 연결
    tracker.sig_event.connect(win.on_event)
    tracker.sig_event.connect(order_mgr.on_event)
    tracker.sig_status.connect(win.on_status)
    tracker.sig_data_updated.connect(win.on_data_updated)
    tracker.sig_hot_changed.connect(win.on_hot_changed)
    # 매수 체결 시 보유종목 즉시 새로고침
    order_mgr.sig_holdings_dirty.connect(tracker.request_holdings_refresh)

    # 카카오 토큰 점검 (없거나 만료여도 진행, 카카오만 비활성)
    import notify
    notify.init_kakao()

    if ok:
        tracker.start()
        order_mgr.start()
        win.append_log("연기금 트래커 + 오더매니저 시작")
        if config.KIWOOM_MOCK_TRADING:
            mode_msg = "🧪 연기금 봇 시작 (모의투자 모드)"
        elif config.DRY_RUN:
            mode_msg = "🧪 연기금 봇 시작 (DRY_RUN: 실데이터 + 주문 시뮬)"
        else:
            mode_msg = "🟢 연기금 봇 시작 (실계좌 LIVE)"
        notify.notify_system(mode_msg)
        win.append_log(mode_msg)

    # 종료 시 정리
    def _shutdown():
        tracker.stop()
        order_mgr.stop()
        log.info("shutdown")

    app.aboutToQuit.connect(_shutdown)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
