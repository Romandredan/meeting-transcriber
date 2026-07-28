#Requires -Version 5.1
# Диагностика для поддержки: собирает состояние системы в один zip-архив.
# НИКАКИХ СЕКРЕТОВ: .env в архив не попадает — фиксируется только факт
# «HF_TOKEN задан / не задан».
# Файл ОБЯЗАН оставаться в UTF-8 с BOM (см. install.ps1).
$ErrorActionPreference = "Continue"   # диагностика не должна падать: собираем что есть
$proj = $PSScriptRoot
Set-Location $proj

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stage = Join-Path $env:TEMP "mt-diag-$stamp"
New-Item -ItemType Directory -Force $stage | Out-Null

function Save($name, [scriptblock]$cmd) {
    try { & $cmd 2>&1 | Out-String | Out-File -Encoding utf8 (Join-Path $stage $name) }
    catch { "не удалось собрать: $_" | Out-File -Encoding utf8 (Join-Path $stage $name) }
}

Write-Host "Собираю диагностику..."

# --- система ------------------------------------------------------------------
Save "01-system.txt" {
    "Дата:            $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    "Компьютер:       $env:COMPUTERNAME"
    "Пользователь:    $env:USERNAME"
    "Windows:         $([Environment]::OSVersion.VersionString)"
    "PowerShell:      $($PSVersionTable.PSVersion)"
    "Папка проекта:   $proj"
}

# --- python и пакеты ------------------------------------------------------------
$venvPy = Join-Path $proj ".venv\Scripts\python.exe"
Save "02-python.txt" {
    if (Test-Path $venvPy) {
        "== версия =="
        & $venvPy --version
        "== ключевые пакеты =="
        & $venvPy -m pip list 2>$null | Select-String -Pattern "torch|ctranslate|faster-whisper|pyannote|transformers|fastapi|uvicorn|watchdog"
    } else {
        "venv не найден — установка не завершалась?"
    }
}

# --- GPU ----------------------------------------------------------------------
Save "03-gpu.txt" {
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { & nvidia-smi }
    else { "nvidia-smi не найден — NVIDIA GPU отсутствует или драйвер не установлен" }
}

# --- Ollama -------------------------------------------------------------------
Save "04-ollama.txt" {
    if (Get-Command ollama -ErrorAction SilentlyContinue) {
        "== ollama ps =="
        & ollama ps
        "== переменные демона =="
        "OLLAMA_FLASH_ATTENTION = $([Environment]::GetEnvironmentVariable('OLLAMA_FLASH_ATTENTION', 'User'))"
        "OLLAMA_KV_CACHE_TYPE   = $([Environment]::GetEnvironmentVariable('OLLAMA_KV_CACHE_TYPE', 'User'))"
    } else { "ollama не установлена" }
    "== /api/version =="
    try { (Invoke-WebRequest -Uri "http://localhost:11434/api/version" -UseBasicParsing -TimeoutSec 3).Content }
    catch { "демон Ollama не отвечает: $_" }
}

# --- настройки (без секретов) ---------------------------------------------------
Save "05-env.txt" {
    if (Test-Path ".env") {
        $envTxt = [IO.File]::ReadAllText((Join-Path $proj ".env"), [Text.Encoding]::UTF8)
        "HF_TOKEN:        " + $(if ($envTxt -match '(?m)^\s*HF_TOKEN\s*=\s*\S') { "задан (значение скрыто)" } else { "НЕ задан — диаризация работать не будет" })
        foreach ($k in @("APP_HOST", "APP_PORT", "ANALYZE_ENABLED", "LLM_MODEL", "WHISPER_MODEL", "MOVE_PROCESSED", "IDLE_UNLOAD_SECONDS")) {
            $m = [Text.RegularExpressions.Regex]::Match($envTxt, "(?m)^\s*$k\s*=\s*(.*)$")
            if ($m.Success) { "$k = $($m.Groups[1].Value.Trim())" }
        }
    } else { ".env не создан" }
}

# --- сервер ---------------------------------------------------------------------
$port = 8473
if (Test-Path ".env") {
    $m = Select-String -Path ".env" -Pattern '^\s*APP_PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}
Save "06-server.txt" {
    $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    "Порт ${port}: " + $(if ($listening) { "слушается (PID $($listening[0].OwningProcess))" } else { "НЕ слушается — сервер не запущен" })
    "== /api/llm/health =="
    try { (Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/llm/health" -UseBasicParsing -TimeoutSec 5).Content }
    catch { "сервер не ответил: $_" }
    "== последние встречи и ошибки (из БД) =="
    if ((Test-Path $venvPy) -and (Test-Path (Join-Path $proj "data.db"))) {
        & $venvPy -c "import sqlite3; c = sqlite3.connect('data.db'); c.row_factory = sqlite3.Row; [print(f'{r[\"id\"]:>4} {r[\"status\"]:<10} {r[\"filename\"][:60]}  {r[\"error\"][:200]}') for r in c.execute('SELECT id, status, filename, error FROM jobs ORDER BY id DESC LIMIT 20')]"
    }
}

# --- логи -----------------------------------------------------------------------
New-Item -ItemType Directory -Force (Join-Path $stage "logs") | Out-Null
foreach ($f in @("install.log", "server.out.log", "server.out.log.1", "server.err.log", "server.err.log.1")) {
    $src = Join-Path $proj "logs\$f"
    if (Test-Path $src) { Copy-Item $src (Join-Path $stage "logs\$f") }
}

# --- упаковка ---------------------------------------------------------------------
New-Item -ItemType Directory -Force (Join-Path $proj "logs") | Out-Null
$zipPath = Join-Path $proj "logs\diag-$stamp.zip"
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zipPath -Force
Remove-Item -Recurse -Force $stage

Write-Host ""
Write-Host "Диагностика собрана:" -ForegroundColor Green
Write-Host "  $zipPath" -ForegroundColor Cyan
Write-Host "Отправьте этот файл в поддержку вместе с описанием проблемы." -ForegroundColor Yellow
