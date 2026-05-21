# 연기금 자동매매 봇 - 셋업 가이드

## ⚠️ 환경 요구사항 (무조건)

| 항목 | 사양 |
|---|---|
| OS | **Windows 10 / 11** (필수, 키움 OCX는 Windows 전용) |
| Python | **3.8 또는 3.9 의 32-bit 버전** (64-bit 절대 X) |
| 키움 계좌 | 본인 명의 키움증권 계좌 (실계좌) |
| 키움 OpenAPI+ | 영웅문에서 신청 후 설치 |

---

## 1. 키움증권 사전 작업

### 1-0. 모의투자 신청 (먼저)
처음에는 **모의투자로 며칠 검증** 후 실계좌로 전환합니다.

1. 영웅문 → **트레이딩 → 모의투자 → 모의투자 신청**
2. 신청 즉시 승인, 가상자금 1억 지급
3. **모의투자 전용 계좌번호** 가 별도 부여됨 → 이 번호를 `app_secrets.py` 에 사용
4. 영웅문 → 모의투자 → 모의투자 정보에서 계좌번호 확인

> 실계좌 전환 시: `config.py` 의 `KIWOOM_MOCK_TRADING = False` 로 변경 + `app_secrets.py` 의 `KIWOOM_ACCOUNT` 를 실계좌 번호로 교체

### 1-1. OpenAPI 사용 신청
1. 영웅문(HTS) 로그인
2. 메뉴 → **상품·서비스 → OpenAPI 사용신청**
3. 약관 동의 후 신청 (즉시 승인됨)

### 1-2. OpenAPI+ 모듈 다운로드/설치
1. https://www3.kiwoom.com → 트레이딩채널 → OpenAPI 메뉴
2. **OpenAPI+ 모듈** 다운로드 → 설치
3. 설치 후 시작메뉴에 "KOA Studio" 와 "OpenAPI" 폴더 생김

### 1-3. 버전처리
- 설치 후 **반드시 1회 KOA Studio 또는 OpenAPI 로그인 실행** → 자동 버전업 진행
- "버전처리" 팝업이 뜨면 **확인** 누르고 끝까지 완료

### 1-4. 테스트
- KOA Studio 실행 → 로그인 → 잘 들어가지면 OK

---

## 2. Python 32-bit 환경 만들기

### 옵션 A. python.org 에서 직접 설치 (권장)
1. https://www.python.org/downloads/windows/ 에서 **Python 3.9.13 Windows installer (32-bit)** 다운로드
2. 설치 시 "Add Python to PATH" 체크
3. 다른 경로에 설치 (예: `C:\Python39-32`) — 64bit 와 충돌 방지

### 옵션 B. py launcher 활용
이미 py launcher(`py`)가 깔려있다면:
```
py -3.9-32 --version
```
이게 작동하면 OK. 안 되면 옵션 A로 32bit 설치.

### 가상환경 생성 + 패키지 설치
```cmd
cd "E:\1.총무\★개인\연기금-자동매매"
py -3.9-32 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

설치 확인:
```cmd
python -c "import sys; print(sys.maxsize > 2**32)"
```
→ `False` 가 출력되어야 32-bit 입니다. `True` 면 64-bit 잘못 설치된 것.

---

## 3. 알림 채널 셋업

알림은 **카카오톡(주) + 디스코드(보조)** 두 채널로 동시 발송됩니다.
하나가 죽어도 다른 쪽으로 알림이 옵니다.

### 3-A. 디스코드 웹훅 4개 만들기

1. 디스코드 서버 하나 생성 (혹은 기존 서버 사용)
2. 채널 4개 생성:
   - `#연기금-신규매수`
   - `#연일매수`
   - `#보유종목-경고`
   - `#체결로그`
3. 각 채널에서: 채널설정 → **연동 → 웹훅 → 새 웹훅 만들기 → URL 복사**
4. 4개 URL을 `app_secrets.py` 에 붙여넣기

### 3-B. 카카오 디벨로퍼스 앱 등록

카톡 "나에게 메시지 보내기" 자동화를 위한 1회 셋업입니다.

1. **https://developers.kakao.com 가입 + 로그인** (본인 카카오 계정으로)
2. **내 애플리케이션 → 애플리케이션 추가하기**
   - 앱 이름: 아무거나 (예: "연기금 봇")
   - 회사명: 본인이름 가능
3. 생성된 앱 클릭 → **앱 설정 → 플랫폼**
   - **Web 플랫폼 등록** → 사이트 도메인: `http://localhost:8080`
