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
.\install.ps1     # поставит Python 3.12/ffmpeg при необходимости, выберет torch под вашу GPU/CPU;
                  # в конце спросит, настроить ли автозапуск при входе в Windows
# впишите HF_TOKEN в .env (для диаризации; без него работает транскрипция)
.\run.ps1
```
Открыть напечатанный URL (по умолчанию http://127.0.0.1:8473; порт меняется в `.env` → `APP_PORT`, или `.\run.ps1 9000`) или класть файлы в `inbox/`.

> Без NVIDIA GPU приложение работает на CPU (значительно медленнее). Диаризация требует бесплатный HuggingFace-токен и принятия условий `pyannote/speaker-diarization-community-1`.

Тихая установка без вопроса: `.\install.ps1 -Autostart` (с автозапуском) или `.\install.ps1 -NoAutostart`.

## Автозапуск при входе в систему (опционально)
`install.ps1` спрашивает об этом в конце. Управлять можно и отдельно, в любой момент:
```powershell
.\register-autostart.ps1     # включить автозапуск (задача Планировщика «при входе»)
.\unregister-autostart.ps1   # отключить автозапуск
Start-ScheduledTask MeetingTranscriber   # запустить сейчас, не дожидаясь входа
```
Сервер стартует скрыто (через `autostart.ps1`), второй экземпляр не плодит. Логи: `logs\server.out.log` и `logs\server.err.log`. Задача работает в вашей пользовательской сессии — это нужно, чтобы был доступ к GPU.

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
