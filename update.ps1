#Requires -Version 5.1
# Обновление Meeting Transcriber. Пользовательские данные НЕ трогаются:
# .env, data.db, inbox/, output/, processed/, models/, logs/ остаются на месте.
# Файл ОБЯЗАН оставаться в UTF-8 с BOM (см. install.ps1).
$ErrorActionPreference = "Stop"
$proj = $PSScriptRoot
Set-Location $proj

function Fail([string]$msg) {
    Write-Host "ОШИБКА: $msg" -ForegroundColor Red
    Write-Host "Пришлите этот текст в поддержку." -ForegroundColor Yellow
    exit 1
}

Write-Host "== Meeting Transcriber: обновление ==" -ForegroundColor Cyan

# --- 1. Код ---------------------------------------------------------------------
if (Test-Path (Join-Path $proj ".git")) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "папка .git есть, но git не установлен — обновитесь zip-архивом (распакуйте поверх, НЕ удаляя .env, data.db и папки inbox/output/processed/models)." }
    Write-Host "== git pull"
    & git pull --ff-only
    if ($LASTEXITCODE -ne 0) { Fail "git pull не прошёл (код $LASTEXITCODE). Возможно, локальные файлы изменены — пришлите вывод выше в поддержку." }
} else {
    Write-Host "Это не git-копия. Обновление вручную: распакуйте новый zip-архив" -ForegroundColor Yellow
    Write-Host "поверх этой папки, НЕ удаляя .env, data.db и папки inbox/output/processed/models." -ForegroundColor Yellow
    Write-Host "После распаковки запустите Обновить.bat ещё раз — доустановятся зависимости."
}

# --- 2. Зависимости (могли измениться в новой версии) ----------------------------
$venvPy = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Fail "venv не найден — сначала запустите Установить.bat" }
Write-Host "== проверка зависимостей"
& $venvPy -m pip install -r requirements-ml.txt -r requirements-base.txt --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip завершился с кодом $LASTEXITCODE (подробности выше)." }

# --- 3. Перезапуск сервера --------------------------------------------------------
$port = 8473
if (Test-Path ".env") {
    $m = Select-String -Path ".env" -Pattern '^\s*APP_PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}
$conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    Write-Host "== перезапуск сервера (PID $($conn.OwningProcess))"
    # Супервизор (autostart.ps1) заметит падение и поднимет сервер с новым кодом
    # сам в течение полминуты. Если супервизора нет — подсказываем ручной путь.
    Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 8
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        Write-Host "Сервер перезапущен с новым кодом." -ForegroundColor Green
    } else {
        Write-Host "Сервер остановлен. Запустите его: Запустить.bat" -ForegroundColor Yellow
    }
} else {
    Write-Host "Сервер сейчас не запущен — новый код подхватится при следующем запуске."
}

Write-Host ""
Write-Host "Обновление завершено. Интерфейс: http://127.0.0.1:$port" -ForegroundColor Green
