"""
DART (전자공시) OpenAPI 클라이언트.

기능:
- corp_code 매핑 (종목코드 6자리 → DART corp_code 8자리)
- 종목별 최근 N일 공시 fetch
- 공시 종류별 sentiment 점수 매핑

환경변수:
    DART_API_KEY — https://opendart.fss.or.kr/uss/umt/EgovMberInsertView.do 발급

호출 한도: 일 10,000회 (충분히 여유)
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import os
import xml.etree.ElementTree as ET
import zipfile
from typing import Optional

import requests

log = logging.getLogger(__name__)

DART_BASE = "https://opendart.fss.or.kr/api"


# ==========================================================================
# 공시 종류별 점수 (한국 상장사 호재/악재 분류)
# ==========================================================================
# report_nm(공시 보고서명) 키워드 매칭. 키워드 발견 시 점수 가산.

DISCLOSURE_SCORES = [
    # 호재 (양수)
    ("흑자전환", 15),
    ("매출액또는손익구조30%", 10),     # 매출 30%이상 변동 (큰 변화)
    ("단일판매·공급계약", 8),
    ("단일판매공급계약", 8),
    ("주요사항보고서(자기주식취득", 8),
    ("주요사항보고서(자기주식취득)", 8),
    ("자기주식취득", 6),
    ("주식분할", 5),
    ("무상증자", 5),
    ("현금배당", 4),
    ("주식배당", 4),
    ("신규시설투자", 5),
    ("회사합병", 5),
    ("영업양수", 4),
    # 악재 (음수)
    ("적자전환", -15),
    ("적자지속", -10),
    ("감자", -12),
    ("주요사항보고서(감자", -12),
    ("회생절차", -25),
    ("파산", -30),
    ("상장폐지", -30),
    ("관리종목", -20),
    ("주요사항보고서(부실", -20),
    ("거래정지", -15),
    ("유상증자", -3),                # 희석 효과
    ("주요사항보고서(유상증자", -3),
    ("전환사채", -2),                # 희석 효과 약함
    ("교환사채", -2),
    ("신주인수권부사채", -2),
    ("영업양도", -4),
    ("회사분할", -3),
]


# ==========================================================================
# corp_code 매핑 (전체 회사 8자리 코드 ↔ 종목코드)
# ==========================================================================

_corp_map_cache: Optional[dict] = None    # stock_code(6자) → corp_code(8자)


def get_api_key() -> Optional[str]:
    return os.environ.get("DART_API_KEY", "").strip() or None


def load_corp_code_map(force: bool = False) -> dict:
    """전체 회사 corp_code 매핑 다운로드 (메모리 캐시, ~7MB ZIP)."""
    global _corp_map_cache
    if _corp_map_cache is not None and not force:
        return _corp_map_cache

    key = get_api_key()
    if not key:
        log.warning("DART_API_KEY 미설정 → DART 기능 skip")
        _corp_map_cache = {}
        return _corp_map_cache

    try:
        r = requests.get(
            f"{DART_BASE}/corpCode.xml",
            params={"crtfc_key": key},
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("DART corp_code 다운 실패: %s", e)
        _corp_map_cache = {}
        return _corp_map_cache

    try:
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            # ZIP 안에 CORPCODE.xml 하나
            with z.open(z.namelist()[0]) as f:
                tree = ET.parse(f)
        root = tree.getroot()
        result = {}
        for item in root.findall("list"):
            corp = (item.findtext("corp_code") or "").strip()
            stock = (item.findtext("stock_code") or "").strip()
            if corp and stock and stock != " ":
                result[stock] = corp
        log.info("DART corp_code 로드: %d 종목 매핑", len(result))
        _corp_map_cache = result
        return result
    except Exception as e:
        log.exception("DART corp_code 파싱 실패")
        _corp_map_cache = {}
        return _corp_map_cache


def fetch_disclosures(stock_code: str, days_back: int = 7) -> list:
    """
    종목별 최근 N일 공시 목록 fetch.
    return: list of dict {report_nm, rcept_dt, ...}
    """
    key = get_api_key()
    if not key:
        return []

    corp_map = load_corp_code_map()
    corp_code = corp_map.get(stock_code)
    if not corp_code:
        return []

    today = dt.date.today()
    bgn = (today - dt.timedelta(days=days_back)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    try:
        r = requests.get(
            f"{DART_BASE}/list.json",
            params={
                "crtfc_key": key,
                "corp_code": corp_code,
                "bgn_de": bgn,
                "end_de": end,
                "page_count": 50,
            },
            timeout=10,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        log.warning("DART %s fetch 실패: %s", stock_code, e)
        return []

    if payload.get("status") not in ("000", "013"):  # 013 = 데이터 없음
        log.warning("DART %s status=%s msg=%s",
                    stock_code, payload.get("status"), payload.get("message"))
        return []

    return payload.get("list") or []


def compute_dart_score(disclosures: list) -> tuple:
    """
    공시 목록 → (점수, 매칭된 키워드들).
    return: (score, [matched_keyword, ...])
    """
    score = 0
    matched = []
    for d in disclosures:
        name = (d.get("report_nm") or "").replace(" ", "")
        for keyword, kw_score in DISCLOSURE_SCORES:
            if keyword.replace(" ", "") in name:
                score += kw_score
                matched.append(f"{keyword}({kw_score:+d})")
                break  # 같은 공시에 여러 키워드 매칭 방지
    return score, matched


def fetch_dart_scores(stock_codes: list, days_back: int = 7) -> dict:
    """
    여러 종목의 DART 점수 일괄 fetch.
    return: {stock_code: {"score": int, "matched": [...], "count": int}}
    """
    if not get_api_key():
        return {}

    # 첫 호출이면 corp_code 다운로드 (~5초)
    load_corp_code_map()

    result = {}
    for code in stock_codes:
        if not code:
            continue
        disclosures = fetch_disclosures(code, days_back=days_back)
        score, matched = compute_dart_score(disclosures)
        result[code] = {
            "score": score,
            "matched": matched,
            "count": len(disclosures),
        }
    log.info("DART scores: %d 종목 (점수≠0: %d개)",
             len(result), sum(1 for v in result.values() if v["score"] != 0))
    return result


# ==========================================================================
# 대량보유상황보고서 (5% 룰) — /api/majorstock.json
# ==========================================================================
NPS_REPORTERS = ("국민연금공단", "국민연금기금", "국민연금")


def _to_int_safe(v) -> int:
    if v is None:
        return 0
    try:
        return int(str(v).replace(",", "").replace(" ", "") or 0)
    except (ValueError, TypeError):
        return 0


def _to_float_safe(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(str(v).replace(",", "").replace(" ", "") or 0)
    except (ValueError, TypeError):
        return 0.0


def fetch_major_holdings(stock_code: str) -> list:
    """종목의 대량보유상황보고서 (5%룰) 전체 이력."""
    key = get_api_key()
    if not key:
        return []
    corp_map = load_corp_code_map()
    corp_code = corp_map.get(stock_code)
    if not corp_code:
        return []
    try:
        r = requests.get(
            f"{DART_BASE}/majorstock.json",
            params={"crtfc_key": key, "corp_code": corp_code},
            timeout=15,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        log.warning("DART majorstock %s fetch 실패: %s", stock_code, e)
        return []
    if payload.get("status") not in ("000", "013"):
        log.warning("DART majorstock %s status=%s", stock_code, payload.get("status"))
        return []
    raw = payload.get("list") or []
    result = []
    for d in raw:
        result.append({
            "stock_code": stock_code,
            "corp_code": corp_code,
            "rcept_no": d.get("rcept_no", ""),
            "rcept_dt": d.get("rcept_dt", ""),
            "corp_name": d.get("corp_name", ""),
            "report_tp": d.get("report_tp", ""),
            "repror": (d.get("repror") or "").strip(),
            "stkqy": _to_int_safe(d.get("stkqy")),
            "stkqy_irds": _to_int_safe(d.get("stkqy_irds")),
            "stkrt": _to_float_safe(d.get("stkrt")),
            "stkrt_irds": _to_float_safe(d.get("stkrt_irds")),
            "report_resn": d.get("report_resn", ""),
        })
    return result


def fetch_major_holdings_bulk(
    stock_codes: list,
    cache_path: Optional[str] = None,
    cache_ttl_hours: int = 12,
) -> dict:
    """여러 종목 대량보유공시 일괄 fetch + 디스크 캐시."""
    if not get_api_key():
        return {}
    cache_data: dict = {"fetched_at": "", "data": {}}
    if cache_path and os.path.exists(cache_path):
        try:
            import json as _json
            with open(cache_path, "r", encoding="utf-8") as f:
                cache_data = _json.load(f)
        except Exception:
            cache_data = {"fetched_at": "", "data": {}}
    fresh = False
    if cache_data.get("fetched_at"):
        try:
            fetched = dt.datetime.fromisoformat(cache_data["fetched_at"])
            if (dt.datetime.now() - fetched).total_seconds() < cache_ttl_hours * 3600:
                fresh = True
        except Exception:
            pass
    if fresh and cache_data.get("data"):
        log.info("DART majorstock 캐시 사용 (TTL %dh 이내, %d 종목)",
                 cache_ttl_hours, len(cache_data["data"]))
        return cache_data["data"]
    load_corp_code_map()
    result: dict = {}
    hit = 0
    for code in stock_codes:
        if not code:
            continue
        items = fetch_major_holdings(code)
        if items:
            result[code] = items
            hit += 1
    log.info("DART majorstock fetch 완료: %d/%d 종목 hit", hit, len(stock_codes))
    if cache_path:
        try:
            import json as _json
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                _json.dump(
                    {"fetched_at": dt.datetime.now().isoformat(), "data": result},
                    f, ensure_ascii=False, indent=2,
                )
        except Exception as e:
            log.warning("DART majorstock 캐시 저장 실패: %s", e)
    return result


def filter_nps_holdings(majorstock_data: dict) -> dict:
    """fetch_major_holdings_bulk 결과 → 보고자=국민연금공단 인 공시만."""
    nps = {}
    for code, items in majorstock_data.items():
        nps_items = [
            d for d in items
            if any(r in (d.get("repror") or "") for r in NPS_REPORTERS)
        ]
        if nps_items:
            nps_items.sort(key=lambda x: x.get("rcept_dt", ""), reverse=True)
            nps[code] = nps_items
    return nps


def latest_nps_holding(nps_data: dict) -> dict:
    """각 종목별 국민연금 최신 보유 1건 추출."""
    out = {}
    for code, items in nps_data.items():
        if not items:
            continue
        latest = items[0]
        out[code] = {
            "qty": latest["stkqy"],
            "rate_pct": latest["stkrt"],
            "rcept_dt": latest["rcept_dt"],
            "report_tp": latest["report_tp"],
            "qty_irds": latest["stkqy_irds"],
            "rate_irds": latest["stkrt_irds"],
        }
    return out
