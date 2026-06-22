#Requires -Version 5.1
# Регистрирует задачу Планировщика, поднимающую сервер при входе в систему.
# Запускается из install.ps1 (по выбору пользователя) или вручную в любой момент.
$ErrorActionPreference = "Stop"
$proj = $PSScriptRoot
$script = Join-Path $proj "autostart.ps1"
if (-not (Test-Path $script)) { Write-Error "Не найден autostart.ps1 рядом со скриптом."; exit 1 }

$taskName = "MeetingTranscriber"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Автозапуск Meeting Transcriber при входе в систему" -Force | Out-Null

Write-Host "Автозапуск включён (задача '$taskName')." -ForegroundColor Green
Write-Host "Запустить сейчас:  Start-ScheduledTask $taskName" -ForegroundColor Cyan
Write-Host "Отключить:         .\unregister-autostart.ps1" -ForegroundColor Cyan
