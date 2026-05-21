@echo off
setlocal enabledelayedexpansion
REM ===========================================================
REM 매시간 자동 실행 — Windows 작업 스케줄러에 등록
REM
REM 동작:
REM   1. 항상 realtime 모드 빌드 (realtime.html + index.html)
REM   2. 현재 시각이 16시 이후면 closing 모드도 빌드 (closing.html)
REM      (todayygg는 16:10에 당일 15:30 스냅샷 게시 → 16시 이후 빌드 권장)
REM ===========================================================
chcp 65001 > nul
pushd "%~dp0"
call .venv\Scripts\activate.bat

echo. >> logs\auto_update.log
echo [%date% %time%] auto_update start >> logs\auto_update.log

REM 1) 실시간 빌드 — 매번
python -m report.generate --mode realtime >> logs\auto_update.log 2>&1
set RT_RESULT=%errorlevel%

REM 2) 시각 확인 (HH 부분 추출, leading space → 0 변환)
set HOUR=%time:~0,2%
set HOUR=%HOUR: =0%

REM 3) 16시 이후면 마감 빌드도
set CL_RESULT=0
if %HOUR% GEQ 16 (
    echo [%date% %time%] closing build trigger ^(hour=%HOUR%^) >> logs\auto_update.log
    python -m report.generate --mode closing >> logs\auto_update.log 2>&1
    set CL_RESULT=!errorlevel!
)

echo [%date% %time%] auto_update done ^(realtime=%RT_RESULT%, closing=%CL_RESULT%^) >> logs\auto_update.log

REM 4) GitHub Pages 자동 배포 (publish.bat)
call publish.bat >> logs\auto_update.log 2>&1
set PUB_RESULT=!errorlevel!
echo [%date% %time%] publish done ^(exit=!PUB_RESULT!^) >> logs\auto_update.log

popd
exit /b %RT_RESULT%
