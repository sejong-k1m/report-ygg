@echo off
REM ===========================================================
REM 수동 리포트 빌드 (기본: realtime 모드)
REM
REM 사용:
REM   build_report.bat                    → realtime 빌드 + 브라우저 오픈
REM   build_report.bat 20260521           → 특정 날짜
REM   build_report.bat --mode closing     → 마감 모드
REM   build_report.bat --mode closing 20260521
REM ===========================================================
chcp 65001 > nul
pushd "%~dp0"
call .venv\Scripts\activate.bat
python -m report.generate %*
if errorlevel 1 (
    echo.
    echo [ERROR] report build failed
    popd
    pause
    exit /b 1
)
echo.
echo [OK] opening report\output\index.html
start "" "report\output\index.html"
popd
