# install_reckonant_daily.ps1
# Schedules reckonant_daily.py (Reckonant only) to run every day at 10:30.
# Right-click this file > Run with PowerShell. Run it again any time to change the time.
# Does not touch the Alert Engine.

$ErrorActionPreference = "Stop"
$Folder   = "D:\Reckonant"
$Script   = Join-Path $Folder "reckonant_daily.py"
$TaskName = "Reckonant daily refresh"
$RunAt    = "10:30"

if (-not (Test-Path $Script)) { throw "Not found: $Script. Save reckonant_daily.py in $Folder first." }

$python = (Get-Command python -ErrorAction Stop).Source

$action   = New-ScheduledTaskAction -Execute $python -Argument "`"$Script`"" -WorkingDirectory $Folder
$trigger  = New-ScheduledTaskTrigger -Daily -At $RunAt
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Reckonant: reload API data from PostgreSQL daily" -Force | Out-Null

Write-Host "Scheduled '$TaskName' for $RunAt every day."
Write-Host "Python: $python"
Write-Host "Script: $Script"
Write-Host "Log folder: $Folder\logs"
Write-Host ""
Write-Host "The Reckonant API (port 8001) must be running at that time."
