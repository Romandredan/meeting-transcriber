#Requires -Version 5.1
$ErrorActionPreference = "Stop"
if (-not (Test-Path ".\.venv\Scripts\python.exe")) { throw "venv не найден. Сначала запустите .\install.ps1" }
# Необязательный аргумент-порт перекрывает .env/env; иначе хост/порт берутся из config (.env → APP_PORT, дефолт 8473)
if ($args.Count -ge 1) { $env:APP_PORT = $args[0] }
& ".\.venv\Scripts\python.exe" -m app.main
