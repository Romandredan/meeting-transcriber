#Requires -Version 5.1
# Установка Meeting Transcriber. Скрипт ИДЕМПОТЕНТЕН: повторный запуск безопасен,
# уже установленное пропускается — это наш путь «починить после сбоя».
#
# ВАЖНО про ошибки: $ErrorActionPreference="Stop" в PowerShell 5.1 НЕ ловит
# ненулевые коды возврата внешних команд (winget, pip, ollama). Поэтому каждая
# внешняя команда идёт через Run-Exe с проверкой $LASTEXITCODE, а любой сбой
# заканчивается читаемым сообщением и указанием прислать logs\install.log
# в поддержку — молчаливо «Готово» на сломанной установке недопустимо.
#
# Файл ОБЯЗАН оставаться в UTF-8 с BOM — без BOM PowerShell 5.1 читает русский
# текст как cp1251 и все сообщения превращаются в кракозябры.
param(
    [switch]$Autostart,    # включить автозапуск без вопроса
    [switch]$NoAutostart,  # не включать автозапуск без вопроса
    [switch]$SkipOllama    # не ставить Ollama и не качать LLM (анализ не нужен)
)
$ErrorActionPreference = "Stop"
$proj = $PSScriptRoot
Set-Location $proj
New-Item -ItemType Directory -Force (Join-Path $proj "logs") | Out-Null
Start-Transcript -Path (Join-Path $proj "logs\install.log") -Force | Out-Null

