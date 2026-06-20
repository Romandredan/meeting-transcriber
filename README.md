# Meeting Transcriber

Локальный транскрибатор записей встреч: видео/аудио → диаризованная транскрипция с таймкодами (TXT/SRT/VTT/JSON/MD/DOCX) на GPU.

## Требования
- Windows, NVIDIA GPU (проверено на RTX 5070 Ti, 16 ГБ).
- Python 3.10–3.13 (отдельно от системного 3.14).
- ffmpeg в PATH.
- HuggingFace-токен для диаризации (примите условия `pyannote/speaker-diarization-3.1`).

## Установка
```bash
py -3.12 -m venv .venv
.venv/Scripts/pip install --upgrade pip
.venv/Scripts/pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
.venv/Scripts/pip install -r requirements-ml.txt -r requirements-base.txt
copy .env.example .env   # вписать HF_TOKEN
```

## Проверка GPU (рекомендуется до первого запуска)
```bash
.venv/Scripts/python scripts/spike_gpu.py
```

## Запуск
```bash
.venv/Scripts/python -m app.main
```
Открыть напечатанный URL (по умолчанию http://127.0.0.1:8473; порт меняется в `.env` → `APP_PORT`). Либо просто класть файлы в `inbox/`.

## Тесты
```bash
.venv/Scripts/python -m pytest -q
```
