"""
종목 유니버스 로더.

connect 직후 키움 OCX에서 KOSPI/KOSDAQ 전 종목 코드를 받아와
watchlist를 자동 확장한다. 이렇게 해야 "수동 등록한 종목" 안에서만 평가하는
한계를 벗고 시장 전체에서 연기금이 매수/매도하는 종목을 자동 발견할 수 있다.

ETF/ETN/스팩/우선주/리츠 등은 기본 제외 (연기금 의미가 다름).
"""
from __future__ import annotations

import logging

import config

log = logging.getLogger(__name__)


# 종목명 키워드 블랙리스트 (대소문자 무관 부분 일치)
_ETF_NAME_KEYWORDS = (
    "KODEX", "TIGER", "KOSEF", "ARIRANG", "HANARO", "KBSTAR", "KINDEX",
    "SOL ", "SOL_", "ACE ", "ACE_", "TIMEFOLIO", "WOORI",
    "ETN", "ETF", "스팩", "SPAC", "리츠", "REITs", "REIT",
    "선물", "인버스", "레버리지", "TR)", "(H)", "콜)", "풋)",
)


def _looks_like_etf_or_special(name: str) -> bool:
    if not name:
        return False
    u = name.upper()
    for k in _ETF_NAME_KEYWORDS:
        if k.upper() in u:
            return True
    return False


def _is_preferred_stock(code: str, name: str) -> bool:
    """우선주 판별 (보통주 코드와 분리). 종목명 끝이 '우' 또는 '우B' 인 경우."""
    if not name:
        return False
    n = name.strip()
    return n.endswith("우") or n.endswith("우B") or n.endswith("우C") or n.endswith("(전환)")


def load_universe(kiwoom) -> list:
    """
    config.UNIVERSE_MODE 에 따라 종목 코드 리스트 반환.

    mode:
      - "watchlist"     : 빈 리스트 (기존 watchlist.txt + 보유종목만 사용)
      - "kospi"         : KOSPI 전 종목 (~900)
      - "kosdaq"        : KOSDAQ 전 종목 (~1500)
      - "kospi+kosdaq"  : 둘 다 (~2500, TR 한도 주의)
    """
    mode = (getattr(config, "UNIVERSE_MODE", "watchlist") or "watchlist").lower()
    if mode == "watchlist":
        return []

    raw: list = []
    if "kospi" in mode:
        raw.extend(kiwoom.get_code_list("0"))
    if "kosdaq" in mode:
        raw.extend(kiwoom.get_code_list("10"))

    # 중복 제거 + 6자리 숫자 코드만
    seen = set()
    cleaned = []
    for c in raw:
        c = (c or "").strip()
        if not c or not c.isdigit() or len(c) != 6:
            continue
        if c in seen:
            continue
        seen.add(c)
        cleaned.append(c)

    # ETF/우선주 등 제외
    exclude_etf = getattr(config, "UNIVERSE_EXCLUDE_ETF", True)
    if exclude_etf:
        filtered = []
        for c in cleaned:
            try:
                name = kiwoom.get_master_code_name(c)
            except Exception:
                name = ""
            if _looks_like_etf_or_special(name):
                continue
            if _is_preferred_stock(c, name):
                continue
            filtered.append(c)
        log.info("Universe %s: raw=%d filtered=%d (excluded ETF/우선주/스팩)",
                 mode, len(cleaned), len(filtered))
        return filtered

    log.info("Universe %s: %d codes", mode, len(cleaned))
    return cleaned
