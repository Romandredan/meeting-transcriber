# Meeting Transcriber

Локальный транскрибатор записей встреч: видео/аудио → диаризованная транскрипция с таймкодами (TXT/SRT/VTT/JSON/MD/DOCX) на GPU.

## Требования
- Windows, NVIDIA GPU (проверено на RTX 5070 Ti, 16 ГБ).
- Python 3.10–3.13 и ffmpeg — `install.ps1` устанавливает их автоматически через winget, если они не найдены.
- HuggingFace-токен для диаризации (примите условия `pyannote/speaker-diarization-community-1` тем же аккаунтом, что выдал токен; без токена транскрипция работает, диаризация просто пропускается).

## Установка (Windows, рекомендуется)
```powershell
git clone <repo-url> meeting-transcriber
cd meeting-transcriber
.\install.ps1     # поставит Python 3.12/ffmpeg при необходимости, выберет torch под вашу GPU/CPU
# впишите HF_TOKEN в .env (для диаризации; без него работает транскрипция)
.\run.ps1
```
Открыть напечатанный URL (по умолчанию http://127.0.0.1:8473; порт меняется в `.env` → `APP_PORT`, или `.\run.ps1 9000`) или класть файлы в `inbox/`.

> Без NVIDIA GPU приложение работает на CPU (значительно медленнее). Диаризация требует бесплатный HuggingFace-токен и принятия условий `pyannote/speaker-diarization-community-1`.

## Автозапуск при входе в систему (опционально)
Чтобы сервер сам поднимался после входа в Windows (и сразу отслеживал `inbox/`), зарегистрируйте задачу Планировщика, которая запускает `autostart.ps1` (стартует сервер скрыто, логи — в `logs/`, второй экземпляр не плодит):
```powershell
$proj = (Get-Location).Path
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$proj\autostart.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "MeetingTranscriber" -Action $action -Trigger $trigger -Principal $principal -Force
```
Запустить сейчас, не дожидаясь входа: `Start-ScheduledTask MeetingTranscriber`.
Отключить автозапуск: `Unregister-ScheduledTask MeetingTranscriber -Confirm:$false`.
Логи сервера: `logs\server.out.log` и `logs\server.err.log`.

<details><summary>Ручная установка (если скрипт не подошёл)</summary>

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install --upgrade pip
.venv\Scripts\pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
.venv\Scripts\pip install -r requirements-ml.txt -r requirements-base.txt
copy .env.example .env
```
</details>

## Тесты
```bash
.venv/Scripts/python -m pytest -q
```
