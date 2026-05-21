@echo off
REM ===========================================================
REM GitHub Pages 자동 배포
REM   output/ → docs/ 복사 + git add/commit/push
REM
REM 호출 시점:
REM   - auto_update.bat 가 매시간 빌드 후 자동 호출
REM   - 수동: publish.bat
REM ===========================================================
chcp 65001 > nul
pushd "%~dp0"

REM docs 폴더 준비
if not exist "docs" mkdir docs

REM output/* → docs/ 복사 (html, json, csv 전부)
xcopy /Y /Q /S "report\output\*" "docs\" > nul

REM 변경사항 있을 때만 commit
git add docs >> logs\publish.log 2>&1
git diff --cached --quiet
if errorlevel 1 (
    echo [%date% %time%] commit + push >> logs\publish.log
    git commit -m "auto-update %date% %time%" >> logs\publish.log 2>&1
    git push origin main >> logs\publish.log 2>&1
    if errorlevel 1 (
        echo [%date% %time%] PUSH FAILED — 인증 또는 네트워크 확인 >> logs\publish.log
        popd
        exit /b 1
    )
    echo [%date% %time%] push 완료 >> logs\publish.log
) else (
    echo [%date% %time%] 변경 없음 — push 생략 >> logs\publish.log
)

popd
exit /b 0
