# install_daily_10am.ps1
# Schedules daily_update.py in D:\Reckonant to run every day at 10:00.
# Right-click this file > Run with PowerShell  (run it once; run it again any time to reset the time).

$ErrorActionPreference = "Stop"
$Folder   = "D:\Reckonant"
$Script   = Join-Path $Folder "daily_update.py"
$TaskName = "Reckonant daily update 10am"

if (-not (Test-Path $Script)) { throw "Not found: $Script. Save daily_update.py in $Folder first." }

$python = (Get-Command python -ErrorAction Stop).Source

$action   = New-ScheduledTaskAction -Execute $python -Argument "`"$Script`"" -WorkingDirectory $Folder
$trigger  = New-ScheduledTaskTrigger -Daily -At 10:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 12) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Daily register update (Reckonant folder), 10:00" -Force | Out-Null

Write-Host "Scheduled '$TaskName' for 10:00 every day."
Write-Host "Python:  $python"
Write-Host "Script:  $Script"
Write-Host ""
Write-Host "Next: open Task Scheduler and DISABLE or DELETE the old 10am task that points at D:\AlertEngine, so the update does not run twice."
