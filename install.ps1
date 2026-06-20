#Requires -Version 5.1
$ErrorActionPreference = "Stop"
Write-Host "== Meeting Transcriber: установка ==" -ForegroundColor Cyan

function Has($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# 1. Python 3.10–3.13 (предпочтительно 3.12)
$py = $null
foreach ($v in @("3.12", "3.11", "3.13", "3.10")) {
    $ver = & py "-$v" --version 2>$null
    if ($ver -match "3\.(1[0-3])") { $py = "py -$v"; break }
}
if (-not $py) {
    Write-Host "Python 3.10–3.13 не найден. Ставлю 3.12 через winget..." -ForegroundColor Yellow
    if (Has winget) { winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements; $py = "py -3.12" }
    else { throw "Нет winget. Установите Python 3.12 вручную с python.org и перезапустите." }
}
Write-Host "Python: $py"

# 2. ffmpeg
if (-not (Has ffmpeg)) {
    Write-Host "ffmpeg не найден. Ставлю через winget..." -ForegroundColor Yellow
    if (Has winget) { winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements }
    else { throw "Нет winget. Установите ffmpeg вручную и добавьте в PATH." }
}

# 3. Детект GPU → выбор torch-индекса
$torchIndex = "https://download.pytorch.org/whl/cpu"
if (Has nvidia-smi) {
    Write-Host "Обнаружена NVIDIA GPU → torch cu128" -ForegroundColor Green
    $torchIndex = "https://download.pytorch.org/whl/cu128"
} else {
    Write-Host "NVIDIA не обнаружена → torch CPU (работа будет медленной)" -ForegroundColor Yellow
}

# 4. venv + зависимости
$pyParts = $py.Split(" ")
& $pyParts[0] $pyParts[1..($pyParts.Length-1)] -m venv .venv
$venvPy = ".\.venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install torch torchaudio --index-url $torchIndex
& $venvPy -m pip install -r requirements-ml.txt
& $venvPy -m pip install -r requirements-base.txt

# 5. .env
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "Создан .env — впишите HF_TOKEN для диаризации." -ForegroundColor Yellow }

Write-Host "Готово. Запуск: .\run.ps1" -ForegroundColor Cyan
