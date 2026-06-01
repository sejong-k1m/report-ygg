"""
외부 데이터 소스 fetcher — KRX 직접 접근이 anti-bot 으로 막힌 우회 경로.

소스:
1. todayygg.com  — 연기금 매매 일일 리포트. JSON/CSV 공개 (의도된 다운로드).
2. judal.co.kr   — 종목별 가치지표 (PBR/PER/52주변동률 등). HTML 스크래핑.

이용 약관 메모:
- todayygg: "투자권유 아님 + KRX 공개 데이터 가공물" 명시. JSON/CSV 다운로드는 명시적 제공.
- judal: 공개 HTML. 일일 1회 가벼운 스크래핑.

회사 PC에서 todayygg.com / judal.co.kr 두 URL이 안 뚫리면 이 모듈도 동작 안 함.
그 경우 CSV 수동 다운로드 워크플로로 폴백.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import requests

log = logging.getLogger(__name__)

TODAYYGG_JSON_URL = "https://todayygg.com/summary_latest.json"
TODAYYGG_CSV_URL = "https://todayygg.com/analysis_latest.csv"

JUDAL_BUY_URL = "https://www.judal.co.kr/?view=stockList&type=fundBuy"
JUDAL_SELL_URL = "https://www.judal.co.kr/?view=stockList&type=fundSell"

# Toss Securities 비공식 API — stock-prices/details (시총/현재가/전일종가)
TOSS_STOCK_PRICES_URL = "https://wts-info-api.tossinvest.com/api/v3/stock-prices/details"
TOSS_TRADING_TREND_URL = "https://wts-info-api.tossinvest.com/api/v1/stock-infos/trade/trend/trading-trend"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
}


# ==========================================================================
# todayygg
# ==========================================================================

def fetch_todayygg_summary() -> Optional[dict]:
    """todayygg.com 의 summary_latest.json 다운로드."""
    try:
        r = requests.get(TODAYYGG_JSON_URL, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("todayygg JSON fetch 실패: %s", e)
        return None


def fetch_todayygg_csv() -> Optional[str]:
    """todayygg.com 의 analysis_latest.csv 다운로드 (텍스트 반환). UTF-8 강제."""
    try:
        r = requests.get(TODAYYGG_CSV_URL, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        # Content-Type charset 자동 추측이 ISO-8859-1로 떨어지면 한글 깨짐.
        # UTF-8로 강제 decode.
        return r.content.decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("todayygg CSV fetch 실패: %s", e)
        return None


# ==========================================================================
# 종목 자동 분류 (sector 가 빈 종목에 키워드/규칙 기반 sector 채움)
# ==========================================================================
ETF_PREFIXES = (
    "KODEX ", "TIGER ", "ACE ", "KOACT", "RISE ", "HANARO ", "SOL ",
    "1Q ", "PLUS ", "TIMEFOLIO", "WOORI", "KBSTAR", "MIRAE ASSET",
    "SMART", "ARIRANG", "FOCUS", "히어로", "WON ", "PLUSPRO",
)

# 종목명 키워드 → sector 매핑 (앞쪽 매칭 우선)
KEYWORD_SECTOR_MAP = [
    # 반도체 (장비/소재/부품/PCB 포함)
    (["반도체", "에스피", "하이닉스", "DB하이텍", "한미반도체", "이수페타시스",
      "테스", "원익IPS", "원익Qnc", "원익QnC", "주성", "ISC", "리노공업",
      "한솔케미칼", "솔브레인", "동진", "동운", "이오테크닉스", "넥스틴",
      "테크윙", "에스앤에스텍", "피에스케이", "코미코", "티에스이",
      "한양디지텍", "한양이엔지", "디엔에프", "비에이치", "디바이스",
      "두산테스나", "원익머트리얼즈", "고영", "네패스", "GST", "오스코텍",
      "예스티", "ISC", "한솔아이원스", "에이치브이엠", "삼화콘덴서",
      "삼현", "비츠로셀", "에스앤에스텍", "파두", "쎄트렉아이"], "반도체"),
    (["디스플레이", "LG디스플레이", "LX세미콘", "덕산네오룩스", "선익시스템",
      "덕산하이메탈", "코세스"], "디스플레이"),

    # 2차전지 / 배터리
    (["2차전지", "이차전지", "배터리", "에코프로", "엘앤에프", "포스코퓨처엠",
      "양극재", "음극재", "리튬", "코스모", "동화기업", "대주전자재료",
      "삼성SDI", "LG에너지", "에코프로비엠", "에코프로머티"], "2차전지"),

    # 바이오 / 제약
    (["바이오", "제약", "팜", "메디", "치료제", "신약", "백신", "셀트리온",
      "삼성바이오", "한미약품", "유한양행", "녹십자", "동아", "한올", "알테오젠",
      "리가켐", "올릭스", "펩트론", "오스코텍", "에이비엘", "에이프릴바이오",
      "엘앤씨바이오", "지투지바이오", "메디포스트", "한스바이오", "삼천당",
      "리센스메디컬", "노바렉스", "에스티팜", "오름테라"], "바이오/제약"),

    # 자동차
    (["현대차", "기아", "현대모비스", "한온시스템", "HL만도", "한국타이어",
      "현대글로비스", "현대위아", "현대로템", "자동차"], "자동차"),

    # 방산 / 조선
    (["방산", "디펜스", "에어로스페이스", "한화시스템", "한화에어로",
      "한국항공우주", "LIG넥스원", "한화오션", "STX엔진"], "방산"),
    (["HD한국조선", "HD현대중공업", "HD현대마린", "삼성중공업", "현대미포",
      "조선", "한화엔진", "비에이치아이"], "조선"),

    # 금융
    (["은행", "지주", "증권", "투자증권", "보험", "캐피탈", "카드",
      "KB금융", "신한지주", "하나금융", "우리금융", "iM금융", "BNK금융",
      "삼성생명", "삼성화재", "DB손해보험", "현대해상", "메리츠금융",
      "한국금융지주", "키움증권", "미래에셋", "NH투자", "삼성증권",
      "유진투자", "교보증권", "신영증권", "DGB", "기업은행"], "금융"),

    # IT / 소프트웨어 / 인터넷
    (["NAVER", "네이버", "카카오", "엔씨", "넷마블", "크래프톤",
      "더존비즈온", "하이브", "JYP", "위메이드", "SOOP", "SBS",
      "엔터", "엔터테인먼트", "삼성에스디에스", "LG씨엔에스", "현대오토에버",
      "더블유게임즈", "에스엠"], "IT/소프트웨어"),
    (["통신", "SK텔레콤", "LG유플러스", "KT"], "미디어/통신"),
    # 전기/전자 (반도체 외 일반 전자/가전/디스플레이 부품)
    (["LG전자", "삼성전기", "LG이노텍", "삼화콘덴서", "대덕전자",
      "HD현대일렉트릭", "LS ELECTRIC", "일진전기", "효성중공업",
      "한화비전", "비에이치", "성호전자"], "전기/전자"),

    # 화학 / 정유 / 소재
    (["화학", "케미컬", "이수화학", "OCI", "효성티앤씨", "롯데케미칼",
      "금호석유", "대한유화", "LG화학", "코오롱인더", "효성중공업",
      "S-Oil", "SK이노베이션"], "화학/정유"),

    # 철강 / 비철금속
    (["POSCO", "포스코", "고려아연", "현대제철", "동국제강", "세아베스틸",
      "풍산", "철강", "삼화콘덴서"], "철강/금속"),

    # 식음료 / 소비재
    (["식품", "푸드", "삼양", "오리온", "롯데칠성", "롯데쇼핑",
      "현대백화점", "신세계", "이마트", "BGF리테일", "GS리테일",
      "한섬", "F&F", "신세계인터", "코스맥스", "아모레", "한미글로벌"], "소비재/유통"),

    # 건설 / 인프라
    (["건설", "GS건설", "DL이앤씨", "삼성E&A", "현대건설", "대우건설",
      "현대엘리베이터", "한샘", "한국전력", "한전기술", "한전KPS",
      "한국가스공사", "씨에스윈드", "두산에너", "HD현대일렉트릭", "HD현대에너지"], "건설/인프라"),

    # 농업 / 기타 산업재
    (["산업", "엔진", "기계", "산일전기"], "산업재"),

    # 부동산 / 리츠
    (["리츠", "맥쿼리", "신한알파"], "부동산"),
]


# 허용된 깔끔한 sector 이름 목록 (이 안에 있거나 짧고 단순하면 통과)
ALLOWED_SECTORS = {
    "반도체", "디스플레이", "2차전지", "바이오/제약", "자동차", "방산", "조선",
    "금융", "IT/소프트웨어", "IT/전자", "소프트웨어", "미디어/통신", "통신",
    "화학/정유", "화학", "정유", "철강/금속", "소비재/유통", "소비재", "유통",
    "음식료", "음식료/소비재", "건설/인프라", "건설", "산업재", "부동산",
    "ETF/펀드", "ETF", "에너지", "유틸리티", "운송", "헬스케어", "화장품",
    "농업", "광물", "기타",
}


def _is_clean_sector(sec: str) -> bool:
    """sector 이름이 깔끔한지 판정."""
    if not sec:
        return False
    if sec in ALLOWED_SECTORS:
        return True
    # 너무 길거나 dirty 패턴
    if len(sec) > 10:
        return False
    if "(" in sec or ")" in sec:
        return False
    if sec.count(" ") > 1:
        return False
    return True   # 짧고 단순하면 통과


# ==========================================================================
# 네이버 금융 종목별 업종 스크래핑 (캐시 활용)
# ==========================================================================
NAVER_FINANCE_URL = "https://finance.naver.com/item/main.naver"

# 네이버 업종 (WICS) → 우리 표준 카테고리 매핑
NAVER_SECTOR_MAP = {
    # 반도체 / 디스플레이
    "반도체와반도체장비": "반도체",
    "반도체장비": "반도체",
    "반도체": "반도체",
    "전자장비와기기": "전기/전자",
    "전기제품": "전기/전자",
    "전자제품": "전기/전자",
    "전자부품": "전기/전자",
    "디스플레이장비및부품": "디스플레이",
    "디스플레이": "디스플레이",
    "전기부품및연결장치": "전기/전자",
    "전기조명": "전기/전자",
    # 자동차
    "자동차": "자동차",
    "자동차부품": "자동차",
    # 화학 / 정유
    "화학": "화학/정유",
    "정유": "화학/정유",
    "석유와가스": "에너지",
    "에너지장비및서비스": "에너지",
    # 철강 / 금속
    "철강": "철강/금속",
    "금속과광업": "철강/금속",
    "비철금속": "철강/금속",
    # 금융
    "은행": "금융",
    "다각화된금융서비스": "금융",
    "자본시장": "금융",
    "소비자금융": "금융",
    "보험": "금융",
    "손해보험": "금융",
    "생명보험": "금융",
    "증권": "금융",
    # IT / 소프트웨어
    "소프트웨어": "IT/소프트웨어",
    "응용소프트웨어": "IT/소프트웨어",
    "시스템소프트웨어": "IT/소프트웨어",
    "양방향미디어와서비스": "IT/소프트웨어",
    "엔터테인먼트": "IT/소프트웨어",
    "인터랙티브미디어및서비스": "IT/소프트웨어",
    "정보기술서비스": "IT/소프트웨어",
    "IT서비스": "IT/소프트웨어",
    # 미디어 / 통신
    "다각화된통신서비스": "미디어/통신",
    "무선통신서비스": "미디어/통신",
    "통신서비스": "미디어/통신",
    "미디어": "미디어/통신",
    "광고": "미디어/통신",
    "방송과엔터테인먼트": "미디어/통신",
    # 바이오 / 제약
    "제약": "바이오/제약",
    "생명과학도구및서비스": "바이오/제약",
    "건강관리장비와용품": "바이오/제약",
    "건강관리기술": "바이오/제약",
    "건강관리업체및서비스": "바이오/제약",
    "건강관리장비와서비스": "바이오/제약",
    "바이오테크": "바이오/제약",
    "생명공학": "바이오/제약",
    # 음식료 / 소비재
    "식품": "음식료/소비재",
    "식품과기본식료품소매": "음식료/소비재",
    "음료": "음식료/소비재",
    "담배": "음식료/소비재",
    # 소비재 / 유통
    "가정용품": "소비재/유통",
    "의류,신발및호화품": "소비재/유통",
    "섬유,의류,신발및호화품": "소비재/유통",
    "내구소비재와의류": "소비재/유통",
    "백화점과일반상점": "소비재/유통",
    "전문소매": "소비재/유통",
    "복합기업": "소비재/유통",
    "호텔,레스토랑,레저": "소비재/유통",
    "여행과여가": "소비재/유통",
    # 화장품
    "화장품": "화장품",
    "퍼스널케어": "화장품",
    # 건설 / 인프라
    "건설": "건설/인프라",
    "건설업": "건설/인프라",
    "건설과엔지니어링": "건설/인프라",
    "건축자재": "건설/인프라",
    "건축제품": "건설/인프라",
    # 조선 / 방산
    "조선": "조선",
    "방위산업": "방산",
    "항공우주와국방": "방산",
    # 산업재
    "기계": "산업재",
    "산업재": "산업재",
    "거래회사와판매업체": "산업재",
    "사무용전자제품": "산업재",
    "상업서비스와공급품": "산업재",
    # 유틸리티
    "전기유틸리티": "유틸리티",
    "복합유틸리티": "유틸리티",
    "가스유틸리티": "유틸리티",
    "수도유틸리티": "유틸리티",
    "독립전력생산및에너지거래업자": "유틸리티",
    # 운송
    "항공화물운송과물류": "운송",
    "도로와철도": "운송",
    "해운사": "운송",
    "항공사": "운송",
    "운송인프라": "운송",
    # 부동산
    "부동산": "부동산",
    "부동산투자신탁": "부동산",
    "부동산관리및개발": "부동산",
    # 2차전지 / 에너지저장
    "전기장비": "2차전지",   # LG에너지 등이 여기 분류됨
}


def fetch_naver_sector(stock_code: str) -> str:
    """
    네이버 금융 종목 페이지에서 WICS 업종 추출.
    return: 업종명 (예: "반도체와반도체장비") 또는 "" 실패 시.
    """
    if not stock_code or not stock_code.isdigit() or len(stock_code) != 6:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ""
    try:
        r = requests.get(
            NAVER_FINANCE_URL,
            params={"code": stock_code},
            headers=_HEADERS,
            timeout=8,
        )
        r.raise_for_status()
        r.encoding = "euc-kr"   # 네이버 금융은 euc-kr
    except Exception:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    # 업종 링크 (a[href*="sise_group_detail"])
    a = soup.find("a", href=lambda h: h and ("sise_group_detail" in h or "sise_group.naver" in h))
    if a:
        return a.get_text(strip=True)
    # backup: section_strategy 안의 텍스트
    section = soup.find("div", {"class": "wrap_company"})
    if section:
        link = section.find("a", href=lambda h: h and "WICS" in (h or ""))
        if link:
            return link.get_text(strip=True)
    return ""


def fetch_naver_sectors_bulk(stock_codes: list, cache_path: Optional[str] = None,
                              max_new_fetches: int = 100) -> dict:
    """
    종목 코드 리스트에 대해 네이버 sector 일괄 fetch.
    캐시 활용 + 신규 fetch 한도 (1회 빌드당 max_new_fetches).
    return: {stock_code: standard_sector_name}  (표준 카테고리로 변환됨)
    """
    import json as _json
    import time
    # 캐시 로드
    cache: dict = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = _json.load(f)
        except Exception:
            cache = {}
    result = {}
    new_fetches = 0
    miss_codes = []
    for code in stock_codes:
        if code in cache:
            cached_val = cache[code]
            if cached_val:   # 빈 값은 다시 시도
                result[code] = cached_val
                continue
        miss_codes.append(code)
    # 미캐시 종목만 fetch (한도 내에서)
    for code in miss_codes[:max_new_fetches]:
        naver_raw = fetch_naver_sector(code)
        # 네이버 업종 → 표준 카테고리
        standard = NAVER_SECTOR_MAP.get(naver_raw, "")
        cache[code] = standard or naver_raw or ""   # 빈 값도 캐시 (재시도 방지)
        if standard:
            result[code] = standard
        new_fetches += 1
        time.sleep(0.15)   # rate limit
    # 캐시 저장
    if cache_path and new_fetches > 0:
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                _json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("네이버 sector 캐시 저장 실패: %s", e)
    log.info("네이버 sector: %d 종목 (캐시 hit=%d, 신규 fetch=%d, miss=%d)",
             len(result),
             len(stock_codes) - len(miss_codes),
             new_fetches,
             max(0, len(miss_codes) - new_fetches))
    return result


# ==========================================================================
# 종목 코드 → sector 직접 매핑 (가장 정확, 키워드 매핑보다 우선)
# 자주 등장하는 종목 + 사용자 보유 종목 + 미분류로 자주 잡혔던 종목들
# ==========================================================================
CODE_SECTOR_MAP = {
    # === 반도체 (시총 상위 + KOSDAQ 부품/장비) ===
    "005930": "반도체", "000660": "반도체",         # 삼성전자, SK하이닉스
    "005935": "반도체",                              # 삼성전자우
    "042700": "반도체",                              # 한미반도체
    "000990": "반도체",                              # DB하이텍
    "007660": "반도체",                              # 이수페타시스
    "353200": "반도체",                              # 대덕전자
    "195870": "반도체",                              # 해성디에스
    "058470": "반도체",                              # 리노공업
    "095610": "반도체", "240810": "반도체",          # 테스, 원익IPS
    "074600": "반도체",                              # 원익QnC
    "104830": "반도체",                              # 원익머트리얼즈
    "036930": "반도체",                              # 주성엔지니어링
    "095340": "반도체",                              # ISC
    "131970": "반도체",                              # 두산테스나
    "035080": "반도체",                              # 모트렉스 (대체)
    "036540": "반도체",                              # SFA반도체
    "039030": "반도체",                              # 이오테크닉스
    "319660": "반도체",                              # 피에스케이
    "031980": "반도체",                              # 피에스케이홀딩스
    "183300": "반도체",                              # 코미코
    "064760": "반도체",                              # 티씨케이
    "131290": "반도체",                              # 티에스이
    "089030": "반도체",                              # 테크윙
    "348210": "반도체",                              # 넥스틴
    "101490": "반도체",                              # 에스앤에스텍
    "295310": "반도체",                              # 에이치브이엠
    "187870": "반도체",                              # 디바이스ENG
    "064290": "반도체",                              # 인텍플러스
    "098460": "반도체",                              # 고영
    "033640": "반도체",                              # 네패스
    "083450": "반도체",                              # GST
    "077360": "반도체",                              # 덕산하이메탈
    "061970": "반도체",                              # LB세미콘
    "078350": "반도체",                              # 한양디지텍
    "045100": "반도체",                              # 한양이엔지
    "092070": "반도체",                              # 디엔에프
    "014680": "반도체",                              # 한솔케미칼
    "036540": "반도체",                              # SFA반도체
    "440110": "반도체",                              # 파두
    "099320": "방산",                                 # 쎄트렉아이 (인공위성)
    "080220": "반도체",                              # 제주반도체 (사용자 보유)
    "122640": "반도체",                              # 예스티
    "114810": "반도체",                              # 한솔아이원스

    # === 디스플레이 ===
    "034220": "디스플레이",                          # LG디스플레이
    "213420": "디스플레이",                          # 덕산네오룩스
    "171090": "디스플레이",                          # 선익시스템
    "089890": "디스플레이",                          # 코세스
    "088130": "디스플레이",                          # 동아엘텍

    # === 전기/전자 ===
    "066570": "전기/전자",                           # LG전자
    "011070": "전기/전자",                           # LG이노텍
    "009150": "전기/전자",                           # 삼성전기
    "001820": "전기/전자",                           # 삼화콘덴서
    "043260": "전기/전자",                           # 성호전자
    "001440": "전기/전자",                           # 대한전선 (사용자 보유)
    "082920": "전기/전자",                           # 비츠로셀
    "267260": "전기/전자",                           # HD현대일렉트릭
    "010120": "전기/전자",                           # LS ELECTRIC
    "103590": "전기/전자",                           # 일진전기
    "298040": "전기/전자",                           # 효성중공업
    "489790": "전기/전자",                           # 한화비전
    "090460": "전기/전자",                           # 비에이치
    "033240": "전기/전자",                           # 자화전자

    # === 2차전지 ===
    "006400": "2차전지",                             # 삼성SDI
    "373220": "2차전지",                             # LG에너지솔루션
    "247540": "2차전지",                             # 에코프로비엠
    "086520": "2차전지",                             # 에코프로
    "450080": "2차전지",                             # 에코프로머티
    "066970": "2차전지",                             # 엘앤에프
    "003670": "2차전지",                             # 포스코퓨처엠
    "078600": "2차전지",                             # 대주전자재료
    "091580": "2차전지",                             # 상신이디피
    "295310": "2차전지",                             # 에이치브이엠 (반도체로 매핑 우선)
    "121600": "2차전지",                             # 나노신소재
    "279570": "2차전지",                             # 케이뱅크 (실제 케뱅 — 금융이지만 매핑)

    # === 바이오/제약 ===
    "207940": "바이오/제약",                         # 삼성바이오로직스
    "068270": "바이오/제약",                         # 셀트리온
    "128940": "바이오/제약",                         # 한미약품
    "006280": "바이오/제약",                         # 녹십자
    "009420": "바이오/제약",                         # 한올바이오파마
    "196170": "바이오/제약",                         # 알테오젠
    "141080": "바이오/제약",                         # 리가켐바이오
    "226950": "바이오/제약",                         # 올릭스
    "087010": "바이오/제약",                         # 펩트론
    "039200": "바이오/제약",                         # 오스코텍
    "298380": "바이오/제약",                         # 에이비엘바이오
    "397030": "바이오/제약",                         # 에이프릴바이오
    "237690": "바이오/제약",                         # 에스티팜
    "290650": "바이오/제약",                         # 엘앤씨바이오
    "456160": "바이오/제약",                         # 지투지바이오
    "078160": "바이오/제약",                         # 메디포스트
    "475830": "바이오/제약",                         # 오름테라퓨틱
    "476830": "바이오/제약",                         # 알지노믹스
    "086450": "바이오/제약",                         # 동국제약
    "042520": "바이오/제약",                         # 한스바이오메드
    "394420": "바이오/제약",                         # 리센스메디컬
    "000250": "바이오/제약",                         # 삼천당제약
    "328130": "바이오/제약",                         # 루닛
    "347850": "바이오/제약",                         # 디앤디파마텍
    "194700": "바이오/제약",                         # 노바렉스
    "0126Z0": "바이오/제약",                         # 삼성에피스홀딩스

    # === 자동차 ===
    "005380": "자동차", "005385": "자동차", "005387": "자동차",  # 현대차, 우, 2우B
    "000270": "자동차",                              # 기아
    "012330": "자동차",                              # 현대모비스
    "204320": "자동차",                              # HL만도
    "086280": "자동차",                              # 현대글로비스
    "011210": "자동차",                              # 현대위아
    "064350": "자동차",                              # 현대로템
    "307950": "자동차",                              # 현대오토에버
    "294870": "자동차",                              # IPARK현대산업개발 (애매)

    # === 조선 ===
    "009540": "조선",                                # HD한국조선해양
    "329180": "조선",                                # HD현대중공업
    "443060": "조선",                                # HD현대마린솔루션
    "267270": "조선",                                # HD건설기계
    "010140": "조선",                                # 삼성중공업
    "042660": "조선",                                # 한화오션
    "082740": "조선",                                # 한화엔진
    "100090": "조선",                                # SK오션플랜트
    "083650": "조선",                                # 비에이치아이 (보일러 → 조선 인프라)
    "077970": "조선",                                # STX엔진

    # === 방산 ===
    "079550": "방산",                                # LIG디펜스앤에어로스페이스
    "012450": "방산",                                # 한화에어로스페이스
    "272210": "방산",                                # 한화시스템
    "047810": "방산",                                # 한국항공우주

    # === 금융 (지주/은행/증권/보험) ===
    "105560": "금융",                                # KB금융
    "055550": "금융",                                # 신한지주
    "086790": "금융",                                # 하나금융지주
    "316140": "금융",                                # 우리금융지주
    "139130": "금융",                                # iM금융지주
    "138930": "금융",                                # BNK금융지주
    "138040": "금융",                                # 메리츠금융지주
    "024110": "금융",                                # 기업은행
    "006800": "금융",                                # 미래에셋증권
    "005940": "금융",                                # NH투자증권
    "016360": "금융",                                # 삼성증권
    "039490": "금융",                                # 키움증권
    "071050": "금융",                                # 한국금융지주
    "001200": "금융",                                # 유진투자증권
    "030610": "금융",                                # 교보증권
    "001720": "금융",                                # 신영증권
    "032830": "금융",                                # 삼성생명
    "088350": "금융",                                # 한화생명
    "000810": "금융",                                # 삼성화재
    "005830": "금융",                                # DB손해보험
    "001450": "금융",                                # 현대해상
    "031210": "금융",                                # 서울보증보험

    # === IT/소프트웨어 ===
    "035420": "IT/소프트웨어",                       # NAVER
    "035720": "IT/소프트웨어",                       # 카카오
    "036570": "IT/소프트웨어",                       # NC
    "251270": "IT/소프트웨어",                       # 넷마블
    "259960": "IT/소프트웨어",                       # 크래프톤
    "352820": "IT/소프트웨어",                       # 하이브
    "035900": "IT/소프트웨어",                       # JYP Ent.
    "012510": "IT/소프트웨어",                       # 더존비즈온
    "192080": "IT/소프트웨어",                       # 더블유게임즈
    "018260": "IT/소프트웨어",                       # 삼성에스디에스
    "064400": "IT/소프트웨어",                       # LG씨엔에스
    "067160": "IT/소프트웨어",                       # SOOP

    # === 미디어/통신 ===
    "017670": "미디어/통신",                         # SK텔레콤
    "032640": "미디어/통신",                         # LG유플러스
    "030200": "미디어/통신",                         # KT

    # === 화학/정유 ===
    "051910": "화학/정유",                           # LG화학
    "096770": "화학/정유",                           # SK이노베이션
    "010950": "화학/정유",                           # S-Oil
    "011170": "화학/정유",                           # 롯데케미칼
    "011780": "화학/정유",                           # 금호석유화학
    "009830": "화학/정유",                           # 한화솔루션
    "010060": "화학/정유",                           # OCI홀딩스
    "005950": "화학/정유",                           # 이수화학
    "457190": "화학/정유",                           # 이수스페셜티케미컬
    "069260": "화학/정유",                           # TKG휴켐스
    "298020": "화학/정유",                           # 효성티앤씨
    "004800": "화학/정유",                           # 효성
    "006650": "화학/정유",                           # 대한유화
    "120110": "화학/정유",                           # 코오롱인더

    # === 철강/금속 ===
    "005490": "철강/금속",                           # POSCO홀딩스
    "010130": "철강/금속",                           # 고려아연
    "004020": "철강/금속",                           # 현대제철
    "460860": "철강/금속",                           # 동국제강
    "001430": "철강/금속",                           # 세아베스틸지주
    "103140": "철강/금속",                           # 풍산
    "017960": "철강/금속",                           # 한국카본
    "006110": "철강/금속",                           # 삼아알미늄
    "044490": "철강/금속",                           # 태웅

    # === 음식료/소비재 ===
    "003230": "음식료/소비재",                       # 삼양식품
    "001040": "음식료/소비재",                       # CJ
    "005300": "음식료/소비재",                       # 롯데칠성
    "001740": "음식료/소비재",                       # SK네트웍스

    # === 소비재/유통 ===
    "028260": "소비재/유통",                         # 삼성물산
    "007070": "소비재/유통",                         # GS리테일
    "282330": "소비재/유통",                         # BGF리테일
    "069960": "소비재/유통",                         # 현대백화점
    "004170": "소비재/유통",                         # 신세계
    "139480": "소비재/유통",                         # 이마트
    "023530": "소비재/유통",                         # 롯데쇼핑
    "020000": "소비재/유통",                         # 한섬
    "031430": "소비재/유통",                         # 신세계인터내셔날
    "383220": "소비재/유통",                         # F&F
    "192820": "소비재/유통",                         # 코스맥스
    "111770": "소비재/유통",                         # 영원무역
    "009970": "소비재/유통",                         # 영원무역홀딩스
    "034230": "소비재/유통",                         # 파라다이스
    "039130": "소비재/유통",                         # 하나투어
    "452260": "소비재/유통",                         # 한화갤러리아
    "030000": "소비재/유통",                         # 제일기획
    "009240": "소비재/유통",                         # 한샘
    "419530": "소비재/유통",                         # SAMG엔터

    # === 화장품 ===
    "090430": "화장품",                              # 아모레퍼시픽
    "278470": "화장품",                              # 에이피알

    # === 건설/인프라 ===
    "006360": "건설/인프라",                         # GS건설
    "047040": "건설/인프라",                         # 대우건설
    "000720": "건설/인프라",                         # 현대건설
    "375500": "건설/인프라",                         # DL이앤씨
    "028050": "건설/인프라",                         # 삼성E&A
    "017800": "건설/인프라",                         # 현대엘리베이터
    "060980": "건설/인프라",                         # HL홀딩스
    "010780": "건설/인프라",                         # 아이에스동서

    # === 에너지 / 유틸리티 ===
    "015760": "유틸리티",                            # 한국전력
    "036460": "유틸리티",                            # 한국가스공사
    "051600": "유틸리티",                            # 한전KPS
    "052690": "유틸리티",                            # 한전기술
    "112610": "에너지",                              # 씨에스윈드
    "034020": "에너지",                              # 두산에너빌리티
    "336260": "에너지",                              # 두산퓨얼셀
    "322000": "에너지",                              # HD현대에너지솔루션

    # === 산업재 ===
    "062040": "산업재",                              # 산일전기
    "454910": "산업재",                              # 두산로보틱스
    "108670": "산업재",                              # LX하우시스
    "232680": "산업재",                              # 라온로보틱스

    # === 지주 / 복합 (애매) ===
    "034730": "금융",                                # SK (지주)
    "003550": "금융",                                # LG (지주)
    "000150": "금융",                                # 두산
    "005440": "금융",                                # 현대지에프홀딩스

    # === 운송 ===
    "003490": "운송",                                # 대한항공

    # === ETF (코드 패턴) ===
    # ETF_PREFIXES 가 종목명으로 잡지만 명시 안전망
    "069500": "ETF/펀드", "102110": "ETF/펀드",      # KODEX 200, TIGER 200
    "148020": "ETF/펀드",                            # RISE 200
    "305540": "ETF/펀드",                            # TIGER 2차전지테마
    "329200": "ETF/펀드",                            # TIGER 리츠부동산인프라
    "434730": "ETF/펀드",                            # HANARO 원자력iSelect
    "491820": "ETF/펀드",                            # HANARO 전력설비투자
    "466920": "ETF/펀드",                            # SOL 조선TOP3플러스
    "461580": "ETF/펀드",                            # TIGER 코스닥글로벌
    "494330": "ETF/펀드",                            # ACE 라이프자산주주가치액티브

    # === 사용자 보유 종목 (네이버 검증 코드) ===
    "469160": "반도체",                              # TIGER 일본반도체 FACTSET (사용자 보유)
    "0015B0": "ETF/펀드",                            # KoAct 미국나스닥성장기업액티브 (사용자 보유)
}


def auto_classify_sector(stock_code: str, stock_name: str, existing: str = "") -> str:
    """
    sector 자동 분류 (우선순위):
    1) 종목 코드 매핑 (CODE_SECTOR_MAP)
    2) ETF 종목명 패턴
    3) 종목명 키워드 매핑
    """
    # 1) 코드 매핑 (가장 정확)
    if stock_code and stock_code in CODE_SECTOR_MAP:
        return CODE_SECTOR_MAP[stock_code]

    if not stock_name:
        return ""
    name = stock_name.upper().strip()

    # 2) ETF / 펀드 (종목명 prefix)
    for p in ETF_PREFIXES:
        if name.startswith(p.upper()):
            return "ETF/펀드"

    # 3) 키워드 매칭
    for keywords, sector in KEYWORD_SECTOR_MAP:
        for kw in keywords:
            if kw.upper() in name:
                return sector

    return ""


def fill_sector_for_preferred(rows: list):
    """
    우선주 (종목명에 "우", "우B" 끝, 코드 끝 5/7) → 본주의 sector 상속.
    in-place 수정.
    """
    # 본주 매핑: 종목명에서 "우"/"우B" 제거한 이름 → sector
    base_sector = {}
    for r in rows:
        name = (r.get("stock_name") or "").strip()
        sec = (r.get("sector") or "").strip()
        if sec and not name.endswith("우") and not name.endswith("우B"):
            base_sector[name] = sec
    # 우선주 sector 상속
    filled = 0
    for r in rows:
        if r.get("sector"):
            continue
        name = (r.get("stock_name") or "").strip()
        base_name = None
        if name.endswith("우B"):
            base_name = name[:-2].strip()
        elif name.endswith("우"):
            base_name = name[:-1].strip()
        if base_name and base_name in base_sector:
            r["sector"] = base_sector[base_name]
            filled += 1
    if filled:
        log.info("우선주 sector 상속: %d 종목", filled)


def parse_todayygg_csv(csv_text: str) -> list:
    """todayygg CSV → list of standard row dict."""
    import csv as csvlib
    import io
    if not csv_text:
        return []
    reader = csvlib.DictReader(io.StringIO(csv_text))
    # CSV 컬럼명 진단용 로그 (한 번만)
    if reader.fieldnames:
        log.info("todayygg CSV fields (%d개): %s", len(reader.fieldnames), reader.fieldnames)
    # sector 후보 키들 (영문/한글 둘 다 시도)
    SECTOR_KEYS = ("sector", "industry", "category", "업종", "섹터", "분류",
                   "sub_industry", "gics_sector", "stock_sector")
    rows = []
    nonempty_sector = 0
    for r in reader:
        code = (r.get("symbol") or "").strip()
        if not code or not code.isdigit():
            continue
        # sector: 여러 후보 키 시도
        sector_val = ""
        for k in SECTOR_KEYS:
            v = r.get(k)
            if v and str(v).strip():
                sector_val = str(v).strip()
                break
        if sector_val:
            nonempty_sector += 1
        rows.append({
            "stock_code": code,
            "stock_name": r.get("name", ""),
            "market": r.get("market", ""),
            "buy_amount":  _to_int(r.get("buy_amount")),
            "sell_amount": _to_int(r.get("sell_amount")),
            "net_amount":  _to_int(r.get("net_buy_amount")),
            "buy_qty":  _to_int(r.get("buy_quantity")),
            "sell_qty": _to_int(r.get("sell_quantity")),
            "net_qty":  _to_int(r.get("net_buy_quantity")),
            "market_cap": _to_int(r.get("market_cap")),
            "close_price": _to_int(r.get("close_price")),
            "today_buy_avg":   _to_float(r.get("today_buy_average_price")),
            "period_buy_avg":  _to_float(r.get("period_buy_average_price")),
            "cumulative_net_amount": _to_int(r.get("cumulative_net_buy_amount")),
            "consecutive_sell_days": _to_int(r.get("consecutive_sell_days")),
            "period_start_date": r.get("period_start_date", ""),
            "period_end_date": r.get("period_end_date", ""),
            "delta_buy_amount":  _to_int(r.get("delta_buy_amount_vs_yesterday")),
            "delta_sell_amount": _to_int(r.get("delta_sell_amount_vs_yesterday")),
            "delta_net_amount":  _to_int(r.get("delta_net_buy_amount_vs_yesterday")),
            "net_vs_prev_vol_ratio":  _to_float(r.get("net_buy_vs_prev_volume_ratio")),
            "net_vs_prev_val_ratio":  _to_float(r.get("net_buy_amount_vs_prev_trade_value_ratio")),
            "sector":  sector_val,
            "industry": r.get("industry", ""),
            "source": "todayygg-csv",
        })
    log.info("todayygg CSV parsed: %d rows (sector 채워진 row: %d)",
             len(rows), nonempty_sector)
    return rows


def _to_int(v) -> int:
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).replace(",", "").strip()
    try:
        return int(float(s))
    except Exception:
        return 0


def _to_float(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    try:
        return float(s)
    except Exception:
        return 0.0


def todayygg_to_standard_rows(payload: dict) -> tuple:
    """
    todayygg JSON → 표준 row 포맷.
    return: (trade_date, [rows])  rows 는 list of dict (generate.py 가 기대하는 키들)
    """
    if not payload:
        return None, []
    trade_date = payload.get("trade_date", "")
    rows_by_code = {}

    # buy/sell/market_cap top 종목들 → dedup
    for src_key in ("buy_top30", "sell_top30", "market_cap_buy_top30", "market_cap_sell_top30"):
        for r in payload.get(src_key) or []:
            code = (r.get("symbol") or "").strip()
            if not code:
                continue
            if code in rows_by_code:
                continue
            rows_by_code[code] = {
                "stock_code": code,
                "stock_name": r.get("name", ""),
                "market": r.get("market", ""),
                "buy_amount":  _to_int(r.get("buy_amount")),
                "sell_amount": _to_int(r.get("sell_amount")),
                "net_amount":  _to_int(r.get("net_buy_amount")),
                "buy_qty":  _to_int(r.get("buy_quantity")),
                "sell_qty": _to_int(r.get("sell_quantity")),
                "net_qty":  _to_int(r.get("net_buy_quantity")),
                "market_cap": _to_int(r.get("market_cap")),
                "close_price": _to_int(r.get("close_price")),
                "today_buy_avg":   _to_float(r.get("today_buy_average_price")),
                "period_buy_avg":  _to_float(r.get("period_buy_average_price")),
                "cumulative_net_amount": _to_int(r.get("cumulative_net_buy_amount")),
                "consecutive_sell_days": _to_int(r.get("consecutive_sell_days")),
                "period_start_date": r.get("period_start_date", ""),
                "period_end_date": r.get("period_end_date", ""),
                "period_id": _to_int(r.get("period_id")),
                "delta_buy_amount":  _to_int(r.get("delta_buy_amount_vs_yesterday")),
                "delta_sell_amount": _to_int(r.get("delta_sell_amount_vs_yesterday")),
                "delta_net_amount":  _to_int(r.get("delta_net_buy_amount_vs_yesterday")),
                "net_vs_prev_vol_ratio":  _to_float(r.get("net_buy_vs_prev_volume_ratio")),
                "net_vs_prev_val_ratio":  _to_float(r.get("net_buy_amount_vs_prev_trade_value_ratio")),
                "cap_gross_ratio":  _to_float(r.get("market_cap_gross_trade_ratio")),
                "sector":  r.get("sector", ""),
                "industry": r.get("industry", ""),
                "in_active_period": bool(r.get("in_active_period") or False),
                "source": "todayygg",
            }
    rows = list(rows_by_code.values())
    log.info("todayygg → %d 종목 (trade_date=%s)", len(rows), trade_date)
    return trade_date, rows


# ==========================================================================
# Toss stock-prices/details — 시총/현재가/등락률 보강용
# ==========================================================================

def fetch_toss_trading_trend(codes: list) -> dict:
    """
    토스 trading-trend — 종목별 일별 투자자별 매매 (장중 분 단위 갱신).
    종목당 1 호출 (size=1로 오늘 데이터만).

    return: {stock_code: {
        base_date, updated_at,
        pension_buy_qty, pension_sell_qty, pension_net_qty,
        foreigner_buy_qty, foreigner_sell_qty, foreigner_net_qty,
        institution_buy_qty, institution_sell_qty, institution_net_qty,
        close, is_today: bool
    }}
    """
    if not codes:
        return {}
    import datetime as dt
    today = dt.date.today().strftime("%Y-%m-%d")
    result = {}

    for code in codes:
        if not code:
            continue
        product_code = code if code.startswith("A") else f"A{code}"
        try:
            r = requests.get(
                TOSS_TRADING_TREND_URL,
                params={"productCode": product_code, "size": 1},
                headers=_HEADERS,
                timeout=10,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            log.warning("toss trading-trend %s failed: %s", code, e)
            continue

        body = (payload.get("result") or {}).get("body") or []
        if not body:
            continue
        latest = body[0]
        base_date = latest.get("baseDate", "")
        is_today = (base_date == today)

        # 연기금: 매수량 + 순매수 → 매도량 도출
        pension_buy = int(latest.get("pensionFundBuyVolume") or 0)
        pension_net = int(latest.get("netPensionFundBuyVolume") or 0)
        pension_sell = pension_buy - pension_net

        # 외국인
        foreigner_buy = int(latest.get("foreignerBuyVolume") or 0)
        foreigner_sell = int(latest.get("foreignerSellVolume") or 0)
        foreigner_net = int(latest.get("netForeignerBuyVolume") or 0)

        # 기관 합계
        institution_buy = int(latest.get("institutionBuyVolume") or 0)
        institution_sell = int(latest.get("institutionSellVolume") or 0)
        institution_net = int(latest.get("netInstitutionBuyVolume") or 0)

        result[code.lstrip("A")] = {
            "base_date": base_date,
            "updated_at": latest.get("updatedAt", ""),
            "is_today": is_today,
            "pension_buy_qty": pension_buy,
            "pension_sell_qty": pension_sell,
            "pension_net_qty": pension_net,
            "foreigner_buy_qty": foreigner_buy,
            "foreigner_sell_qty": foreigner_sell,
            "foreigner_net_qty": foreigner_net,
            "institution_buy_qty": institution_buy,
            "institution_sell_qty": institution_sell,
            "institution_net_qty": institution_net,
            "close": int(latest.get("close") or 0),
        }
    log.info("toss trading-trend → %d 종목 (today=%d개)",
             len(result), sum(1 for v in result.values() if v["is_today"]))
    return result


def fetch_toss_stock_prices(codes: list, chunk_size: int = 50) -> dict:
    """
    토스 stock-prices/details 호출. 종목코드(6자리, 'A' 없이) 리스트 → 'A' 붙여 호출.
    여러 종목 한 번에 가능 (콤마 구분). 한도 회피 위해 chunk로 쪼개서.

    return: {stock_code(6자리): {close, base, market_cap, change_rate, volume, value, ...}}
    """
    if not codes:
        return {}
    result = {}
    # 'A' prefix 정규화 + chunking
    a_codes = [(c if c.startswith("A") else f"A{c}") for c in codes if c]
    for i in range(0, len(a_codes), chunk_size):
        chunk = a_codes[i:i + chunk_size]
        try:
            r = requests.get(
                TOSS_STOCK_PRICES_URL,
                params={"productCodes": ",".join(chunk)},
                headers=_HEADERS,
                timeout=15,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            log.warning("toss stock-prices fetch failed chunk %d: %s", i, e)
            continue

        for item in payload.get("result") or []:
            code = (item.get("code") or "").lstrip("A")
            if not code:
                continue
            close = _to_int(item.get("close"))
            base = _to_int(item.get("base"))
            change_rate = ((close - base) / base * 100) if base > 0 else 0.0
            result[code] = {
                "close": close,
                "base": base,
                "market_cap": _to_int(item.get("marketCap")),
                "volume": _to_int(item.get("volume")),
                "value": _to_int(item.get("value")),
                "change_rate": round(change_rate, 2),
                "change_type": item.get("changeType", ""),
                "high52w": _to_int(item.get("high52w")),
                "low52w": _to_int(item.get("low52w")),
                "trading_strength": _to_float(item.get("tradingStrength")),
            }
    log.info("toss stock-prices → %d 종목 가격/시총 수집", len(result))
    return result


# ==========================================================================
# judal
# ==========================================================================

def _parse_amount_eok(text: str) -> int:
    """'388억' / '1,234억' → int(원). '5,210만' → int(원)."""
    if not text:
        return 0
    t = text.replace(",", "").strip()
    m = re.match(r"(-?[\d.]+)\s*(억원|억|만원|만)?$", t)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2) or ""
    if "억" in unit:
        return int(val * 100_000_000)
    if "만" in unit:
        return int(val * 10_000)
    return int(val)


def fetch_judal(direction: str = "buy") -> list:
    """
    주달 연기금 매수/매도 페이지 스크래핑.
    direction: 'buy' (fundBuy) | 'sell' (fundSell)

    return: [{stock_code(없을 수 있음), stock_name, amount, current_price, change_pct_52w,
              pbr, per, eps, market_cap, ...}]
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("beautifulsoup4 미설치 → judal 스크래핑 skip")
        return []

    url = JUDAL_BUY_URL if direction == "buy" else JUDAL_SELL_URL
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log.warning("judal %s fetch 실패: %s", direction, e)
        return []

    import re

    soup = BeautifulSoup(r.text, "html.parser")
    # 데이터 테이블 찾기 (페이지에 여러 표가 있을 수 있으니 가장 큰 표를 우선)
    candidates = soup.find_all("table")
    target = None
    max_rows = 0
    for t in candidates:
        n = len(t.find_all("tr"))
        if n > max_rows:
            target = t
            max_rows = n
    if not target:
        log.warning("judal %s: table not found", direction)
        return []

    # judal HTML 구조 특성:
    #   - 헤더 행: 모든 셀이 <th>
    #   - 데이터 행: 첫 셀(종목명) = <th scope="row">, 나머지 = <td>
    # 따라서 모든 <th> 를 헤더로 보면 종목명 셀들이 섞여 진짜 헤더 + 종목명들이 함께 나옴.
    # → 행 단위로 처리하면서 "모두 th 인 행"만 헤더로 인식.
    header_cells_text = []
    data_rows = []
    for tr in target.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells or len(cells) < 3:
            continue
        if all(c.name == "th" for c in cells):
            # 진짜 헤더 행 (첫 한 번만 채택)
            if not header_cells_text:
                header_cells_text = [c.get_text(strip=True) for c in cells]
        else:
            data_rows.append(cells)

    log.info("judal %s headers: %s", direction, header_cells_text[:25])
    log.info("judal %s data rows: %d", direction, len(data_rows))

    if not header_cells_text or not data_rows:
        log.warning("judal %s: header or data rows missing", direction)
        return []

    # 컬럼 인덱스 매핑 (퍼지)
    def _find_col(*candidates):
        for cand in candidates:
            for i, h in enumerate(header_cells_text):
                if cand in h:
                    return i
        return -1

    idx_name   = _find_col("종목명")
    idx_amount = _find_col("매수금액" if direction == "buy" else "매도금액", "금액")
    idx_price  = _find_col("현재가")
    idx_52w_var = _find_col("52주 변동률", "52주변동률")
    idx_3y_var  = _find_col("3년 변동률", "3년변동률")
    idx_pbr    = _find_col("PBR")
    idx_per    = _find_col("PER")
    idx_eps    = _find_col("EPS")
    idx_mcap   = _find_col("시가총액", "시총")
    idx_expected = _find_col("기대 수익률", "기대수익률")
    idx_3d_sum   = _find_col("3일합산", "3일 합산")
    idx_theme    = _find_col("관련테마", "테마")

    # 종목명 셀 텍스트에서 "삼성전자KOSPI 005930Information" → name/market/code 추출
    name_re = re.compile(r'^(.+?)(KOSPI|KOSDAQ|KONEX)\s*([0-9A-Z]{6,7})')

    rows = []
    for cells in data_rows:
        def _cell(i):
            if i < 0 or i >= len(cells):
                return ""
            return cells[i].get_text(strip=True)

        raw_name = _cell(idx_name)
        if not raw_name:
            continue
        # 종목명 / 시장 / 코드 분리
        m = name_re.match(raw_name)
        if m:
            name, market, code = m.group(1).strip(), m.group(2), m.group(3)
        else:
            # 정규식 매칭 실패 → 텍스트 그대로 종목명, 시장·코드 빈 값
            name, market, code = raw_name, "", ""
            # "Information" suffix 제거
            if name.endswith("Information"):
                name = name[:-len("Information")].strip()

        rows.append({
            "stock_code": code,
            "stock_name": name,
            "market": market,
            "amount": _parse_amount_eok(_cell(idx_amount)),
            "current_price": _to_int(_cell(idx_price)),
            "change_pct_52w": _cell(idx_52w_var),
            "change_pct_3y":  _cell(idx_3y_var),
            "pbr": _to_float(_cell(idx_pbr)),
            "per": _to_float(_cell(idx_per)),
            "eps": _to_float(_cell(idx_eps)),
            "market_cap": _parse_amount_eok(_cell(idx_mcap)),
            "expected_return": _cell(idx_expected),
            "three_day_sum": _parse_amount_eok(_cell(idx_3d_sum)),
            "theme": _cell(idx_theme),
            "source": "judal",
            "direction": direction,
        })
    log.info("judal %s → %d 종목 (코드 추출: %d)",
             direction, len(rows), sum(1 for r in rows if r.get("stock_code")))
    return rows


