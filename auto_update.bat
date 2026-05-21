@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM Hourly auto build + publish (called by Windows Task Scheduler)
REM   1. Always build realtime mode
REM   2. After 16:00 KST, also build closing mode
REM   3. Push to GitHub Pages via publish.bat
REM ============================================================
chcp 65001 > nul
pushd "%~dp0"
call .venv\Scripts\activate.bat

echo. >> logs\auto_update.log
echo [%date% %time%] auto_update start >> logs\auto_update.log

REM 1) realtime build (every run)
python -m report.generate --mode realtime >> logs\auto_update.log 2>&1
set RT_RESULT=%errorlevel%

REM 2) extract current hour (HH), pad leading space to 0
set HOUR=%time:~0,2%
set HOUR=%HOUR: =0%

REM 3) after 16:00, also build closing
set CL_RESULT=0
if %HOUR% GEQ 16 (
    echo [%date% %time%] closing build trigger hour=%HOUR% >> logs\auto_update.log
    python -m report.generate --mode closing >> logs\auto_update.log 2>&1
    set CL_RESULT=!errorlevel!
)

echo [%date% %time%] auto_update done realtime=%RT_RESULT% closing=!CL_RESULT! >> logs\auto_update.log

REM 4) GitHub Pages publish
call publish.bat >> logs\auto_update.log 2>&1
set PUB_RESULT=!errorlevel!
echo [%date% %time%] publish done exit=!PUB_RESULT! >> logs\auto_update.log

popd
exit /b %RT_RESULT%
