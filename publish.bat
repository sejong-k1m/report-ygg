@echo off
REM ============================================================
REM GitHub Pages auto deploy
REM   Copy report\output\* to docs\ + git add/commit/push
REM ============================================================
chcp 65001 > nul
pushd "%~dp0"

if not exist "docs" mkdir docs
xcopy /Y /Q /S "report\output\*" "docs\" > nul

git add docs >> logs\publish.log 2>&1
git diff --cached --quiet
if errorlevel 1 (
    echo [%date% %time%] commit + push >> logs\publish.log
    git commit -m "auto-update %date% %time%" >> logs\publish.log 2>&1
    git push origin main >> logs\publish.log 2>&1
    if errorlevel 1 (
        echo [%date% %time%] PUSH FAILED - check auth or network >> logs\publish.log
        popd
        exit /b 1
    )
    echo [%date% %time%] push complete >> logs\publish.log
) else (
    echo [%date% %time%] no changes - skip push >> logs\publish.log
)

popd
exit /b 0
