@echo off
REM ============================================================
REM Manual report build (default: realtime mode)
REM Usage:
REM   build_report.bat
REM   build_report.bat 20260521
REM   build_report.bat --mode closing
REM ============================================================
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