def fetch_judal_both() -> dict:
    """
    매수 + 매도 한 번에.
    return: {key: judal_row} — key 는 stock_code (있으면) 또는 stock_name (없으면).
    """
    out = {}
    for d in ("buy", "sell"):
        for r in fetch_judal(d):
            key = r.get("stock_code") or r.get("stock_name")
            if key:
                out[key] = r
    return out


# ==========================================================================
# 통합 fetcher — 외부 자동 vs CSV 수동 폴백
# ==========================================================================

def fetch_auto(merge_judal: bool = True, merge_toss_prices: bool = True,
                merge_toss_trend: bool = True, merge_dart: bool = True) -> dict:
    """
    자동 fetch 시도. 성공 시:
      {"trade_date": ..., "rows": [...], "source": "todayygg+toss+judal"}
    실패 시 None.

    데이터 소스 머지:
    - todayygg: 활발 종목 발견 (어제 마감 기준)
    - toss trading-trend: 종목별 오늘 장중 매매 (분 단위 갱신) ← 진짜 실시간
    - toss stock-prices/details: 현재가 / 시가총액 / 등락률 (시총비/상승% 계산용)
    - judal: PBR/PER/52주변동률/기대수익률 (보조 가치지표)
    """
    yyg = fetch_todayygg_summary()
    if not yyg:
        return None
    trade_date, rows = todayygg_to_standard_rows(yyg)
    if not rows:
        return None
    sources_used = ["todayygg"]

    # CSV 로 전체 종목 보강 (Top 30 외 종목까지)
    try:
        csv_text = fetch_todayygg_csv()
        if csv_text:
            csv_rows = parse_todayygg_csv(csv_text)
            existing_codes = {r["stock_code"] for r in rows}
            added = 0
            for cr in csv_rows:
                if cr["stock_code"] not in existing_codes:
                    rows.append(cr)
                    added += 1
            log.info("todayygg CSV: +%d 종목 보강 (총 %d)", added, len(rows))
            if added > 0:
                sources_used.append("csv-all")
    except Exception:
        log.exception("todayygg CSV merge failed (계속 진행)")

    # 1) Toss 가격/시총/등락률 머지
    if merge_toss_prices:
        codes = [r["stock_code"] for r in rows if r.get("stock_code")]
        price_map = fetch_toss_stock_prices(codes)
        toss_merged = 0
        for r in rows:
            p = price_map.get(r["stock_code"])
            if not p:
                continue
            # 시가총액: todayygg 값보다 toss 값을 우선 (todayygg는 0인 경우 多)
            if p.get("market_cap"):
                r["market_cap"] = p["market_cap"]
            r["close_price"] = p.get("close", r.get("close_price", 0))
            r["base_price"] = p.get("base", 0)
            r["change_rate"] = p.get("change_rate", 0.0)
            r["volume"] = p.get("volume", 0)
            r["value"] = p.get("value", 0)
            r["high52w"] = p.get("high52w", 0)
            r["low52w"] = p.get("low52w", 0)
            r["trading_strength"] = p.get("trading_strength", 0.0)
            toss_merged += 1
        log.info("toss prices merged: %d/%d", toss_merged, len(rows))
        if toss_merged > 0:
            sources_used.append("toss-prices")

    # 1.5) Toss trading-trend 머지 — 오늘 장중 실시간 매매 데이터로 덮어씀
    # Top 50 종목만 (전 종목 호출 시 빌드 시간 큼)
    intraday_updated_at = ""
    intraday_base_date = ""
    if merge_toss_trend:
        top_for_trend = sorted(
            [r for r in rows if r.get("stock_code")],
            key=lambda r: abs(r.get("net_amount", 0) or 0),
            reverse=True
        )[:50]
        codes = [r["stock_code"] for r in top_for_trend]
        trend_map = fetch_toss_trading_trend(codes)
        today_merged = 0
        for r in rows:
            t = trend_map.get(r["stock_code"])
            if not t:
                continue
            # 토스 trading-trend가 오늘 데이터를 주면 매매 수치 덮어씀
            if t["is_today"]:
                close = r.get("close_price") or t.get("close") or 0
                # 거래량(주) × 종가 = 거래대금 근사
                r["buy_amount"] = t["pension_buy_qty"] * close
                r["sell_amount"] = t["pension_sell_qty"] * close
                r["net_amount"] = t["pension_net_qty"] * close
                r["buy_qty"] = t["pension_buy_qty"]
                r["sell_qty"] = t["pension_sell_qty"]
                r["net_qty"] = t["pension_net_qty"]
                r["intraday"] = True
                r["intraday_updated_at"] = t["updated_at"]
                # net_to_cap 재계산
                mc = r.get("market_cap", 0)
                r["net_to_cap"] = (r["net_amount"] / mc * 100) if mc > 0 else 0.0
                if not intraday_updated_at or t["updated_at"] > intraday_updated_at:
                    intraday_updated_at = t["updated_at"]
                intraday_base_date = t["base_date"]
                today_merged += 1
        log.info("toss trading-trend today data merged: %d/%d", today_merged, len(rows))
        if today_merged > 0:
            sources_used.append("toss-realtime")

    # 1.7) DART 공시 머지 (호재/악재 키워드 점수) — Top |net| 30 종목만 (비용 절약)
    if merge_dart:
        try:
            from report import dart
            # 순매수 절대값 큰 종목 30개만 DART 호출 (빌드 시간 절약)
            top_for_dart = sorted(
                [r for r in rows if r.get("stock_code")],
                key=lambda r: abs(r.get("net_amount", 0) or 0),
                reverse=True
            )[:30]
            codes = [r["stock_code"] for r in top_for_dart]
            dart_map = dart.fetch_dart_scores(codes, days_back=7)
            dart_merged = 0
            for r in rows:
                d = dart_map.get(r["stock_code"])
                if d and (d["score"] != 0 or d["count"] > 0):
                    r["dart_score"] = d["score"]
                    r["dart_matched"] = d["matched"]
                    r["dart_count"] = d["count"]
                    dart_merged += 1
            log.info("dart merged: %d/%d (with disclosures)", dart_merged, len(rows))
            if dart_merged > 0:
                sources_used.append("dart")
        except Exception:
            log.exception("dart merge failed (계속 진행)")

    # 2) judal 가치지표 머지 (sector 는 채우지 않음 — judal theme 은 구분자 없는 긴 문자열이라 dirty)
    if merge_judal:
        judal_map = fetch_judal_both()
        judal_by_name = {}
        for jr in judal_map.values():
            n = jr.get("stock_name")
            if n:
                judal_by_name[n] = jr
        merged = 0
        for r in rows:
            jr = judal_map.get(r.get("stock_code")) or judal_by_name.get(r.get("stock_name"))
            if jr:
                r["pbr"] = jr.get("pbr")
                r["per"] = jr.get("per")
                r["eps"] = jr.get("eps")
                r["change_pct_52w"] = jr.get("change_pct_52w")
                r["change_pct_3y"] = jr.get("change_pct_3y")
                r["expected_return"] = jr.get("expected_return")
                r["theme"] = jr.get("theme") or r.get("theme", "")
                merged += 1
        log.info("judal merged: %d/%d", merged, len(rows))
        if merged > 0:
            sources_used.append("judal")

    # 3) sector 정리 — 키워드 매핑 우선 (CSV sector 가 dirty/잘못된 경우 덮어씀)
    auto_classified = 0
    overridden = 0
    for r in rows:
        sec = (r.get("sector") or "").strip()
        keyword_sec = auto_classify_sector(r.get("stock_code", ""), r.get("stock_name", ""))
        if keyword_sec:
            if sec != keyword_sec:
                if sec:
                    overridden += 1
                else:
                    auto_classified += 1
                r["sector"] = keyword_sec
        elif not _is_clean_sector(sec):
            # 키워드 매핑 실패 + CSV sector 도 dirty
            r["sector"] = ""   # 빈 값으로 → 다음 단계 네이버 fetch 대상
    log.info("sector 키워드 매핑: %d 신규, %d 덮어씀", auto_classified, overridden)

    # 4) 우선주 → 본주 sector 상속
    fill_sector_for_preferred(rows)

    # 5) 여전히 빈 sector 종목 → 네이버 금융에서 보충 (캐시 + 한도 100건/빌드)
    missing_codes = [r["stock_code"] for r in rows
                      if not r.get("sector") and r.get("stock_code", "").isdigit()
                      and len(r.get("stock_code", "")) == 6]
    if missing_codes:
        try:
            import os as _os
            cache_path = _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                "data", "sector_cache.json"
            )
            naver_map = fetch_naver_sectors_bulk(
                missing_codes, cache_path=cache_path, max_new_fetches=30,
            )
            naver_filled = 0
            for r in rows:
                if not r.get("sector") and r["stock_code"] in naver_map:
                    r["sector"] = naver_map[r["stock_code"]]
                    naver_filled += 1
            log.info("네이버 sector 보충: %d 종목 채움", naver_filled)
        except Exception as e:
            log.warning("네이버 sector 보충 실패: %s", e)

    # 최종 sector 채워진 비율
    final_filled = sum(1 for r in rows if r.get("sector"))
    log.info("sector 최종: %d/%d (%.0f%%)",
             final_filled, len(rows), 100 * final_filled / max(len(rows), 1))

    # trade_date 결정: 토스 trading-trend의 base_date(오늘 데이터)가 있으면 그것 우선
    final_trade_date = intraday_base_date.replace("-", "") if intraday_base_date else trade_date

    return {
        "trade_date": final_trade_date,
        "rows": rows,
        "source": "+".join(sources_used),
        "intraday_updated_at": intraday_updated_at,
        "intraday": bool(intraday_updated_at),
    }