function Has($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Fail([string]$step, [string]$detail) {
    Write-Host ""
    Write-Host "ОШИБКА на шаге: $step" -ForegroundColor Red
    if ($detail) { Write-Host $detail -ForegroundColor Red }
    Write-Host ""
    Write-Host "Установка остановлена. Пришлите этот текст и файл logs\install.log" -ForegroundColor Yellow
    Write-Host "в поддержку — по ним найдём причину. После исправления просто запустите" -ForegroundColor Yellow
    Write-Host "установку ещё раз: всё готовое будет пропущено." -ForegroundColor Yellow
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}

function Run-Exe([string]$step, [string]$exe, [string[]]$exeArgs) {
    Write-Host ""
    Write-Host "== $step" -ForegroundColor Cyan
    & $exe @exeArgs
    if ($LASTEXITCODE -ne 0) {
        Fail $step ("Команда завершилась с кодом {0}. Подробности — выше в окне и в logs\install.log." -f $LASTEXITCODE)
    }
}

# PATH текущей сессии не обновляется после установки новых программ — подхватываем
# из реестра вручную, иначе «только что поставленный» ffmpeg/ollama не находится.
function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

# Значение KEY=VALUE из .env (без чтения чужих секретов в лог — возвращаем только
# запрошенный ключ вызывающему коду).
function Get-EnvValue([string]$name, [string]$default) {
    if (Test-Path ".env") {
        $m = Select-String -Path ".env" -Pattern "^\s*$name\s*=\s*(.*)$" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($m) {
            $v = $m.Matches[0].Groups[1].Value.Trim()
            if ($v -ne "") { return $v }
        }
    }
    return $default
}

Write-Host "== Meeting Transcriber: установка ==" -ForegroundColor Cyan

# --- 0. Место на диске -------------------------------------------------------
# Полный набор: torch cu128 (~3 ГБ) + модель Whisper (~3 ГБ) + LLM (~9 ГБ) +
# веса pyannote + сам venv. Без анализа — заметно меньше.
$analyzeEnabled = (Get-EnvValue "ANALYZE_ENABLED" "true") -notin @("false", "0", "no", "off")
$needGb = if ($analyzeEnabled -and -not $SkipOllama) { 20 } else { 8 }
$drive = (Get-Item $proj).PSDrive.Name
$freeGb = [math]::Round((Get-PSDrive $drive).Free / 1GB, 1)
if ($freeGb -lt $needGb) {
    Fail "Проверка места на диске" "На диске ${drive}: свободно $freeGb ГБ, нужно хотя бы $needGb ГБ. Освободите место и запустите установку снова."
}
Write-Host "Диск ${drive}: свободно $freeGb ГБ (нужно $needGb ГБ) — ок"

# --- 1. Python 3.10–3.13 ------------------------------------------------------
function Find-Python {
    foreach ($v in @("3.12", "3.11", "3.13", "3.10")) {
        try {
            $ver = & py "-$v" --version 2>$null
            if ($LASTEXITCODE -eq 0 -and "$ver" -match "3\.(1[0-3])") { return @("py", "-$v") }
        } catch { }  # py-лаунчера нет вовсе — ставим Python ниже
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    if (Has winget) {
        Run-Exe "Установка Python 3.12 (winget)" "winget" @("install", "-e", "--id", "Python.Python.3.12", "--accept-source-agreements", "--accept-package-agreements")
    } else {
        # Fallback без winget (старые Windows / LTSC): официальный установщик.
        Write-Host "winget недоступен — скачиваю установщик Python с python.org..." -ForegroundColor Yellow
        $url = "https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe"
        $dst = Join-Path $env:TEMP "python-3.12.9-amd64.exe"
        try { Invoke-WebRequest -Uri $url -OutFile $dst -UseBasicParsing }
        catch { Fail "Скачивание Python" "Не удалось скачать $url. Проверьте интернет или установите Python 3.12 вручную с python.org (при установке поставьте галочку 'Add python.exe to PATH'), затем запустите установку снова.`n$_" }
        Run-Exe "Установка Python 3.12 (python.org)" $dst @("/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1", "Include_test=0")
    }
    Update-SessionPath
    $py = Find-Python
    if (-not $py) { Fail "Установка Python" "Python установлен, но не находится. Перезагрузите компьютер и запустите установку ещё раз." }
}
Write-Host "Python: $($py -join ' ')"

# --- 2. ffmpeg ---------------------------------------------------------------
function Install-FfmpegFallback {
    # Fallback без winget: готовая сборка с gyan.dev, распаковка в tools\ffmpeg.
    Write-Host "Скачиваю ffmpeg напрямую (gyan.dev)..." -ForegroundColor Yellow
    $url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    $zip = Join-Path $env:TEMP "ffmpeg-essentials.zip"
    try { Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing }
    catch { Fail "Скачивание ffmpeg" "Не удалось скачать $url. Проверьте интернет или установите ffmpeg вручную (нужно, чтобы ffmpeg был в PATH).`n$_" }
    $tools = Join-Path $proj "tools"
    $tmp = Join-Path $env:TEMP "ffmpeg-extract"
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    try { Expand-Archive -Path $zip -DestinationPath $tmp -Force }
    catch { Fail "Распаковка ffmpeg" "Архив $zip не распаковался: $_" }
    $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
    Remove-Item -Recurse -Force (Join-Path $tools "ffmpeg") -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force $tools | Out-Null
    Move-Item $inner.FullName (Join-Path $tools "ffmpeg")
    $bin = Join-Path $tools "ffmpeg\bin"
    # В PATH текущей сессии и в PATH пользователя (для будущих запусков сервера).
    $env:Path = "$bin;$env:Path"
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ("$userPath" -notlike "*$bin*") {
        [Environment]::SetEnvironmentVariable("Path", "$bin;$userPath", "User")
    }
}

if (-not (Has ffmpeg)) {
    if (Has winget) {
        Run-Exe "Установка ffmpeg (winget)" "winget" @("install", "-e", "--id", "Gyan.FFmpeg", "--accept-source-agreements", "--accept-package-agreements")
        Update-SessionPath
        if (-not (Has ffmpeg)) {
            Write-Host "winget поставил ffmpeg, но он не виден в PATH — ставлю напрямую..." -ForegroundColor Yellow
            Install-FfmpegFallback
        }
    } else {
        Install-FfmpegFallback
    }
}
# Явная ре-проверка: без ffmpeg сервер ставится, но каждая расшифровка падает.
if (-not (Has ffmpeg)) { Fail "Проверка ffmpeg" "ffmpeg установлен, но не находится в PATH. Перезагрузите компьютер и запустите установку ещё раз." }
Write-Host "ffmpeg: найден"

# --- 3. GPU == выбор сборки torch ----------------------------------------------
$torchIndex = "https://download.pytorch.org/whl/cpu"
if (Has nvidia-smi) {
    Write-Host "Обнаружена NVIDIA GPU == torch cu128" -ForegroundColor Green
    $torchIndex = "https://download.pytorch.org/whl/cu128"
} else {
    Write-Host "NVIDIA не обнаружена == torch CPU (расшифровка будет медленной)" -ForegroundColor Yellow
}

# --- 4. venv + зависимости ----------------------------------------------------
$venvPy = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    $pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)
    Run-Exe "Создание виртуального окружения" $pyExe ($pyArgs + @("-m", "venv", ".venv"))
}
Run-Exe "Обновление pip" $venvPy @("-m", "pip", "install", "--upgrade", "pip")
Run-Exe "Установка torch (это несколько ГБ, может занять время)" $venvPy @("-m", "pip", "install", "torch", "torchaudio", "--index-url", $torchIndex)
Run-Exe "Установка ML-зависимостей" $venvPy @("-m", "pip", "install", "-r", "requirements-ml.txt")
Run-Exe "Установка базовых зависимостей" $venvPy @("-m", "pip", "install", "-r", "requirements-base.txt")

