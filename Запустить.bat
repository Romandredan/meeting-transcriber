@echo off
rem Pure ASCII only (comments too) - see the install bat header for why.
rem Russian output is produced by run.ps1.
cd /d "%~dp0"
title Meeting Transcriber
powershell -NoProfile -ExecutionPolicy Bypass -File "run.ps1" %*
if errorlevel 1 (
    echo.
    echo ERROR: server zavershilsya s oshibkoy - sm. tekst vyshe ^(po-russki^).
    pause
    exit /b 1
)
