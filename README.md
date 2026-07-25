# Meeting Transcriber

Локальный транскрибатор записей встреч: видео/аудио → диаризованная транскрипция с таймкодами (TXT/SRT/VTT/JSON/MD/DOCX) на GPU.

## Требования
- Windows + современная NVIDIA GPU (рекомендуется ≥ 8 ГБ VRAM). Установщик ставит сборку PyTorch **cu128** — это требует относительно свежей видеокарты и драйвера; на очень старых картах, выпавших из cu128-колёс, GPU задействован не будет.
- Python 3.10–3.13 и ffmpeg — `install.ps1` устанавливает их автоматически через winget, если они не найдены.
- HuggingFace-токен для диаризации (примите условия `pyannote/speaker-diarization-community-1` тем же аккаунтом, что выдал токен; без токена транскрипция работает, диаризация просто пропускается).

## Установка (Windows, рекомендуется)
```powershell
git clone <repo-url> meeting-transcriber
cd meeting-transcriber
.\install.ps1     # поставит Python 3.10–3.13 и ffmpeg при необходимости, выберет torch под вашу GPU/CPU;
                  # в конце спросит, настроить ли автозапуск при входе в Windows
# впишите HF_TOKEN в .env (для диаризации; без него работает транскрипция)
.\run.ps1
```
Открыть напечатанный URL (по умолчанию http://127.0.0.1:8473; порт меняется в `.env` → `APP_PORT`, или `.\run.ps1 9000`) или класть файлы в `inbox/`.

> Без NVIDIA GPU приложение работает на CPU (значительно медленнее). Диаризация требует бесплатный HuggingFace-токен и принятия условий `pyannote/speaker-diarization-community-1`.
>
> Основной движок — faster-whisper (CTranslate2) на NVIDIA/CUDA. PyTorch-native fallback (transformers) присутствует, но **экспериментальный** — для надёжной работы рекомендуется NVIDIA GPU.

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

## Анализ транскрипта локальной LLM (опционально)

Готовую расшифровку можно превратить в протокол, список требований или краткое резюме —
локально, без отправки текста наружу. Нужна [Ollama](https://ollama.com) и модель:

```powershell
winget install Ollama.Ollama
ollama pull qwen3:14b
```

После этого в карточке готовой встречи появятся выбор шаблона и кнопка «Анализ».
Шаблоны редактируются в разделе «Шаблоны анализа» веб-интерфейса.

Выключить фичу целиком: `ANALYZE_ENABLED=false` в `.env` (тогда Ollama не нужна).

### Настройки демона Ollama (важно для 16 ГБ VRAM)

Две переменные задаются **демону Ollama**, а не приложению — приложение их выставить не может.
Без них KV-кэш занимает вдвое больше видеопамяти и окно в 32k токенов не помещается:

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_FLASH_ATTENTION", "1", "User")
[Environment]::SetEnvironmentVariable("OLLAMA_KV_CACHE_TYPE", "q8_0", "User")
# перезапустить Ollama, чтобы переменные подхватились
```

Если модель всё равно не влезла в VRAM целиком — это **не ошибка**: Ollama досчитает часть
слоёв на процессоре, анализ просто пойдёт медленнее. Предупреждение об этом появится
рядом с кнопкой «Анализ».

## Тесты
```bash
.venv/Scripts/python -m pytest -q
```