4. **제품 설정 → 카카오 로그인** → 활성화 ON
   - **Redirect URI 등록**: `http://localhost:8080`
5. **카카오 로그인 → 동의항목**
   - **카카오톡 메시지 전송** → 상태 "사용함" + 동의단계 "선택 동의"
6. **앱 설정 → 앱 키** → **REST API 키** 복사 (32자리)
   - 이 키를 `app_secrets.py` 의 `KAKAO_REST_API_KEY` 에 붙여넣기

### 3-C. 카카오 첫 토큰 발급 (1회)

```cmd
cd "E:\1.총무\★개인\연기금-자동매매"
.venv\Scripts\activate
python tools\kakao_auth.py
```

진행:
1. 브라우저가 자동으로 열림 → 카카오 로그인
2. "카카오톡 메시지 전송" 동의 체크 → **동의하고 계속하기**
3. localhost:8080 으로 리다이렉트 → "✅ 카카오 인증 완료" 페이지
4. 터미널로 돌아오면 **테스트 메시지가 본인 카톡 "나에게 보내기"에 도착**
5. `data\kakao_tokens.json` 파일 생성 (gitignore 됨)

> ⚠️ refresh_token 은 **60일 후 만료** 됩니다. 만료 7일 전부터 봇 로그에 경고가 뜨고 카톡으로도 알림이 옵니다. 만료 전에 `python tools\kakao_auth.py` 를 다시 실행하면 됩니다 (1분 작업).

---

## 4. app_secrets.py 작성

```cmd
copy app_secrets.py.example app_secrets.py
notepad app_secrets.py
```

> 파일명이 `secrets.py` 가 아닌 `app_secrets.py` 인 것에 주의 (Python 표준 `secrets` 모듈과 충돌 방지).

`app_secrets.py` 의 모든 `...` 자리를 실제 값으로 교체:
- `KIWOOM_ACCOUNT` : 키움 모의투자 계좌번호 10자리
- 디스코드 웹훅 4개 URL
- `KAKAO_REST_API_KEY` : 카카오 디벨로퍼스 앱의 REST API 키

---

## 5. 첫 실행 (반드시 장외시간에)

처음에는 **장외시간(오후 4시 이후 또는 주말)**에 실행해서 로그인/연결만 검증하세요.
실제 거래 트리거는 안 발동하고 GUI/디스코드만 뜹니다.

```cmd
cd "E:\1.총무\★개인\연기금-자동매매"
.venv\Scripts\activate
python main.py
```

순서대로 확인:
1. ✅ 키움 로그인 창 뜸
   - **⚠️ 로그인 창 하단의 "모의투자접속" 체크박스 ON** ← 모의투자 모드
   - 모의투자 ID/비밀번호 입력
2. ✅ GUI 메인 윈도우 뜸 (3개 탭, 상단에 보라색 "🧪 모의투자" 뱃지)
3. ✅ 디스코드 #체결로그 채널에 "🧪 연기금 봇 시작 (모의투자 모드)" 메시지 도착
4. ✅ 카톡 "나에게 보내기" 에도 동일 메시지 도착
5. ✅ "보유종목 N건 동기화 완료" 로그 표시 (모의투자 잔고)

### 모의투자 며칠 운영 시 체크포인트
- [ ] 70억+ 매수 종목이 #연기금-신규매수 채널에 뜨는지 (실시간 데이터 검증)
- [ ] 보유종목(모의 매수해놓은 것)에 연기금 매도 발생 시 #보유종목-경고 알림 오는지
- [ ] 40억+ 매도 발생 시 자동매도 주문 정상 실행 + 가상 체결되는지
- [ ] -0.7% 가격 계산이 정확한지 (체결가 vs 의도가 비교)
- [ ] 1차 미체결 + 추가하락 시 -2.5% 페일세이프 발동하는지
- [ ] GUI 킬스위치 정상 작동하는지
- [ ] 한도(1회/종목당/전체) 초과 시 차단되는지

위 7개 모두 확인되면 실계좌 전환:
1. `config.py` → `KIWOOM_MOCK_TRADING = False`
2. `app_secrets.py` → `KIWOOM_ACCOUNT` 를 실계좌 번호로
3. 영웅문 로그인 시 "모의투자접속" 체크 **해제**

