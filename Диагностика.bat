@echo off
rem Pure ASCII only (comments too) - see the install bat header for why.
rem Russian output is produced by diag.ps1.
cd /d "%~dp0"
title Meeting Transcriber - diagnostika
powershell -NoProfile -ExecutionPolicy Bypass -File "diag.ps1"
echo.
pause
