@echo off
rem Pure ASCII only (comments too) - see the install bat header for why.
rem Russian output is produced by update.ps1.
cd /d "%~dp0"
title Meeting Transcriber - obnovlenie
powershell -NoProfile -ExecutionPolicy Bypass -File "update.ps1" %*
if errorlevel 1 (
    echo.
    echo ERROR: obnovlenie zavershilos' s oshibkoy - sm. tekst vyshe ^(po-russki^).
    pause
    exit /b 1
)
pause