### ⚠️ OPT10059 데이터 점검 (모의투자 첫 실행 시 필수)
모의투자 환경에서 OPT10059 가 실데이터를 안 줄 수도 있습니다. 첫 실행 후 확인:
- 30분 정도 돌려본 후 #연기금-신규매수 채널에 종목이 하나도 안 뜨거나
- GUI 로그에 `pension_buy=0 sell=0` 만 반복되면

→ OPT10059 가 모의에서 작동 안 함. 이 경우:
1. `config.py` → `KIWOOM_MOCK_TRADING = False` + `DRY_RUN = True`
2. 영웅문 로그인 시 "모의투자접속" 체크 **해제** (실계좌 로그인)
3. 실데이터 받되 주문은 시뮬레이션만 → 며칠 검증 후 `DRY_RUN = False`

---

## 6. config.py 한도 조정

운영하면서 한도 상향이 필요해지면 `config.py` 상단 값만 수정:

```python
MAX_SELL_PER_ORDER = 10_000_000           # 1회 1천만 → 변경 가능
MAX_SELL_PER_STOCK_DAILY = 20_000_000     # 종목당 일일 2천만
MAX_SELL_TOTAL_DAILY = 50_000_000         # 일일 전체 5천만
```

다른 자주 만지는 값:
- `THRESHOLD_NEW_BUY` : 70억 (신규매수 알림 기준)
- `THRESHOLD_HOLD_WARN` : 20억 (보유종목 경고)
- `THRESHOLD_HOLD_AUTO_SELL` : 40억 (자동매도)
- `ALERT_ONLY_UNTIL` : "10:30" (이전엔 알림만)
- `DRY_RUN` : `True`로 바꾸면 주문 안 나감 (테스트용)

---

## 7. 킬스위치

운영 중 즉시 정지가 필요하면:
- **GUI 우측 상단 🛑 버튼** 클릭
- 또는 **`Ctrl + Shift + K`** 단축키
- 또는 프로젝트 루트에 `KILLSWITCH.lock` 파일을 직접 생성 (어느 위치에서든 즉시 정지)

해제는 GUI 의 "정지 해제" 버튼 (사용자 명시 액션 필요).

---

## 7a. 일일 리포트 사이트 (todayygg 스타일)

장 마감 후(보통 16:10) 또는 18:10 이후에 KRX 확정치 받아서 연기금 매매 리포트 HTML을 생성합니다.

```cmd
build_report.bat            # 직전 영업일 데이터로 리포트 빌드 + 브라우저 자동 오픈
build_report.bat 20241230   # 특정 일자
```

출력:
- `report\output\index.html` — 메인 리포트 페이지
- `report\output\summary_latest.json` — 요약 JSON
- `report\output\pension_latest.csv` — 전 종목 CSV

리포트 구성:
- 오늘 연기금 매수/매도 총합
- 최근 7거래일 일별 추이
- 연기금 순매수 Top 30
- 연기금 순매도 Top 30
- 시총 대비 순매수/순매도 Top 30 (소형주에서 큰 비중 자금 유입 감지)

**일일 자동 실행 (Windows 작업 스케줄러):**
1. 검색 → "작업 스케줄러" 실행
2. 작업 만들기 → 트리거: 매일 16:30 (장 마감 후)
3. 동작: 프로그램 시작 → `E:\1.총무\★개인\연기금-자동매매\build_report.bat`
4. (선택) "사용자가 로그온할 때만 실행" 체크

---

## 8a. KRX 직접 조회 (연기금 매매 종목 발견 — 권장 채널)

봇은 **KRX 정보데이터시스템(data.krx.co.kr)을 직접 호출**해 연기금이 매매하는 종목을 찾습니다. Kiwoom OPT10059는 종목별 호출이라 비효율적이고, 모의투자 환경에선 빈 응답을 주는 문제도 있어서 **KRX 채널이 주된 발견 경로**입니다.

```
[KRX 정보데이터시스템] ← pykrx 5분마다 호출
       ↓ 연기금 순매수 TOP 리스트 (전 종목 랭킹, 한 번에)
[10억+ 매수/매도 종목 자동 발견]
       ↓
[watchlist 자동 추가 + HOT 승격]
       ↓
[기존 룰엔진 평가 → 60억+ 자동매수 / 40억+ 자동매도]
```

**config.py 설정:**
- `KRX_DISCOVERY_ENABLED = True` (기본)
- `KRX_DISCOVERY_INTERVAL_SEC = 300` (5분 주기)
- `KRX_DISCOVERY_MIN_BUY = 1_000_000_000` (10억+ 매수 발생 시 발견)
- `KRX_DISCOVERY_MIN_SELL = 1_000_000_000` (10억+ 매도 발생 시 발견)

