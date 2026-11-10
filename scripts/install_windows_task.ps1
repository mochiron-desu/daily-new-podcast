param(
    [switch]$WithStandbyDaemon,
    [string]$CheckTime = "06:20"
)

$ErrorActionPreference = "Stop"

$RepoDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonW = Join-Path $RepoDir ".venv\Scripts\pythonw.exe"
$PythonExe = Join-Path $RepoDir ".venv\Scripts\python.exe"

if (Test-Path $PythonW) {
    $Runner = $PythonW
} elseif (Test-Path $PythonExe) {
    $Runner = $PythonExe
} else {
    throw "Virtual environment Python not found under $RepoDir\.venv\Scripts\. Create .venv first."
}

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun:$false `
    -MultipleInstances IgnoreNew

$FailoverAction = New-ScheduledTaskAction `
    -Execute $Runner `
    -Argument "-m src.cli failover-check" `
    -WorkingDirectory $RepoDir

$DailyTrigger = New-ScheduledTaskTrigger -Daily -At $CheckTime
$DailyTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At $CheckTime -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Hours 2)).Repetition

$LogonTrigger = New-ScheduledTaskTrigger -AtLogOn

Register-ScheduledTask `
    -TaskName "DailyNewsPodcast-FailoverCheck" `
    -Action $FailoverAction `
    -Trigger @($DailyTrigger, $LogonTrigger) `
    -Settings $Settings `
    -Description "Checks Railway primary status after cutoff and generates/publishes the daily podcast locally if Railway is down." `
    -Force | Out-Null

Write-Host "[OK] Registered Scheduled Task: DailyNewsPodcast-FailoverCheck (using $Runner)"

if ($WithStandbyDaemon) {
    $StandbyAction = New-ScheduledTaskAction `
        -Execute $Runner `
        -Argument "-m src.cli serve" `
        -WorkingDirectory $RepoDir
    $StandbyTrigger = New-ScheduledTaskTrigger -AtLogOn
    $StandbySettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Days 0) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)

    Register-ScheduledTask `
        -TaskName "DailyNewsPodcast-StandbyDaemon" `
        -Action $StandbyAction `
        -Trigger $StandbyTrigger `
        -Settings $StandbySettings `
        -Description "Runs local standby peer HTTP server for multi-machine election and state sync." `
        -Force | Out-Null

    Write-Host "[OK] Registered Scheduled Task: DailyNewsPodcast-StandbyDaemon"
}

