#Requires -Version 5.1
# Launcher for autostart-at-logon (registered as a Scheduled Task).
# Starts the server hidden, logs to logs\, and won't start a second copy.
$ErrorActionPreference = "Stop"
$proj = $PSScriptRoot
Set-Location $proj
New-Item -ItemType Directory -Force (Join-Path $proj "logs") | Out-Null

$py = Join-Path $proj ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "venv не найден ($py). Сначала .\install.ps1"; exit 1 }

# Порт берём из .env (APP_PORT) или дефолт 8473 — для проверки «уже запущен».
$port = 8473
$envFile = Join-Path $proj ".env"
if (Test-Path $envFile) {
    $m = Select-String -Path $envFile -Pattern '^\s*APP_PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    # уже работает — выходим тихо
    exit 0
}

Start-Process -FilePath $py -ArgumentList @("-m", "app.main") -WorkingDirectory $proj `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $proj "logs\server.out.log") `
    -RedirectStandardError  (Join-Path $proj "logs\server.err.log")