**장점:**
- Kiwoom TR 한도와 완전 무관 (별도 채널)
- 한 번 호출 = 전 종목 랭킹 → 발견 효율 압도적
- 무료, 인증 불필요
- **모의투자 환경에서도 작동** (KRX는 실데이터)

**한계:**
- KRX 잠정치 갱신 주기에 종속 (수 분 단위)
- pykrx 라이브러리가 KRX HTML 포맷 바뀌면 깨질 수 있음 (정기 업데이트 필요)

---

## 8b. 시장 유니버스 (Kiwoom OPT10059 정밀 폴링용)

기본값으로 봇은 **KOSPI 전 종목(~900개)을 자동으로 추적**합니다. watchlist.txt 의 수동 등록은 우선 폴링용으로 그대로 두고, 거기에 KOSPI 전체를 합칩니다.

`config.py` 의 `UNIVERSE_MODE` 로 변경:
- `"watchlist"` : 기존 동작 (watchlist.txt + 보유종목만, ~50종목)
- `"kospi"`    : KOSPI 전체 추적 ← **권장 (기본값)**
- `"kosdaq"`   : KOSDAQ 전체 (~1500)
- `"kospi+kosdaq"` : 둘 다 (~2500, 풀사이클 ~2.8시간 — 너무 느림)

기본 폴링: 60초마다 15종목씩 라운드로빈 → 시간당 ~900건 (키움 한도 1000건/시간 안에서)
- KOSPI 풀사이클: ~60분 (모든 종목이 1시간에 한 번 검사됨)
- 60억+ 연기금 매수 발견 시 → 자동매수 (`AUTO_BUY_ENABLED=True`)
- 40억+ 연기금 매도(보유) 발견 시 → 자동매도
- 20억+ 연기금 매도(보유) 발견 시 → 카톡/디스코드 경고만

ETF/ETN/스팩/리츠/우선주는 기본 제외 (`UNIVERSE_EXCLUDE_ETF=True`).

---

## 9. 알려진 이슈 / TODO

- **OPT10059 컬럼명 검증 필요**: `core/kiwoom_client.py` 의 `_parse_opt10059` 컬럼명("연기금등", "외국인투자자" 등)은 KOA Studio 에서 실제 응답으로 검증해야 정확. 첫 실행 시 일부 데이터 0으로 나오면 KOA Studio 의 "TR목록 → OPT10059" 응답 구조 확인 후 컬럼명 수정.
- **장 마감 후 확정치 보정**: `is_confirmed=True` 데이터 적재 로직 미구현. 다음 단계에서 KRX 또는 키움 일자별 확정치 TR로 보정 추가.
- **모의투자 환경 OPT10059 데이터 미제공**: 모의투자에선 OPT10059 가 빈 응답을 줄 가능성 큼. 실데이터 검증은 `KIWOOM_MOCK_TRADING=False` + `DRY_RUN=True` 모드로 실계좌 로그인해서 진행.

---

## 폴더 구조

```
연기금-자동매매/
├── main.py                  # 진입점
├── config.py                # 임계값/한도/시간룰
├── app_secrets.py           # 계좌 + 웹훅 + 카카오 키 (gitignore)
├── app_secrets.py.example   # 템플릿
├── requirements.txt
├── SETUP.md                 # 이 파일
├── core/
│   ├── kiwoom_client.py     # OpenAPI+ COM 래퍼
│   ├── pension_tracker.py   # 데이터 폴링 + 룰 평가
│   ├── order_manager.py     # 주문 + 미체결 관리
│   ├── rule_engine.py       # 70억/20억/40억/연일매수 판정
│   └── safety.py            # 한도/멱등/킬스위치/시간가드
├── notify/
│   ├── __init__.py          # 디스패처 (카톡 + 디스코드 동시 발송)
│   ├── discord.py           # 디스코드 4채널 웹훅
│   └── kakao.py             # 카카오 talk/memo + 토큰 자동갱신
├── tools/
│   └── kakao_auth.py        # 카카오 OAuth 첫 토큰 발급 + 60일마다 재발급
├── storage/
│   ├── db.py                # SQLite 래퍼
│   └── schema.sql
├── gui/
│   └── main_window.py       # PyQt5 GUI
├── data/
│   ├── pension_bot.db       # SQLite (자동 생성)
│   └── kakao_tokens.json    # 카카오 토큰 (자동 생성, gitignore)
└── logs/                    # 일자별 로그 (자동 생성)
```