# --- 5. .env и HF_TOKEN -------------------------------------------------------
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Создан .env из шаблона." -ForegroundColor Yellow
}
$hfToken = Get-EnvValue "HF_TOKEN" ""
if (-not $hfToken) {
    Write-Host ""
    Write-Host "Диаризация (разделение реплик по спикерам) требует бесплатный токен HuggingFace:" -ForegroundColor Yellow
    Write-Host "  1) huggingface.co == Settings == Access Tokens == Create new token (тип Read)"
    Write-Host "  2) на странице huggingface.co/pyannote/speaker-diarization-community-1 принять условия"
    Write-Host "  3) вставить токен ниже (или потом — в файл .env, строка HF_TOKEN=)"
    $ans = Read-Host "HF_TOKEN (Enter — пропустить, спикеры не будут размечаться)"
    if ($ans) {
        # .env переписываем через .NET в UTF-8 без BOM: Get-Content/Set-Content
        # в PS 5.1 гарантированно испортили бы русские комментарии файла.
        $envPath = Join-Path $proj ".env"
        $txt = [IO.File]::ReadAllText($envPath, [Text.Encoding]::UTF8)
        $txt = [Text.RegularExpressions.Regex]::Replace($txt, '(?m)^\s*HF_TOKEN=.*$', "HF_TOKEN=$ans")
        $utf8NoBom = New-Object Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($envPath, $txt, $utf8NoBom)
        $hfToken = $ans
        Write-Host "Токен записан в .env"
    } else {
        Write-Host "Пропущено: расшифровка будет работать, но без разделения на спикеров." -ForegroundColor DarkYellow
    }
}

# --- 6. Ollama + модель анализа -----------------------------------------------
function Ensure-OllamaRunning {
    try {
        $null = Invoke-WebRequest -Uri "http://localhost:11434/api/version" -UseBasicParsing -TimeoutSec 3
        return
    } catch { }
    Write-Host "Запускаю демон Ollama..."
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            $null = Invoke-WebRequest -Uri "http://localhost:11434/api/version" -UseBasicParsing -TimeoutSec 2
            return
        } catch { }
    }
    Fail "Запуск Ollama" "Демон Ollama не ответил за 30 секунд. Перезагрузите компьютер и запустите установку ещё раз."
}

