#Requires -Version 5.1
# Ручной запуск сервера (второй экземпляр не плодим — проверка порта ниже).
# Файл ОБЯЗАН оставаться в UTF-8 с BOM (см. install.ps1).
$ErrorActionPreference = "Stop"
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    throw "venv не найден. Сначала запустите Установить.bat (или .\install.ps1)"
}

# Порт: аргумент командной строки > APP_PORT из .env > дефолт 8473.
$port = 8473
if (Test-Path ".env") {
    $m = Select-String -Path ".env" -Pattern '^\s*APP_PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}
if ($args.Count -ge 1) { $port = [int]$args[0] }

if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "Сервер уже запущен — откройте http://127.0.0.1:$port" -ForegroundColor Green
    Write-Host "(Второй экземпляр не нужен; чтобы перезапустить — Обновить.bat или перезагрузка.)"
    exit 0
}

$env:APP_PORT = "$port"
& ".\.venv\Scripts\python.exe" -m app.main
