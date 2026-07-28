@echo off
rem IMPORTANT: pure ASCII only (comments too!). cmd parses .bat in the console
rem codepage, and UTF-8 Cyrillic desyncs its parser even inside rem lines.
rem All Russian output lives in install.ps1 (UTF-8 with BOM, PS writes Unicode).
cd /d "%~dp0"
title Meeting Transcriber - ustanovka
powershell -NoProfile -ExecutionPolicy Bypass -File "install.ps1" %*
if errorlevel 1 (
    echo.
    echo ERROR: ustanovka zavershilas' s oshibkoy - sm. tekst vyshe ^(po-russki^).
    pause
    exit /b 1
)
pause
