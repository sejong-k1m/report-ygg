# ============================================================
# Windows Task Scheduler registration - hourly auto build
#
# Usage:
#   PowerShell:
#     Set-ExecutionPolicy -Scope Process Bypass
#     .\install_scheduler.ps1
#
# Two modes:
#   1. 24h hourly (default)  - simplest, always fresh
#   2. Market hours only (09-16) - set $marketHoursOnly = $true
# ============================================================

$marketHoursOnly = $false   # set $true for 09:00-16:00 only (8 times/day)

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BatPath = Join-Path $ScriptRoot "auto_update.bat"
$TaskName = "PensionReportAutoUpdate"

if ($marketHoursOnly) {
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date "09:00") `
        -RepetitionInterval (New-TimeSpan -Hours 1) `
        -RepetitionDuration (New-TimeSpan -Hours 8)
    $modeLabel = "market hours 09-16"
} else {
    # 9999 days = ~27 years (Windows scheduler can't accept TimeSpan.MaxValue)
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date "00:00") `
        -RepetitionInterval (New-TimeSpan -Hours 1) `
        -RepetitionDuration (New-TimeSpan -Days 9999)
    $modeLabel = "24h hourly"
}

$action = New-ScheduledTaskAction -Execute $BatPath -WorkingDirectory $ScriptRoot
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Trigger $trigger -Action $action `
    -Principal $principal -Settings $settings `
    -Description "Pension report auto build + GitHub Pages publish ($modeLabel)"

Write-Host ""
Write-Host "===========================================" -ForegroundColor Green
Write-Host "  Task registered: $TaskName"
Write-Host "  Mode: $modeLabel"
Write-Host "===========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Check:   Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "Run now: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove:  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Write-Host ""
Write-Host "Log: logs\auto_update.log + logs\publish.log"
