# run_failed_steps.ps1
# Runs the failed Reckonant steps one at a time. Before each step you choose:
#   y = run it, s = skip it, q = quit.
# Run from PowerShell:  powershell -ExecutionPolicy Bypass -File D:\Reckonant\loaders\run_failed_steps.ps1

$ErrorActionPreference = "Continue"
Set-Location D:\Reckonant\loaders

# Settings for today (accounts are read from the Alert Engine's copy, not downloaded)
$env:ACCOUNTS_DIR = "E:\AlertEngine\Accounts"
$env:RK_ACCOUNTS_NO_DOWNLOAD = "1"
if (-not $env:RK_REFRESH_TOKEN) {
    $env:RK_REFRESH_TOKEN = [Environment]::GetEnvironmentVariable("RK_REFRESH_TOKEN", "User")
}

# Failed steps, in the order they must run.
# "company" must run before "mapping" because the mapping step uses the company master.
$steps = @(
    @{ key = "sponsors";   note = "Fixed: the view drop now uses CASCADE." },
    @{ key = "company";    note = "Close the API window first (Ctrl+C in it), so its hourly rebuild does not compete with this step." },
    @{ key = "mapping";    note = "Needs the company master to finish first." },
    @{ key = "gazette";    note = "Failed earlier with HTTP 400. This run shows whether it still happens." },
    @{ key = "findtender"; note = "The source website was timing out. If it fails again, run it again tomorrow." }
)

foreach ($s in $steps) {
    Write-Host ""
    Write-Host "=== $($s.key) ===" -ForegroundColor Cyan
    Write-Host $s.note -ForegroundColor Yellow
    $answer = Read-Host "Run this step now? (y = run, s = skip, q = quit)"
    if ($answer -eq "q") { break }
    if ($answer -ne "y") { Write-Host "Skipped $($s.key)."; continue }

    python reckonant_update.py $s.key
    if ($LASTEXITCODE -eq 0) {
        Write-Host "$($s.key): OK" -ForegroundColor Green
    } else {
        Write-Host "$($s.key): FAILED (exit $LASTEXITCODE). Full log in D:\Reckonant\logs" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "Done. When the API window is open again, run:  python reckonant_update.py api" -ForegroundColor Cyan
