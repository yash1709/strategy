# Registers a Windows scheduled task that runs the monitor every weekday after NSE close.
# Times are in this PC's local time zone (set them for IST if the PC is elsewhere).
# Two triggers: 17:37 Mon-Fri, and 00:22 Tue-Sat (after NSE's end-of-day file for the previous session). Running
# twice is safe: already-processed days are skipped, and missed days are caught up.
#
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
param(
    [string]$TaskName = "NSE RSI SMA50 Monitor",
    [string]$MainTime = "17:37",
    [string]$RetryTime = "00:22"
)
$root = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Force "$root\logs" | Out-Null

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\scripts\run_daily.ps1`"" `
    -WorkingDirectory $root
$days = "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
$nextDays = "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"  # just after midnight following each session
$triggers = @(
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At $MainTime
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek $nextDays -At $RetryTime
)
# StartWhenAvailable: if the PC was off/asleep at the scheduled time, run as soon as it is back.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2) -RunOnlyIfNetworkAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "NSE RSI<30 -> SMA50 crossover monitor" -Force | Out-Null
Write-Host "Registered '$TaskName' (weekdays $MainTime and $RetryTime)."
