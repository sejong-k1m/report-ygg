"""
키움증권 OpenAPI+ COM 래퍼.

⚠️ 32-bit Python + Windows + 영웅문 OpenAPI+ 설치 필수.
   QAxWidget("KHOPENAPI.KHOpenAPICtrl.1") 가 32bit COM 컴포넌트라 64bit Python에서 실패함.

설계:
- PyQt5 메인 스레드에서만 OCX 호출 (COM 스레드 안전성 이유)
- TR 조회는 QEventLoop으로 동기화 (응답까지 블로킹) → 코드 단순
- TR 호출 사이 250ms sleep (1초 5회 제한 여유)
- 실시간 데이터 / 체결 콜백은 Qt 시그널로 외부에 노출

주요 TR:
- OPW00018 : 계좌평가잔고내역 (보유종목)
- OPT10059 : 종목별투자자기관별 (당일 연기금 매수/매도 잠정치)
- OPT10001 : 주식기본정보 (현재가)
- OPT10009 : 시간대별 투자자별 매매 상위 (※ 실제 TR ID는 KOA Studio로 검증 필요)

체결콜백 (OnReceiveChejanData):
- gubun '0' = 주문체결, '1' = 잔고변경
- 9203 = 주문번호, 913 = 주문상태, 902 = 미체결수량, 911 = 체결수량
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QObject, QTimer, pyqtSignal

import config

log = logging.getLogger(__name__)


# 체결콜백 FID
FID_ORDER_NO = "9203"
FID_ORDER_STATUS = "913"        # '접수', '체결', '확인' 등
FID_UNFILLED_QTY = "902"
FID_FILLED_QTY = "911"
FID_FILLED_PRICE = "910"
FID_STOCK_CODE = "9001"


class KiwoomClient(QObject):
    # 외부로 노출되는 시그널
    sig_connected = pyqtSignal(int)                        # err_code (0=성공)
    sig_real_data = pyqtSignal(str, str, dict)             # (code, real_type, fid_dict)
    sig_chejan = pyqtSignal(str, dict)                     # (gubun, fid_dict)
    sig_order_msg = pyqtSignal(str)                        # 서버 메시지

    def __init__(self):
        super().__init__()
        self.ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._loop: Optional[QEventLoop] = None
        self._last_tr: dict = {}
        self._last_tr_code: str = ""
        self._last_rq_name: str = ""
        self._real_screen_no = "9999"
        self._connected = False
        self._connect_loop: Optional[QEventLoop] = None

        # OCX 이벤트 연결
        self.ocx.OnEventConnect.connect(self._on_event_connect)
        self.ocx.OnReceiveTrData.connect(self._on_receive_tr_data)
        self.ocx.OnReceiveRealData.connect(self._on_receive_real_data)
        self.ocx.OnReceiveChejanData.connect(self._on_receive_chejan_data)
        self.ocx.OnReceiveMsg.connect(self._on_receive_msg)

    # --------------------------------------------------------
    # 로그인
    # --------------------------------------------------------
    def connect_block(self, timeout_ms: int = 60000) -> bool:
        """동기식 로그인. 사용자가 영웅문 로그인 창에서 로그인할 때까지 대기."""
        if self._connected:
            return True
        ret = self.ocx.dynamicCall("CommConnect()")
        if ret != 0:
            log.error("CommConnect() returned %s", ret)
            return False
        self._connect_loop = QEventLoop()
        QTimer.singleShot(timeout_ms, self._connect_loop.quit)
        self._connect_loop.exec_()
        return self._connected

    def _on_event_connect(self, err_code):
        self._connected = (err_code == 0)
        log.info("OnEventConnect err=%s connected=%s", err_code, self._connected)
        self.sig_connected.emit(err_code)
        if self._connect_loop and self._connect_loop.isRunning():
            self._connect_loop.quit()

    def is_connected(self) -> bool:
        try:
            return self.ocx.dynamicCall("GetConnectState()") == 1
        except Exception:
            return False

    def get_account_list(self) -> list:
        s = self.ocx.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        return [a for a in s.split(";") if a]

    def get_user_id(self) -> str:
        return self.ocx.dynamicCall("GetLoginInfo(QString)", "USER_ID")

    def get_master_code_name(self, code: str) -> str:
        return self.ocx.dynamicCall("GetMasterCodeName(QString)", code) or ""

    def get_code_list(self, market: str) -> list:
        """
        시장별 전체 종목코드 리스트 (오프라인 캐시, TR 한도 안 씀).
        market: "0"=KOSPI, "10"=KOSDAQ, "3"=ELW, "4"=뮤추얼펀드, "8"=ETF, "50"=KONEX
        """
        s = self.ocx.dynamicCall("GetCodeListByMarket(QString)", market) or ""
        return [c for c in s.split(";") if c]

    def get_master_stock_state(self, code: str) -> str:
        """관리/투자유의/거래정지 등 종목 상태 문자열."""
        return self.ocx.dynamicCall("GetMasterStockState(QString)", code) or ""

    # --------------------------------------------------------
    # TR 동기 호출
    # --------------------------------------------------------
    def request_tr(self, rq_name: str, tr_code: str, inputs: dict, screen: str = "0001", prev_next: int = 0, timeout_ms: int = 10000) -> dict:
        """TR 호출 후 응답까지 블로킹. 응답 dict 반환 (없으면 빈 dict)."""
        for k, v in inputs.items():
            self.ocx.dynamicCall("SetInputValue(QString, QString)", k, str(v))
        self._last_tr = {}
        self._last_tr_code = tr_code
        self._last_rq_name = rq_name
        self._loop = QEventLoop()
        QTimer.singleShot(timeout_ms, self._loop.quit)
        ret = self.ocx.dynamicCall("CommRqData(QString, QString, int, QString)", rq_name, tr_code, prev_next, screen)
        if ret != 0:
            log.error("CommRqData failed: tr=%s rq=%s ret=%s", tr_code, rq_name, ret)
            return {}
        self._loop.exec_()
        # 1초 5건 제한 회피 (안전 마진)
        time.sleep(0.35)
        return self._last_tr

    def _on_receive_tr_data(self, screen, rq_name, tr_code, record_name, prev_next, *_):
        try:
            if tr_code == "OPW00018":
                self._last_tr = self._parse_opw00018(rq_name, tr_code)
            elif tr_code == "OPW00001":
                self._last_tr = self._parse_opw00001(rq_name, tr_code)
            elif tr_code == "OPT10059":
                self._last_tr = self._parse_opt10059(rq_name, tr_code)
            elif tr_code == "OPT10001":
                self._last_tr = self._parse_opt10001(rq_name, tr_code)
            else:
                self._last_tr = {"raw_tr": tr_code, "raw_rq": rq_name}
        except Exception:
            log.exception("TR parse failed: %s/%s", tr_code, rq_name)
            self._last_tr = {}
        finally:
            if self._loop and self._loop.isRunning():
                self._loop.quit()

    def _get(self, tr_code: str, rq_name: str, idx: int, field: str) -> str:
        v = self.ocx.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            tr_code, rq_name, idx, field,
        )
        return (v or "").strip()

    def _repeat_cnt(self, tr_code: str, rq_name: str) -> int:
        return self.ocx.dynamicCall("GetRepeatCnt(QString, QString)", tr_code, rq_name) or 0

    @staticmethod
    def _to_int(s: str) -> int:
        s = (s or "").replace(",", "").replace("+", "").replace(" ", "")
        if not s or s in ("-",):
            return 0
        try:
            return int(s)
        except ValueError:
            try:
                return int(float(s))
            except ValueError:
                return 0

    # --------------------------------------------------------
    # TR 파서
    # --------------------------------------------------------
    def _parse_opw00018(self, rq, tr) -> dict:
        """계좌평가잔고내역 → 보유종목 list"""
        cnt = self._repeat_cnt(tr, rq)
        holdings = []
        for i in range(cnt):
            code = self._get(tr, rq, i, "종목번호").lstrip("A")
            name = self._get(tr, rq, i, "종목명")
            qty = abs(self._to_int(self._get(tr, rq, i, "보유수량")))
            avg = abs(self._to_int(self._get(tr, rq, i, "매입가")))
            cur = abs(self._to_int(self._get(tr, rq, i, "현재가")))
            if qty <= 0:
                continue
            holdings.append({
                "stock_code": code, "stock_name": name,
                "qty": qty, "avg_price": avg, "current_price": cur,
            })
        return {"holdings": holdings}

    def _parse_opt10059(self, rq, tr) -> dict:
        """
        종목별투자자기관별 → 당일/최근 일자별 투자자별 매매 (잠정치 포함).
        키움 OPT10059 컬럼명은 KOA Studio에서 정확히 확인 필요.
        대표적으로 "연기금등" 컬럼이 있음.
        """
        cnt = self._repeat_cnt(tr, rq)
        # 첫 행(=오늘)만 추출
        result = {"days": []}
        for i in range(cnt):
            day = {
                "date": self._get(tr, rq, i, "일자"),
                "close": self._to_int(self._get(tr, rq, i, "현재가")),
                "pension_net": self._to_int(self._get(tr, rq, i, "연기금등")),
                "foreign_net": self._to_int(self._get(tr, rq, i, "외국인투자자")),
                "institution_net": self._to_int(self._get(tr, rq, i, "기관계")),
            }
            result["days"].append(day)
        return result

    def _parse_opt10001(self, rq, tr) -> dict:
        """주식기본정보"""
        return {
            "stock_code": self._get(tr, rq, 0, "종목코드"),
            "stock_name": self._get(tr, rq, 0, "종목명"),
            "current_price": abs(self._to_int(self._get(tr, rq, 0, "현재가"))),
            "prev_close": abs(self._to_int(self._get(tr, rq, 0, "기준가"))),
            "open_price": abs(self._to_int(self._get(tr, rq, 0, "시가"))),
            "high_price": abs(self._to_int(self._get(tr, rq, 0, "고가"))),
            "low_price": abs(self._to_int(self._get(tr, rq, 0, "저가"))),
        }

    def _parse_opw00001(self, rq, tr) -> dict:
        """예수금상세현황. d+2 추정 예수금."""
        return {
            "cash_available": abs(self._to_int(self._get(tr, rq, 0, "주문가능금액"))),
            "deposit": abs(self._to_int(self._get(tr, rq, 0, "예수금"))),
            "d2_estimate": abs(self._to_int(self._get(tr, rq, 0, "d+2추정예수금"))),
        }

    # --------------------------------------------------------
    # 고수준 API
    # --------------------------------------------------------
    def fetch_holdings(self, account: str, password: str = "") -> list:
        res = self.request_tr("opw00018_req", "OPW00018", {
            "계좌번호": account,
            "비밀번호": password,
            "비밀번호입력매체구분": "00",
            "조회구분": "2",
        })
        return res.get("holdings", [])

    def fetch_pension_today(self, stock_code: str) -> dict:
        """
        당일 연기금 순매수 잠정치 (TR 1번 호출 - 한도 회피).
        순매수 양수 → 매수 우세로 간주 (pension_buy = net, sell = 0)
        순매수 음수 → 매도 우세로 간주 (pension_sell = |net|, buy = 0)

        반환: {"date", "close", "pension_buy", "pension_sell", "pension_net"}
        """
        res = self.request_tr("opt10059_req", "OPT10059", {
            "일자": "",
            "종목코드": stock_code,
            "금액수량구분": "1",       # 1=금액(원)
            "매매구분": "0",           # 0=순매수
            "단위구분": "1",          # 1=단주
        })
        days = res.get("days") or []
        if not days:
            return {}
        row = days[0]
        net = int(row.get("pension_net", 0))
        return {
            "date": row.get("date", ""),
            "close": int(row.get("close", 0)),
            "pension_buy": max(net, 0),
            "pension_sell": max(-net, 0),
            "pension_net": net,
        }

    def fetch_current_price(self, stock_code: str) -> int:
        res = self.request_tr("opt10001_req", "OPT10001", {"종목코드": stock_code})
        return res.get("current_price", 0)

    def fetch_basic_info(self, stock_code: str) -> dict:
        """OPT10001 결과 전체 (current_price, open_price, prev_close 등)"""
        return self.request_tr("opt10001_req", "OPT10001", {"종목코드": stock_code})

    # --------------------------------------------------------
    # 주문
    # --------------------------------------------------------
    def send_order_sell_limit(self, account: str, stock_code: str, qty: int, price: int, rq_name: str = "sell_limit") -> int:
        """
        지정가 매도. 반환: 0=성공, 그 외=실패.
        주문번호는 OnReceiveChejanData 콜백으로 비동기 도착.
        """
        # SendOrder(rqname, screen, accno, ordertype, code, qty, price, hogagb, orgordno)
        # ordertype: 1=신규매수, 2=신규매도, 3=매수취소, 4=매도취소, 5=매수정정, 6=매도정정
        # hogagb: "00"=지정가, "03"=시장가, "05"=조건부지정가 등
        ret = self.ocx.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [rq_name, "0010", account, 2, stock_code, qty, price, "00", ""],
        )
        log.info("SendOrder SELL %s qty=%s price=%s ret=%s", stock_code, qty, price, ret)
        return ret

    def send_order_buy_limit(self, account: str, stock_code: str, qty: int, price: int, rq_name: str = "buy_limit") -> int:
        """지정가 매수."""
        ret = self.ocx.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [rq_name, "0010", account, 1, stock_code, qty, price, "00", ""],
        )
        log.info("SendOrder BUY %s qty=%s price=%s ret=%s", stock_code, qty, price, ret)
        return ret

    def fetch_cash(self, account: str, password: str = "") -> int:
        """예수금 조회 (OPW00001 - 예수금상세현황요청). 100% 사용가능 예수금(d+2 추정금액) 반환."""
        res = self.request_tr("opw00001_req", "OPW00001", {
            "계좌번호": account,
            "비밀번호": password,
            "비밀번호입력매체구분": "00",
            "조회구분": "1",
        })
        return int(res.get("cash_available", 0))

    def cancel_order(self, account: str, stock_code: str, qty: int, orig_order_no: str, rq_name: str = "cancel") -> int:
        ret = self.ocx.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [rq_name, "0011", account, 4, stock_code, qty, 0, "00", orig_order_no],
        )
        log.info("CancelOrder %s qty=%s orig=%s ret=%s", stock_code, qty, orig_order_no, ret)
        return ret

    # --------------------------------------------------------
    # 실시간 데이터
    # --------------------------------------------------------
    def subscribe_real(self, codes: list, fids: list, screen: str = "9999", append: bool = True):
        """
        실시간 시세 등록. fids 예: '10' (현재가), '13' (누적거래량), '15' (거래량)
        한 화면당 100종목 한도.
        """
        codes_str = ";".join(codes)
        fids_str = ";".join(str(f) for f in fids)
        type_str = "1" if append else "0"
        self.ocx.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            screen, codes_str, fids_str, type_str,
        )

    def unsubscribe_real(self, codes: list, screen: str = "9999"):
        for c in codes:
            self.ocx.dynamicCall("SetRealRemove(QString, QString)", screen, c)

    def _on_receive_real_data(self, code, real_type, real_data):
        # 실시간 콜백은 매 변동마다 호출 → 데이터 가져오는 건 GetCommRealData
        # 외부 구독자가 필요한 FID만 GetCommRealData로 직접 조회하는 게 효율적이라
        # 여기선 시그널만 raw로 전달
        try:
            self.sig_real_data.emit(code, real_type, {"raw": real_data})
        except Exception:
            log.exception("on_real emit failed")

    def get_real_data(self, code: str, fid: str) -> str:
        return (self.ocx.dynamicCall("GetCommRealData(QString, int)", code, int(fid)) or "").strip()

    # --------------------------------------------------------
    # 체결 / 잔고 변동 콜백
    # --------------------------------------------------------
    def _on_receive_chejan_data(self, gubun, item_cnt, fid_list):
        try:
            fids = {}
            for fid_str in fid_list.split(";"):
                fid_str = fid_str.strip()
                if not fid_str:
                    continue
                v = self.ocx.dynamicCall("GetChejanData(int)", int(fid_str))
                fids[fid_str] = (v or "").strip()
            self.sig_chejan.emit(gubun, fids)
        except Exception:
            log.exception("on_chejan failed")

    def _on_receive_msg(self, screen, rq_name, tr_code, msg):
        log.info("kiwoom msg: %s [%s/%s] %s", screen, rq_name, tr_code, msg)
        self.sig_order_msg.emit(msg or "")
