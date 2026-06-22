#Requires -Version 5.1
# Отключает автозапуск (удаляет задачу Планировщика). Сервер не трогает.
$ErrorActionPreference = "Stop"
$taskName = "MeetingTranscriber"
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Автозапуск отключён (задача '$taskName' удалена)." -ForegroundColor Yellow
} else {
    Write-Host "Задача '$taskName' не найдена — автозапуск и так не настроен." -ForegroundColor Yellow
}
