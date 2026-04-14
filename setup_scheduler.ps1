<# 
HKJC Race Analysis — Windows Task Scheduler Setup
====================================================
Creates two scheduled tasks:
  1. "HKJC Wednesday Meeting" — runs Monday at 09:30 (scrape + analyse Wed racecard)
  2. "HKJC Sunday Meeting"    — runs Thursday at 09:30 (scrape + analyse Sun racecard)

Run this script ONCE as Administrator:
    powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1

To remove the tasks later:
    Unregister-ScheduledTask -TaskName "HKJC Wednesday Meeting" -Confirm:$false
    Unregister-ScheduledTask -TaskName "HKJC Sunday Meeting" -Confirm:$false
#>

$ErrorActionPreference = "Stop"

# ── Configuration ─────────────────────────────────────────────────────────────
$Python   = "C:\Users\tbhbr\miniconda3\python.exe"
$WorkDir  = "c:\Users\tbhbr\OneDrive\Desktop\python\HK races anaylsis, April onwards"
$Script   = Join-Path $WorkDir "run_meeting.py"
$LogDir   = Join-Path $WorkDir "logs"

# Create log directory
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# ── Helper: compute next race date from trigger day ───────────────────────────
# Monday trigger → Wednesday race (offset +2)
# Thursday trigger → Sunday race (offset +3)

function New-HkjcTask {
    param(
        [string]$TaskName,
        [string]$TriggerDay,    # "Monday" or "Thursday"
        [int]$DayOffset,        # days from trigger to race day
        [string]$Description
    )

    # The batch wrapper computes the race date dynamically at runtime
    $BatchFile = Join-Path $WorkDir "scheduled_$($TriggerDay.ToLower()).bat"
    
    $BatchContent = @"
@echo off
REM HKJC Scheduled Task — $TaskName
REM Triggered on $TriggerDay, analyses race day ($TriggerDay + $DayOffset days)

cd /d "$WorkDir"
set PYTHONIOENCODING=utf-8

REM Compute race date (trigger day + $DayOffset)
for /f "tokens=*" %%d in ('"$Python" -c "from datetime import datetime,timedelta;print((datetime.now()+timedelta(days=$DayOffset)).strftime('%%Y-%%m-%%d'))"') do set RACEDATE=%%d

echo ============================================
echo HKJC Scheduled Run: %date% %time%
echo Race date: %RACEDATE%
echo ============================================

"$Python" "$Script" --date %RACEDATE% >> "$LogDir\run_%RACEDATE%.log" 2>&1

if %ERRORLEVEL% EQU 0 (
    echo SUCCESS: Pipeline completed for %RACEDATE%
) else (
    echo ERROR: Pipeline failed for %RACEDATE% with code %ERRORLEVEL%
)

echo Completed at %date% %time%
"@

    Set-Content -Path $BatchFile -Value $BatchContent -Encoding ASCII
    Write-Host "  Created batch wrapper: $BatchFile" -ForegroundColor Cyan

    # Create the scheduled task
    $Action  = New-ScheduledTaskAction `
        -Execute "cmd.exe" `
        -Argument "/c `"$BatchFile`"" `
        -WorkingDirectory $WorkDir

    $Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $TriggerDay -At "09:30"

    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
        -RestartCount 2 `
        -RestartInterval (New-TimeSpan -Minutes 10)

    # Register (replace if exists)
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "  Removed old task: $TaskName" -ForegroundColor Yellow
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Description $Description `
        -RunLevel Highest | Out-Null

    Write-Host "  Registered: $TaskName ($TriggerDay at 09:30)" -ForegroundColor Green
}

# ── Create tasks ──────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "HKJC Race Analysis — Task Scheduler Setup" -ForegroundColor White
Write-Host "==========================================" -ForegroundColor White
Write-Host ""

Write-Host "[1/2] Wednesday Meeting (trigger: Monday)" -ForegroundColor White
New-HkjcTask `
    -TaskName "HKJC Wednesday Meeting" `
    -TriggerDay "Monday" `
    -DayOffset 2 `
    -Description "Scrape HKJC race card and run analysis for Wednesday meeting (triggered Monday 07:00)"

Write-Host ""
Write-Host "[2/2] Sunday Meeting (trigger: Thursday)" -ForegroundColor White
New-HkjcTask `
    -TaskName "HKJC Sunday Meeting" `
    -TriggerDay "Thursday" `
    -DayOffset 3 `
    -Description "Scrape HKJC race card and run analysis for Sunday meeting (triggered Thursday 07:00)"

Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Green
Write-Host "  Tasks will run automatically on Monday (for Wed) and Thursday (for Sun) at 09:30." -ForegroundColor Cyan
Write-Host "  Logs saved to: $LogDir" -ForegroundColor Cyan
Write-Host ""
Write-Host "To verify:" -ForegroundColor Yellow
Write-Host "  Get-ScheduledTask -TaskName 'HKJC*' | Format-Table TaskName, State, Description" -ForegroundColor Yellow
Write-Host ""
Write-Host "To remove:" -ForegroundColor Yellow
$tWed = "HKJC Wednesday Meeting"
$tSun = "HKJC Sunday Meeting"
Write-Host "  Unregister-ScheduledTask -TaskName '$tWed' -Confirm:`$false" -ForegroundColor Yellow
Write-Host "  Unregister-ScheduledTask -TaskName '$tSun' -Confirm:`$false" -ForegroundColor Yellow
