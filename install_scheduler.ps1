# ============================================================
# Windows 작업 스케줄러 등록 — 매시간 자동 빌드
#
# 사용법:
#   PowerShell 관리자 권한 (또는 일반) 으로 실행
#   > Set-ExecutionPolicy -Scope Process Bypass
#   > .\install_scheduler.ps1
#
# 두 가지 모드:
#   1. 24시간 매시간 (기본) — 단순함, 항상 최신
#   2. 장중(09-16시) 매시간 — 외부 사이트 부담 최소화
#      → 아래 $marketHoursOnly 변수를 $true 로 바꾸면 됨
# ============================================================

$marketHoursOnly = $false   # $true 면 09:00~16:00 만 실행 (8회/일)

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BatPath = Join-Path $ScriptRoot "auto_update.bat"
$TaskName = "연기금 일일 리포트"

if ($marketHoursOnly) {
    # 09:00 ~ 16:00 매시간 (8회: 09,10,11,12,13,14,15,16)
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date "09:00") `
        -RepetitionInterval (New-TimeSpan -Hours 1) `
        -RepetitionDuration (New-TimeSpan -Hours 8)
    $modeLabel = "장중 매시간 (09-16시)"
} else {
    # 24시간 매시간 — 가장 단순
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date "00:00") `
        -RepetitionInterval (New-TimeSpan -Hours 1) `
        -RepetitionDuration ([TimeSpan]::MaxValue)
    $modeLabel = "24시간 매시간"
}

$action = New-ScheduledTaskAction -Execute $BatPath -WorkingDirectory $ScriptRoot
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

# 기존 작업 제거 후 재등록
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Trigger $trigger -Action $action `
    -Principal $principal -Settings $settings `
    -Description "연기금 리포트 자동 빌드 ($modeLabel). CSV는 외부 사이트(todayygg/toss/judal)에서 자동 fetch."

Write-Host ""
Write-Host "===========================================" -ForegroundColor Green
Write-Host "  작업 등록 완료: $TaskName"
Write-Host "  모드: $modeLabel"
Write-Host "===========================================" -ForegroundColor Green
Write-Host ""
Write-Host "확인:  Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "수동 실행:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "제거:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Write-Host ""
Write-Host "로그: logs\auto_update.log"
