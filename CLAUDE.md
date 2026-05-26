# REPORT-YGG 프로젝트 메모 (Claude 컨텍스트)

이 파일은 Claude Code가 새 세션 시작 시 자동으로 읽는 프로젝트 메모입니다.
프로젝트 전체 흐름, 외부 의존성, 미해결 이슈를 한 곳에 정리.

---

## 프로젝트 개요

**REPORT-YGG**: 연기금(국민연금) 한국 주식 매매 추적 + 일일 리포트 사이트
- 라이브 URL: https://sejong-k1m.github.io/report-ygg/
- 저장소: https://github.com/sejong-k1m/report-ygg
- 운영자: sejong-k1m (개인 PC, 회사 보안 제약 다수)

원래는 키움 OpenAPI+ 기반 자동매매 봇 프로젝트였으나, 회사 GP/KRX anti-bot 등 다중 제약으로
지금은 **리포트 사이트 위주**로 진화. 봇 코드는 그대로 있으나 비활성.

---

## 데이터 소스 (KRX 직접 접근 차단됨)

| 출처 | 용도 | 상태 |
|---|---|---|
| **todayygg.com** | 연기금 매매 (전 종목, JSON+CSV 공개) | ✅ 핵심 |
| **toss trading-trend** | 종목별 장중 실시간 매매 (분 단위) | ✅ 핵심 |
| **toss stock-prices/details** | 현재가/시총/등락률 | ✅ |
| **judal.co.kr** | PBR/PER/52주변동률 (HTML 스크래핑) | ⚠ 머지 0/60 — HTML 구조 파싱 실패 |
| **DART OpenAPI** | 공시 (호재/악재 키워드) | ✅ API key=GitHub Secrets `DART_API_KEY` |
| **KRX 직접** | — | ❌ anti-bot으로 차단됨. 브라우저 자동화도 회사 GP가 막음 |

### 시도해봤지만 실패한 KRX 우회
- plain requests → LOGOUT
- curl-cffi (Chrome TLS) → LOGOUT
- OTP CSV → LOGOUT
- Playwright/Selenium → 회사 Group Policy로 chromium/Edge/Chrome 모두 차단

---

## 자동화 인프라

```
cron-job.org (15분마다, 외부 안전망)
  ↓ webhook (GitHub PAT)
GitHub Actions schedule */5 (5분마다 자체)
  ↓
.github/workflows/build.yml
  ↓ todayygg + toss(trend+prices) + judal + DART fetch
  ↓ realtime.html + index.html + closing.html + ai.html
  ↓ docs/ 푸시
GitHub Pages 자동 배포
  ↓
https://sejong-k1m.github.io/report-ygg/
```

- **로컬 Windows 스케줄러는 삭제됨** (충돌 방지). GitHub Actions만 source of truth.
- 회사 PC 꺼져도 24/7 자동.
- 빌드 타임아웃: 20분.

---

## 외부 서비스 인증

### GitHub Secrets (Actions에서 사용)
- `DART_API_KEY` — DART OpenAPI 키 (40자)

### Firebase (커뮤니티 게시판)
- 프로젝트: `report-ygg-d53f1`
- Firestore Database (asia-northeast3)
- Authentication: 익명 활성화
- Rules: 본인 uid 글만 수정/삭제
- firebaseConfig는 generate.py 안에 박혀있음 (apiKey는 공개돼도 안전, Rules로 실제 보안)

### cron-job.org
- 매 15분 GitHub workflow_dispatch trigger
- Headers: Authorization Bearer ghp_... (Personal Access Token, workflow scope)

### Personal Access Token
- GitHub PAT (workflow scope) — cron-job.org webhook용
- 만료 없음

---

## 페이지 구성

**3개 모드**: realtime / closing / ai

### 실시간 (index.html / realtime.html)
- 5분마다 자동 갱신 (HTML auto-refresh meta)
- 토스 trading-trend로 장중 누적 매매 데이터 실시간 반영

### 마감 기준 (closing.html)
- trading-trend 머지 X (장중 데이터로 오염 방지)
- todayygg 직전 영업일 15:30 마감 데이터만
- 16시 이후 별도 빌드도 실행

### AI 점수 (ai.html)
- 종목별 점수 분해 표 (15 컬럼)
- 매수 시그널 Top 20 + 매도 시그널 Top 10 카드 (점수 근거 + 추천)

### 섹션 (토글 chip으로 표시/숨김)
1. 오늘 TOP 5 카드 (매수/매도)
2. 오늘 TOP 20 표
3. 매수비율 TOP 30 (= 시총 대비 큰 자금)
4. 주간 누적 TOP 30 (최근 7거래일)
5. 오늘 TOP 50 (기본 OFF)
6. 전체 누적 TOP 30 (REPORT-YGG 시행 이후 영구 합산, 기본 OFF)
7. 전체 종목 (기본 OFF)
8. 💬 커뮤니티 (우측 사이드바, Firebase 익명)