if ($analyzeEnabled -and -not $SkipOllama) {
    if (-not (Has ollama)) {
        if (Has winget) {
            Run-Exe "Установка Ollama (winget)" "winget" @("install", "-e", "--id", "Ollama.Ollama", "--accept-source-agreements", "--accept-package-agreements")
        } else {
            Write-Host "winget недоступен — скачиваю Ollama с ollama.com..." -ForegroundColor Yellow
            $url = "https://ollama.com/download/OllamaSetup.exe"
            $dst = Join-Path $env:TEMP "OllamaSetup.exe"
            try { Invoke-WebRequest -Uri $url -OutFile $dst -UseBasicParsing }
            catch { Fail "Скачивание Ollama" "Не удалось скачать $url. Проверьте интернет или установите Ollama вручную с ollama.com.`n$_" }
            Run-Exe "Установка Ollama" $dst @("/S")
        }
        Update-SessionPath
        if (-not (Has ollama)) { Fail "Установка Ollama" "Ollama установлена, но команда ollama недоступна. Перезагрузите компьютер и запустите установку ещё раз." }
    } else {
        Write-Host "Ollama: уже установлена"
    }

    # Переменные демона: без них KV-кэш ест вдвое больше VRAM и окно 32k не влезает
    # в 16 ГБ. Не перезаписываем существующие значения — вдруг заданы осознанно.
    foreach ($kv in @(@("OLLAMA_FLASH_ATTENTION", "1"), @("OLLAMA_KV_CACHE_TYPE", "q8_0"))) {
        if (-not [Environment]::GetEnvironmentVariable($kv[0], "User")) {
            [Environment]::SetEnvironmentVariable($kv[0], $kv[1], "User")
            Write-Host "Записана переменная $($kv[0])=$($kv[1]) (Ollama подхватит её после перезапуска)"
        }
    }

    Ensure-OllamaRunning
    $llmModel = Get-EnvValue "LLM_MODEL" "qwen3:14b"
    Run-Exe "Скачивание модели анализа $llmModel (несколько ГБ, один раз)" "ollama" @("pull", $llmModel)
} else {
    Write-Host "Анализ отключён (ANALYZE_ENABLED=false или -SkipOllama) — Ollama не требуется."
}

# --- 7. Предзагрузка моделей распознавания ------------------------------------
# Иначе первая расшифровка молча качает ~3 ГБ и выглядит как зависание.
Run-Exe "Скачивание модели распознавания Whisper (≈3 ГБ, один раз)" $venvPy @("scripts\warmup_models.py", "whisper")
if ($hfToken) {
    & $venvPy @("scripts\warmup_models.py", "pyannote")
    if ($LASTEXITCODE -ne 0) {
        # Нефатально: токен может быть без принятых условий pyannote. Пользователь
        # получит понятное предупреждение здесь, а не молчаливую диаризацию «нет».
        Write-Host "ВНИМАНИЕ: веса диаризации не скачались. Проверьте, что на странице" -ForegroundColor DarkYellow
        Write-Host "huggingface.co/pyannote/speaker-diarization-community-1 приняты условия" -ForegroundColor DarkYellow
        Write-Host "именно тем аккаунтом, чей токен в .env. Расшифровка будет без спикеров." -ForegroundColor DarkYellow
    }
}

# --- 8. Автозапуск при входе (по умолчанию — да) -------------------------------
# Сервер должен переживать перезагрузки: задача Планировщика «при входе» +
# супервизор в autostart.ps1 (перезапуск при падении, с защитой от зацикливания).
$doAutostart = $true
if ($Autostart) { $doAutostart = $true }
elseif ($NoAutostart) { $doAutostart = $false }
else {
    Write-Host ""
    $ans = Read-Host "Запускать сервер автоматически при входе в Windows? [Y/n]"
    $doAutostart = ($ans -notmatch '^(n|no|н|нет)$')
}
if ($doAutostart) {
    & (Join-Path $PSScriptRoot "register-autostart.ps1")
    if ($LASTEXITCODE -ne 0) { Fail "Настройка автозапуска" "register-autostart.ps1 завершился с кодом $LASTEXITCODE." }
    # Запускаем сразу, не дожидаясь перезахода в систему.
    Start-ScheduledTask -TaskName "MeetingTranscriber" -ErrorAction SilentlyContinue
} else {
    Write-Host "Автозапуск не настроен. Включить позже: .\register-autostart.ps1" -ForegroundColor DarkGray
}

$port = Get-EnvValue "APP_PORT" "8473"
Write-Host ""
Write-Host "Готово!" -ForegroundColor Green
if ($doAutostart) {
    Write-Host "Сервер запущен и будет стартовать вместе с Windows."
} else {
    Write-Host "Запуск вручную: Запустить.bat"
}
Write-Host "Интерфейс: http://127.0.0.1:$port" -ForegroundColor Cyan
try { Stop-Transcript | Out-Null } catch {}
