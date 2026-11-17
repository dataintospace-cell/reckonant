# start_reckonant_api.ps1
# Starts the Reckonant API on port 8001 with all settings in one go.
# Right-click > Run with PowerShell. Keep the window open while the API should run.
# The daily refresh (18:00) only works while this window is running.

$ErrorActionPreference = "Stop"
$BackendDir = "D:\Reckonant\reckonant-backend\reckonant-backend\reckonant_backend"
Set-Location $BackendDir

function Read-Secret($prompt) {
    $secure = Read-Host $prompt -AsSecureString
    return [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}

$pw    = Read-Secret "Password for reckonant_ro"
$token = Read-Secret "RK_REFRESH_TOKEN (same value the daily job uses)"

$env:RK_DATABASE_URL = "postgresql://reckonant_ro:$pw@localhost:5432/postgres"
$env:RK_DEV          = "1"
$env:RK_APP_DIR      = "D:\Reckonant\reckonant-ui-live"
$env:RK_MAX_COMPANIES = "40000"
$env:RK_REFRESH_TOKEN = $token

Write-Host "Starting Reckonant API on http://localhost:8001 ..."
python -m uvicorn api:app --port 8001