---

## AI 점수 공식

가중합 휴리스틱. **머신러닝 아님.**

```
점수 =
  매수비율(%) × 100
+ 전일거래액 비율 × 5
+ 등락률(%) × 1.5
+ 거래량강도(100기준) × 0.05
+ 활발 매수 기간(일) × 4    (매수 시에만 가산)
+ 누적 순매수(억) × 0.005
+ 전일대비 변화(억) × 0.02
+ DART 공시 점수            (호재/악재 키워드 매칭)
- 연속 매도일수 × 8         (페널티)
```

등급:
- ≥50: ★★★ 강한 매수
- 20~50: ★★ 매수 우세
- 5~20: ★ 약한 매수
- -5~+5: 중립
- -20~-5: ▼ 약한 매도
- -50~-20: ▼▼ 매도 우세
- ≤-50: ▼▼▼ 강한 매도

비용 절약: DART는 net 절대값 Top 30, toss trading-trend는 Top 50 종목만 호출.

---

## 커뮤니티 (Firebase Firestore)

- 익명 인증 (uid 자동)
- 닉네임 입력 → localStorage 저장
- 글/답글 작성 + @종목명 멘션 → 파란 태그
- @멘션 클릭 → 그 종목 글만 필터링
- 본인 글만 수정/삭제 (Firestore Rules로 서버에서도 차단)
- 한글 IME 입력 처리 (composition 이벤트로 깨짐 방지)

---

## 미해결 이슈

1. **judal 머지 0/60** — HTML 파싱 실패 (BS가 데이터 셀을 헤더로 잘못 인식). 종목명+코드 추출 가능하지만 파서 다시 짜야 함.
2. **미국 주식 데이터 없음** — 13F는 분기별이라 일간 데이터 불가. KRX는 미국 데이터 없음.
3. **Phase 2 미완**: 네이버 뉴스 sentiment (작업 안 함)
4. **삼성전자우 같은 우선주** — DART corp_code 매핑 안 될 수 있음 (본주와 공유 여부 미확인)

---

## 자주 쓰는 명령

```cmd
# 수동 빌드 (로컬, 디버그용)
cd /d "E:\1.총무\★개인\연기금-자동매매"
.venv\Scripts\activate
build_report.bat

# Actions 수동 trigger
# https://github.com/sejong-k1m/report-ygg/actions/workflows/build.yml → Run workflow

# Firestore Rules 페이지
# https://console.firebase.google.com/project/report-ygg-d53f1/firestore/rules
```

---

## 잘 알려진 함정

1. **bat 파일 한글 주석 금지** — cmd CP949 인코딩과 충돌. ASCII만.
2. **f-string에 JavaScript 박으면 `{{ }}` escape 필수** — generate.py에서 JS 코드 안의 모든 `{`, `}` 더블링.
3. **로컬 publish.bat 자동 commit 금지** — GitHub Actions가 단일 source of truth. 로컬 스케줄러 삭제됨.
4. **GitHub Actions schedule cron은 신뢰성 낮음** — cron-job.org 외부 ping이 안전망.
5. **conf 1500px 이상 화면**에서만 커뮤니티가 우측 사이드바. 작으면 하단으로 떨어짐.

---

## 코딩 규칙

- generate.py 의 HTML 안에 박힌 JS/CSS는 f-string이라 `{{ }}` 더블링 필요
- bat 파일은 ASCII만 (한글 주석 X). chcp 65001 + pushd "%~dp0" 패턴
- Firestore Rules 변경하면 Firebase 콘솔에서 직접 "게시" 클릭 필요
- 환경변수 (DART_API_KEY 등) 는 GitHub Secrets 통해서만 주입
- `data/pension_report.db` 만 git 추적 (누적 데이터 보존). 다른 db는 .gitignore.

---

## 다음 작업 후보

- [ ] Phase 2: 네이버 뉴스 sentiment 키워드 점수
- [ ] judal HTML 파싱 수정 (PBR/PER 다시 보강)
- [ ] AI 점수 더 정교화 (가중치 조정, 검증)
- [ ] 13F 분기 데이터 (미국 NPS 보유 종목)
- [ ] 종목 검색 기능 (전체 종목 표 안에서 필터)
- [ ] 통계 차트 (Chart.js 등)

---

## 진행 요약 한 줄

KRX/회사 GP 다중 제약 모두 우회 → todayygg+toss+DART로 무인 자동화 + Firebase 익명 커뮤니티 + AI 점수 휴리스틱. 매일 24/7 갱신 운영 중.
