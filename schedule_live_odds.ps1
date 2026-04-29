# schedule_live_odds.ps1 — Register Windows Task Scheduler jobs that
# capture HKJC live odds on race day at four cadence points and run the
# alert engine right after each capture.
#
# Usage:
#   .\schedule_live_odds.ps1 -Date 2026-04-29 -Venue HV `
#       -Races "1-9" -PostHHmm "1915"
#
# Cadence (relative to first race post-time):
#     T-12h   overnight baseline
#     T-04h   morning reaction
#     T-30m   pre-post smart money + connections
#     T-02m   final
#
# All jobs are one-shot — they delete themselves after running. Re-run this
# script each meeting day to schedule that day's captures.

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)] [string]$Date,
    [Parameter(Mandatory=$true)] [ValidateSet("HV","ST")] [string]$Venue,
    [string]$Races = "1-11",
    [Parameter(Mandatory=$true)] [string]$PostHHmm,
    [string]$Python = "C:\Users\tbhbr\miniconda3\python.exe",
    [string]$Repo = (Split-Path -Parent $MyInvocation.MyCommand.Path),
    [string]$Model = "auto",
    [switch]$DryRun,
    [switch]$RemoveExisting
)

# Build first-race datetime
$postT = [datetime]::ParseExact("$Date $PostHHmm", "yyyy-MM-dd HHmm",
                                [System.Globalization.CultureInfo]::InvariantCulture)
$cadence = @(
    @{ Tag = "overnight"; Offset = -12*60 },
    @{ Tag = "morning";   Offset =  -4*60 },
    @{ Tag = "prepost";   Offset = -30    },
    @{ Tag = "final";     Offset =  -2    }
)

$logRoot = Join-Path $Repo "cache\live_odds\logs"
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null

foreach ($c in $cadence) {
    $when = $postT.AddMinutes($c.Offset)
    $name = "HKJCLiveOdds_$($Date)_$($Venue)_$($c.Tag)"

    if ($RemoveExisting) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false `
            -ErrorAction SilentlyContinue
    }

    if ($when -lt (Get-Date)) {
        Write-Host "  [skip] $name — schedule time $when is in the past" `
            -ForegroundColor DarkGray
        continue
    }

    $logFile = Join-Path $logRoot ("{0}_{1}_{2}.log" -f $Date.Replace("-",""), $Venue, $c.Tag)
    $cmd = ("Set-Location -LiteralPath '{0}'; " -f $Repo) +
           "`$env:PYTHONIOENCODING='utf-8'; " +
           "& '$Python' scrape_hkjc_live_odds.py --date $Date --venue $Venue --races $Races --pools wp,qin,qpl ; " +
           "& '$Python' live_odds_alerts.py --date $Date --venue $Venue --model $Model --no-colour"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument ("-NoProfile -ExecutionPolicy Bypass -Command `"& {{ {0} }} *>&1 | Tee-Object -FilePath '{1}'`"" `
            -f $cmd, $logFile)
    $trigger = New-ScheduledTaskTrigger -Once -At $when
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DeleteExpiredTaskAfter (New-TimeSpan -Hours 24) `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Limited

    if ($DryRun) {
        Write-Host "  [dry] $name @ $when" -ForegroundColor Cyan
        Write-Host "        $cmd" -ForegroundColor DarkGray
        continue
    }

    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "  [+]   $name @ $when" -ForegroundColor Green
}

Write-Host ""
Write-Host "Done. Verify with:" -ForegroundColor Yellow
Write-Host "  Get-ScheduledTask -TaskName 'HKJCLiveOdds_$($Date)_$($Venue)_*' | Select-Object TaskName,State,@{n='Next';e={(`$_.Triggers[0].StartBoundary)}}"
Write-Host ""
Write-Host "Logs will appear in: $logRoot"
Write-Host "Alerts JSON:         $(Join-Path $Repo 'reports\live_odds_alerts')"
