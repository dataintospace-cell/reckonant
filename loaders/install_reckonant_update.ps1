# install_reckonant_update.ps1
# Schedules reckonant_update.py (Reckonant's own loaders + API reload) every day at 18:00 BST.
# Replaces the reload-only task "Reckonant daily refresh". Run once with Run with PowerShell.
# Does not touch the Alert Engine.

$ErrorActionPreference = "Stop"
$Folder    = "D:\Reckonant\loaders"
$Script    = Join-Path $Folder "reckonant_update.py"
$TaskName  = "Reckonant daily update"
$OldTask   = "Reckonant daily refresh"
$RunAt     = "18:00"

if (-not (Test-Path $Script)) { throw "Not found: $Script. Save reckonant_update.py in $Folder first." }

if (Get-ScheduledTask -TaskName $OldTask -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $OldTask -Confirm:$false
    Write-Host "Removed old task '$OldTask'."
}

$python   = (Get-Command python -ErrorAction Stop).Source
$action   = New-ScheduledTaskAction -Execute $python -Argument "`"$Script`"" -WorkingDirectory $Folder
$trigger  = New-ScheduledTaskTrigger -Daily -At $RunAt
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 12) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Reckonant: load all registers and reload the API, daily 18:00" -Force | Out-Null

Write-Host "Scheduled '$TaskName' for $RunAt every day."
Write-Host "Python: $python"
Write-Host "Script: $Folder\reckonant_update.py"
Write-Host "Logs:   D:\Reckonant\logs"
