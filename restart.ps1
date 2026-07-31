#Requires -Version 5.1
# Перезапуск сервера с подхватом нового кода (после правок или git pull).
# Гасим процесс на порту: если работает супервизор автозапуска — он сам поднимет
# сервер через несколько секунд; если нет — запускаем сами через run.ps1.
# Файл ОБЯЗАН оставаться в UTF-8 с BOM (см. install.ps1).
$ErrorActionPreference = "Stop"

# Порт: аргумент командной строки > APP_PORT из .env > дефолт 8473 (как в run.ps1).
$port = 8473
if (Test-Path ".env") {
    $m = Select-String -Path ".env" -Pattern '^\s*APP_PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}
if ($args.Count -ge 1) { $port = [int]$args[0] }

$conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $conn) {
    Write-Host "Сервер не запущен — запускаю." -ForegroundColor Yellow
    & ".\run.ps1" $port
    exit $LASTEXITCODE
}

Stop-Process -Id $conn.OwningProcess -Force
Write-Host "Сервер остановлен (порт $port)." -ForegroundColor Green

$task = Get-ScheduledTask -TaskName "MeetingTranscriber" -ErrorAction SilentlyContinue
if ($task -and $task.State -eq "Running") {
    Write-Host "Супервизор автозапуска поднимет сервер через несколько секунд — обновите страницу в браузере." -ForegroundColor Cyan
    exit 0
}

Write-Host "Автозапуск не настроен — запускаю вручную." -ForegroundColor Yellow
& ".\run.ps1" $port
exit $LASTEXITCODE
