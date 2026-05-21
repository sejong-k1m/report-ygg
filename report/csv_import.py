"""
KRX 수동 다운로드 CSV 임포터.

사용자가 data.krx.co.kr 에서 직접 받은 CSV를 report/input/ 폴더에 떨궈주면
이 모듈이 파싱해서 표준 dict 리스트로 반환.

KRX CSV 특징:
- 인코딩: EUC-KR (한국 Windows 환경에서 받으면)
- 또는 UTF-8 with BOM (브라우저 따라)
- 첫 줄 한글 헤더 (예: "종목코드,종목명,매도거래량,...")
- 천 단위 콤마 포함 숫자 (CSV 안에 있어서 따옴표로 감쌈)
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# 컬럼명 후보 (KRX가 다른 페이지에선 다른 이름 줄 수 있음)
_COL_CODE = ("종목코드", "ISU_SRT_CD", "단축코드")
_COL_NAME = ("종목명", "ISU_NM", "ISU_ABBRV", "한글 종목명")
_COL_BUY_AMT = ("매수거래대금", "거래대금_매수", "매수금액", "BID_TRDVAL", "매수대금")
_COL_SELL_AMT = ("매도거래대금", "거래대금_매도", "매도금액", "ASK_TRDVAL", "매도대금")
_COL_NET_AMT = ("순매수거래대금", "거래대금_순매수", "순매수대금", "NETBID_TRDVAL")
_COL_BUY_QTY = ("매수거래량", "거래량_매수", "매수수량", "BID_TRDVOL")
_COL_SELL_QTY = ("매도거래량", "거래량_매도", "매도수량", "ASK_TRDVOL")
_COL_NET_QTY = ("순매수거래량", "거래량_순매수", "순매수수량", "NETBID_TRDVOL")
_COL_MKTCAP = ("시가총액", "MKTCAP")


def _to_int(s) -> int:
    if s is None:
        return 0
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).replace(",", "").replace('"', "").strip()
    if not s or s == "-":
        return 0
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _pick(row: dict, candidates: tuple) -> Optional[str]:
    """row dict 에서 후보 컬럼명 중 첫 매칭 반환."""
    for c in candidates:
        if c in row and row[c] not in (None, ""):
            return row[c]
        # 공백/특수문자 차이 흡수
        for k in row.keys():
            if k.strip().replace(" ", "") == c.strip().replace(" ", ""):
                return row[k]
    return None


def _open_csv(path: Path):
    """EUC-KR 우선, 실패 시 UTF-8 BOM, 그래도 안 되면 UTF-8 일반."""
    for enc in ("euc-kr", "cp949", "utf-8-sig", "utf-8"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                content = f.read()
            return content, enc
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Cannot decode {path} — encoding 미상")


def parse_krx_csv(path: Path) -> list:
    """
    KRX CSV 1개 파싱.

    파일명 규칙 권장: krx_YYYYMMDD_<MARKET>.csv
    예: krx_20241230_KOSPI.csv

    return: [{stock_code, stock_name, buy_amount, sell_amount, net_amount,
              buy_qty, sell_qty, net_qty}]
    """
    if not path.exists():
        return []
    content, enc = _open_csv(path)
    log.info("CSV %s opened with encoding=%s", path.name, enc)

    # CSV 본문에 메타 헤더(다운로드 시점 등)가 앞에 붙는 경우 있어
    # 실제 헤더 라인을 자동 탐지 (첫 컬럼이 '종목코드'나 'ISU_*'인 라인)
    lines = content.splitlines()
    header_idx = 0
    for i, line in enumerate(lines):
        if any(c in line for c in ("종목코드", "ISU_SRT_CD", "단축코드")):
            header_idx = i
            break

    cleaned = "\n".join(lines[header_idx:])
    reader = csv.DictReader(cleaned.splitlines())
    rows = []
    for raw in reader:
        # BOM 잔재 제거
        row = {(k or "").lstrip("﻿").strip(): (v or "").strip() for k, v in raw.items()}
        code_raw = _pick(row, _COL_CODE) or ""
        # 키움/KRX는 'A005930' 형태로 종목코드 줄 수 있음
        code = re.sub(r"^[A-Za-z]+", "", str(code_raw)).strip()
        if not code or not code.isdigit():
            continue
        name = (_pick(row, _COL_NAME) or "").strip()
        rec = {
            "stock_code": code,
            "stock_name": name,
            "buy_amount":  _to_int(_pick(row, _COL_BUY_AMT)),
            "sell_amount": _to_int(_pick(row, _COL_SELL_AMT)),
            "net_amount":  _to_int(_pick(row, _COL_NET_AMT)),
            "buy_qty":  _to_int(_pick(row, _COL_BUY_QTY)),
            "sell_qty": _to_int(_pick(row, _COL_SELL_QTY)),
            "net_qty":  _to_int(_pick(row, _COL_NET_QTY)),
            "market_cap": _to_int(_pick(row, _COL_MKTCAP)),
        }
        # 순매수액이 비어있고 buy/sell이 있으면 계산
        if rec["net_amount"] == 0 and (rec["buy_amount"] or rec["sell_amount"]):
            rec["net_amount"] = rec["buy_amount"] - rec["sell_amount"]
        rows.append(rec)
    log.info("CSV %s parsed: %d rows", path.name, len(rows))
    return rows


def find_csvs_for_date(trade_date: str, input_dir: Path) -> dict:
    """
    날짜로 input 폴더 탐색 → market별 CSV 경로 dict.

    파일명 매칭 (대소문자 무관):
      *YYYYMMDD*KOSPI*.csv      → KOSPI
      *YYYYMMDD*KOSDAQ*.csv     → KOSDAQ
      *YYYYMMDD*전체*.csv 또는 *ALL*.csv → ALL
    """
    if not input_dir.exists():
        return {}
    result = {}
    for p in input_dir.glob("*.csv"):
        name = p.name.upper()
        if trade_date not in name and trade_date.replace("-", "") not in name:
            continue
        if "KOSPI" in name or "STK" in name:
            result.setdefault("KOSPI", p)
        elif "KOSDAQ" in name or "KSQ" in name:
            result.setdefault("KOSDAQ", p)
        elif "KONEX" in name or "KNX" in name:
            result.setdefault("KONEX", p)
        elif "ALL" in name or "전체" in p.name:
            result.setdefault("ALL", p)
    return result
