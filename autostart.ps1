#Requires -Version 5.1
# Супервизор сервера Meeting Transcriber (запускается задачей Планировщика
# «при входе в систему»; вручную его трогать обычно не нужно).
#
# Что делает:
#   1) не плодит второй экземпляр (проверка порта);
#   2) держит сервер запущенным: процесс упал → пауза → перезапуск;
#   3) ЗАЩИТА ОТ ЗАЦИКЛИВАНИЯ: 5 падений за 10 минут → сдаёмся с понятной
#      записью в лог (иначе сломанная установка крутила бы crash-loop вечно);
#   4) логи не затираются: перед каждым запуском прежние server.out/err.log
#      переименовываются с номером (хранятся 5 поколений) — ошибка «вчера
#      вечером» остаётся доступна для диагностики.
#
# Файл ОБЯЗАН оставаться в UTF-8 с BOM (см. install.ps1).
$ErrorActionPreference = "Stop"
$proj = $PSScriptRoot
Set-Location $proj
New-Item -ItemType Directory -Force (Join-Path $proj "logs") | Out-Null

$py = Join-Path $proj ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] venv не найден ($py). Сначала запустите Установить.bat" |
        Out-File -Append -Encoding utf8 (Join-Path $proj "logs\server.err.log")
    exit 1
}

# UTF-8 в выводе python — логи читаются любым редактором.
$env:PYTHONUTF8 = "1"

# Порт берём из .env (APP_PORT) или дефолт 8473 — для проверки «уже запущен».
$port = 8473
$envFile = Join-Path $proj ".env"
if (Test-Path $envFile) {
    $m = Select-String -Path $envFile -Pattern '^\s*APP_PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    exit 0   # уже работает — выходим тихо
}

$outLog = Join-Path $proj "logs\server.out.log"
$errLog = Join-Path $proj "logs\server.err.log"

# Ротация: server.out.log → server.out.log.1, .1 → .2 и т.д. до 5 поколений.
function Rotate-Log([string]$path) {
    if (-not (Test-Path $path)) { return }
    for ($i = 4; $i -ge 1; $i--) {
        if (Test-Path "$path.$i") { Move-Item -Force "$path.$i" "$path.$($i + 1)" }
    }
    Move-Item -Force $path "$path.1"
}

$maxRestarts = 5
$windowSec = 600
$crashes = @()

while ($true) {
    Rotate-Log $outLog
    Rotate-Log $errLog

    # Start-Process с -Wait: супервизор живёт, пока жив сервер; редиректы пишут
    # сырые байты процесса (UTF-8 благодаря PYTHONUTF8 выше). Учтите: редирект
    # ПЕРЕЗАПИСЫВАЕТ файлы — поэтому ротация выше, а не append.
    $started = Get-Date
    $proc = Start-Process -FilePath $py -ArgumentList @("-m", "app.main") `
        -WorkingDirectory $proj -WindowStyle Hidden -Wait -PassThru `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $code = $proc.ExitCode

    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "[$stamp] сервер завершился с кодом $code" | Out-File -Append -Encoding utf8 $errLog

    $now = Get-Date
    $crashes = @($crashes | Where-Object { ($now - $_).TotalSeconds -lt $windowSec }) + $now
    if ($crashes.Count -gt $maxRestarts) {
        "[$stamp] СЕРВЕР УПАЛ $maxRestarts+ РАЗ ЗА $($windowSec / 60) МИНУТ — автоперезапуск остановлен, чтобы не зациклиться. Разберитесь по логам выше или пришлите logs\server.err.log в поддержку. После исправления запустите Запустить.bat." |
            Out-File -Append -Encoding utf8 $errLog
        exit 1
    }
    # Упал почти сразу → вероятно системная проблема (порт занят, битая
    # установка): ждём дольше, чтобы не молотить впустую.
    $uptime = ($now - $started).TotalSeconds
    if ($uptime -lt 60) { Start-Sleep -Seconds 30 } else { Start-Sleep -Seconds 5 }
}
