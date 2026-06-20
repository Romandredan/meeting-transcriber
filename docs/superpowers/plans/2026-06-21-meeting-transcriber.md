# Meeting Transcriber — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Локальное single-user веб-приложение, которое автоматически превращает видео-/аудиозаписи встреч в диаризованную транскрипцию с таймкодами (TXT/SRT/VTT/JSON/MD/DOCX) на GPU.

**Architecture:** Один постоянно работающий процесс FastAPI: раздаёт SPA, держит SQLite-очередь, фоновый GPU-воркер (1 файл за раз) и файловый watcher за папкой `inbox/`. Движок — faster-whisper (нативные word-таймкоды) + pyannote-диаризация + своя сшивка слов со спикерами; PyTorch-native fallback, если CTranslate2 не возьмёт Blackwell. Подача файлов — по локальному пути (без byte-upload).

**Tech Stack:** Python 3.12 (venv для ML), FastAPI + Uvicorn, vanilla-JS SPA, SQLite, faster-whisper + pyannote.audio (+ transformers как fallback), torch cu128, ffmpeg, python-docx, watchdog.

## Global Constraints

- **Python ML-окружение:** отдельный venv на **Python 3.12** (системный 3.14 не имеет wheels ML-стека). Все ML-зависимости ставятся в него.
- **GPU:** NVIDIA RTX 5070 Ti, 16 ГБ, Blackwell **sm_120** → PyTorch ставится из индекса **cu128** (`--index-url https://download.pytorch.org/whl/cu128`).
- **Инференс:** `device="cuda"`, `compute_type="float16"`. Тихий откат на CPU считается провалом.
- **Аудио для Whisper:** ровно `16 kHz mono PCM s16le` (`ffmpeg -ar 16000 -ac 1 -c:a pcm_s16le`). Не менять — это нативный вход Whisper.
- **Очередь:** строго последовательная обработка, один файл за раз (одна модель грузит GPU целиком).
- **Подача файлов:** по локальному пути или через watched-папку `inbox/`. Byte-upload в первой версии не делаем.
- **Словарь:** маппится на Whisper `initial_prompt`, лимит ~224 токена — биас, не гарантия. В UI рядом с полем — предупреждение.
- **Диаризация:** pyannote `speaker-diarization-3.1`, требует `HF_TOKEN` (env). Опциональна (тумблер).
- **Язык кода/комментариев:** комментарии и строки UI — русские; идентификаторы — английские.
- **Стиль:** DRY, YAGNI, TDD, частые коммиты. Чистые функции тестируются строго; GPU-инференс — smoke-тестами.

---

## File Structure

```
meeting-transcriber/
  requirements-base.txt        # сервер: fastapi, uvicorn, watchdog, python-docx
  requirements-ml.txt          # ML: torch(cu128), ctranslate2, faster-whisper, pyannote.audio, transformers
  .env.example                 # HF_TOKEN=...
  .gitignore
  README.md
  scripts/
    spike_gpu.py               # задача №1: проверка CT2/pyannote на Blackwell
  app/
    __init__.py
    config.py                  # пути, чтение .env, дефолты
    models.py                  # Word, Segment, TranscriptResult, JobStatus, Settings
    db.py                      # SQLite: схема, соединение
    job_queue.py               # enqueue/claim/update/recovery
    ffmpeg_tool.py             # детект ffmpeg + извлечение аудио
    stitch.py                  # чистая сшивка слов со спикерами
    diarize.py                 # обёртка pyannote
    engine/
      __init__.py
      base.py                  # TranscriptEngine (протокол) + normalize-хелперы
      faster_whisper_engine.py # основной
      transformers_engine.py   # fallback
      factory.py               # выбор движка
    writers.py                 # txt/srt/vtt/json/md/docx
    progress.py                # SSE-брокер прогресса
    worker.py                  # фоновый поток-конвейер
    watcher.py                 # watchdog за inbox/
    api.py                     # FastAPI-роуты
    main.py                    # сборка приложения, старт воркера+watcher
  web/
    index.html
    app.js
    style.css
  tests/
    conftest.py
    test_models.py
    test_db_queue.py
    test_ffmpeg_tool.py
    test_stitch.py
    test_writers.py
    test_progress.py
    test_watcher.py
    test_worker.py
    test_api.py
```

Каждый модуль — одна ответственность. `engine/*` прячет выбор бэкенда за протоколом `TranscriptEngine`, чтобы fallback не протекал в воркер. Чистая логика (`stitch`, `writers`, `job_queue`, `ffmpeg_tool` arg-building, `watcher` стабилизация) полностью покрыта unit-тестами; GPU-инференс — `scripts/spike_gpu.py` и ручной end-to-end.

---

## Task 0: Project scaffold & dependencies

**Files:**
- Create: `requirements-base.txt`, `requirements-ml.txt`, `.env.example`, `.gitignore`, `app/__init__.py`, `app/engine/__init__.py`, `tests/conftest.py`

**Interfaces:**
- Produces: установленное окружение и структура каталогов для всех последующих задач.

- [ ] **Step 1: Создать `.gitignore`**

```gitignore
__pycache__/
*.pyc
.venv/
.env
inbox/
output/
tmp/
data.db
models/
.pytest_cache/
```

- [ ] **Step 2: Создать `requirements-base.txt`**

```text
fastapi==0.115.*
uvicorn[standard]==0.34.*
watchdog==5.*
python-docx==1.1.*
pytest==8.*
httpx==0.28.*
```

- [ ] **Step 3: Создать `requirements-ml.txt`** (ставится в venv 3.12 ПОСЛЕ torch cu128)

```text
# Перед этим: pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
ctranslate2>=4.5.0
faster-whisper>=1.1.0
pyannote.audio>=3.3.0
transformers>=4.46.0
```

- [ ] **Step 4: Создать `.env.example`**

```text
# Токен HuggingFace для pyannote (примите условия pyannote/speaker-diarization-3.1)
HF_TOKEN=
```

- [ ] **Step 5: Создать пустые `app/__init__.py`, `app/engine/__init__.py`, `tests/conftest.py`**

`tests/conftest.py`:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

- [ ] **Step 6: Создать venv и поставить зависимости**

Run:
```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install --upgrade pip
.venv/Scripts/pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
.venv/Scripts/pip install -r requirements-ml.txt
.venv/Scripts/pip install -r requirements-base.txt
```
Expected: установка без ошибок. (Если `py -3.12` нет — поставить Python 3.12 с python.org.)

- [ ] **Step 7: Commit**

```bash
git add .gitignore requirements-base.txt requirements-ml.txt .env.example app tests
git commit -m "chore: каркас проекта и зависимости (venv 3.12, torch cu128)"
```

---

## Task 1: GPU de-risking spike (ГЕЙТ движка)

**Files:**
- Create: `scripts/spike_gpu.py`, `docs/superpowers/spike-result.md`

**Interfaces:**
- Produces: подтверждённый выбор движка. Если CT2 на cuda работает → основной путь `faster_whisper_engine`. Если падает (`no kernel image` / `Could not load cudnn`) → активируем `transformers_engine` (Task 9) как основной.

- [ ] **Step 1: Написать `scripts/spike_gpu.py`**

```python
"""De-risking spike: проверяет, что faster-whisper и pyannote реально работают на GPU (Blackwell sm_120).

Запуск:  .venv/Scripts/python scripts/spike_gpu.py [путь_к_короткому_аудио_или_видео]
Если путь не задан — генерирует 10 сек тонового WAV через ffmpeg (проверяет только GPU-путь, не качество).
"""
import subprocess
import sys
import tempfile
from pathlib import Path


def make_tone(path: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", path],
        check=True, capture_output=True,
    )


def main() -> int:
    import torch
    print(f"torch={torch.__version__} cuda_available={torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"device={torch.cuda.get_device_name(0)} capability={torch.cuda.get_device_capability(0)}")
    if not torch.cuda.is_available():
        print("FAIL: CUDA недоступна для PyTorch")
        return 1

    tmp = tempfile.mkdtemp()
    audio = sys.argv[1] if len(sys.argv) > 1 else str(Path(tmp) / "tone.wav")
    if len(sys.argv) <= 1:
        make_tone(audio)

    # --- faster-whisper на GPU ---
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("large-v3", device="cuda", compute_type="float16")
        segments, info = model.transcribe(audio, language="ru", word_timestamps=True)
        segs = list(segments)
        print(f"OK faster-whisper: lang={info.language} segments={len(segs)}")
        ct2_ok = True
    except Exception as e:
        print(f"FAIL faster-whisper на cuda: {type(e).__name__}: {e}")
        ct2_ok = False

    # --- pyannote на GPU ---
    try:
        import os
        from pyannote.audio import Pipeline
        token = os.environ.get("HF_TOKEN")
        if not token:
            print("SKIP pyannote: нет HF_TOKEN")
            pyannote_ok = None
        else:
            pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=token)
            pipe.to(torch.device("cuda"))
            diar = pipe(audio)
            print(f"OK pyannote: turns={len(list(diar.itertracks()))}")
            pyannote_ok = True
    except Exception as e:
        print(f"FAIL pyannote на cuda: {type(e).__name__}: {e}")
        pyannote_ok = False

    print("\n=== ИТОГ ===")
    print(f"faster-whisper(CT2) GPU: {'OK' if ct2_ok else 'FAIL → использовать transformers_engine'}")
    print(f"pyannote GPU: {pyannote_ok}")
    return 0 if ct2_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Запустить spike**

Run: `.venv/Scripts/python scripts/spike_gpu.py`
В отдельном окне во время прогона: `nvidia-smi` — должна расти загрузка GPU/VRAM.
Expected: `OK faster-whisper` и (при наличии HF_TOKEN) `OK pyannote`. При `FAIL ... no kernel image is available` или `Could not load cudnn` — см. Step 3.

- [ ] **Step 3: Если cuDNN-ошибка — доставить cuDNN и повторить**

Run: `.venv/Scripts/pip install nvidia-cudnn-cu12`
Повторить Step 2. Если faster-whisper всё равно FAIL на cuda — зафиксировать в spike-result и пометить `transformers_engine` основным (Task 9 обязателен, Task 8 — резерв).

- [ ] **Step 4: Записать результат в `docs/superpowers/spike-result.md`**

Содержимое: дата, вывод spike (скопировать), вердикт «основной движок = faster-whisper | transformers», нужен ли был `nvidia-cudnn-cu12`.

- [ ] **Step 5: Commit**

```bash
git add scripts/spike_gpu.py docs/superpowers/spike-result.md
git commit -m "feat: GPU de-risking spike + зафиксирован выбор движка"
```

---

## Task 2: Data models

**Files:**
- Create: `app/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces:
  - `Word(start: float, end: float, text: str, speaker: str|None=None, score: float|None=None)`
  - `Segment(start: float, end: float, text: str, speaker: str|None=None, words: list[Word]=[])`
  - `TranscriptResult(language: str, duration: float, model: str, diarized: bool, segments: list[Segment])` с методом `to_dict() -> dict`
  - `class JobStatus(str, Enum)`: `QUEUED/PROCESSING/DONE/ERROR`
  - `Settings(model="large-v3-turbo", language: str|None=None, diarize=True, num_speakers: int|None=None, vocabulary="")` с `to_dict()`/`from_dict()`

- [ ] **Step 1: Написать тест `tests/test_models.py`**

```python
from app.models import Word, Segment, TranscriptResult, JobStatus, Settings


def test_transcript_result_to_dict_roundtrip():
    w = Word(start=0.0, end=0.5, text="привет", speaker="SPEAKER_00", score=0.9)
    s = Segment(start=0.0, end=0.5, text="привет", speaker="SPEAKER_00", words=[w])
    r = TranscriptResult(language="ru", duration=0.5, model="large-v3-turbo", diarized=True, segments=[s])
    d = r.to_dict()
    assert d["language"] == "ru"
    assert d["segments"][0]["words"][0]["text"] == "привет"
    assert d["segments"][0]["speaker"] == "SPEAKER_00"


def test_settings_from_dict_defaults():
    st = Settings.from_dict({"model": "large-v3", "diarize": False})
    assert st.model == "large-v3"
    assert st.diarize is False
    assert st.language is None
    assert st.vocabulary == ""


def test_job_status_values():
    assert JobStatus.QUEUED.value == "queued"
    assert JobStatus.DONE == "done"
```

- [ ] **Step 2: Запустить — упадёт**

Run: `.venv/Scripts/python -m pytest tests/test_models.py -v`
Expected: FAIL (`ModuleNotFoundError: app.models`).

- [ ] **Step 3: Написать `app/models.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"


@dataclass
class Word:
    start: float
    end: float
    text: str
    speaker: str | None = None
    score: float | None = None


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[Word] = field(default_factory=list)


@dataclass
class TranscriptResult:
    language: str
    duration: float
    model: str
    diarized: bool
    segments: list[Segment]

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "duration": self.duration,
            "model": self.model,
            "diarized": self.diarized,
            "segments": [asdict(s) for s in self.segments],
        }


@dataclass
class Settings:
    model: str = "large-v3-turbo"
    language: str | None = None
    diarize: bool = True
    num_speakers: int | None = None
    vocabulary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        return cls(
            model=data.get("model", "large-v3-turbo"),
            language=data.get("language"),
            diarize=data.get("diarize", True),
            num_speakers=data.get("num_speakers"),
            vocabulary=data.get("vocabulary", ""),
        )
```

- [ ] **Step 4: Запустить — пройдёт**

Run: `.venv/Scripts/python -m pytest tests/test_models.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_models.py
git commit -m "feat: модели данных (Word/Segment/TranscriptResult/Job/Settings)"
```

---

## Task 3: Config

**Files:**
- Create: `app/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: модуль `config` с путями `BASE_DIR, INBOX_DIR, OUTPUT_DIR, TMP_DIR, DB_PATH, MODELS_DIR` (`pathlib.Path`), `HF_TOKEN: str|None`, функция `ensure_dirs() -> None` (создаёт inbox/output/tmp/models), `load_env() -> None`.

- [ ] **Step 1: Тест `tests/test_config.py`**

```python
from app import config


def test_paths_under_base():
    assert config.INBOX_DIR.name == "inbox"
    assert config.OUTPUT_DIR.name == "output"
    assert config.DB_PATH.name == "data.db"


def test_ensure_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")
    config.ensure_dirs()
    assert (tmp_path / "inbox").is_dir()
    assert (tmp_path / "output").is_dir()
```

- [ ] **Step 2: Запустить — упадёт.** Run: `.venv/Scripts/python -m pytest tests/test_config.py -v` → FAIL.

- [ ] **Step 3: Написать `app/config.py`**

```python
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
INBOX_DIR = BASE_DIR / "inbox"
OUTPUT_DIR = BASE_DIR / "output"
TMP_DIR = BASE_DIR / "tmp"
MODELS_DIR = BASE_DIR / "models"
DB_PATH = BASE_DIR / "data.db"


def load_env() -> None:
    """Читает .env (KEY=VALUE) в os.environ, не перезаписывая существующее."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


load_env()
HF_TOKEN = os.environ.get("HF_TOKEN") or None


def ensure_dirs() -> None:
    for d in (INBOX_DIR, OUTPUT_DIR, TMP_DIR, MODELS_DIR):
        d.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Запустить — пройдёт.** Run: `.venv/Scripts/python -m pytest tests/test_config.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat: конфиг (пути, .env, HF_TOKEN, ensure_dirs)"
```

---

## Task 4: SQLite layer + job queue

**Files:**
- Create: `app/db.py`, `app/job_queue.py`
- Test: `tests/test_db_queue.py`

**Interfaces:**
- `db.connect(path) -> sqlite3.Connection` (row_factory=Row, `init_schema` идемпотентно)
- `job_queue.enqueue(conn, source_path: str, settings_json: str) -> int` (возвращает id; статус `queued`)
- `job_queue.claim_next(conn) -> sqlite3.Row | None` (берёт самый старый `queued`, ставит `processing`, возвращает строку)
- `job_queue.update(conn, job_id: int, *, status=None, progress=None, stage=None, error=None, output_dir=None) -> None`
- `job_queue.get(conn, job_id: int) -> sqlite3.Row | None`
- `job_queue.list_jobs(conn, limit=200) -> list[sqlite3.Row]` (новые сверху)
- `job_queue.recover_stuck(conn) -> int` (все `processing` → `queued`, прогресс 0; возвращает count)

Колонки `jobs`: `id INTEGER PK, source_path TEXT, filename TEXT, status TEXT, progress REAL, stage TEXT, error TEXT, settings_json TEXT, output_dir TEXT, created_at TEXT`.

- [ ] **Step 1: Тест `tests/test_db_queue.py`**

```python
from app import db, job_queue


def make_conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def test_enqueue_and_get(tmp_path):
    conn = make_conn(tmp_path)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    row = job_queue.get(conn, jid)
    assert row["status"] == "queued"
    assert row["filename"] == "a.mp4"


def test_claim_next_sets_processing_and_is_fifo(tmp_path):
    conn = make_conn(tmp_path)
    a = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    b = job_queue.enqueue(conn, "C:/v/b.mp4", "{}")
    first = job_queue.claim_next(conn)
    assert first["id"] == a
    assert first["status"] == "processing"
    second = job_queue.claim_next(conn)
    assert second["id"] == b
    assert job_queue.claim_next(conn) is None


def test_update_fields(tmp_path):
    conn = make_conn(tmp_path)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_queue.update(conn, jid, status="done", progress=1.0, stage="write", output_dir="output/1")
    row = job_queue.get(conn, jid)
    assert row["status"] == "done"
    assert row["progress"] == 1.0
    assert row["output_dir"] == "output/1"


def test_recover_stuck(tmp_path):
    conn = make_conn(tmp_path)
    job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_queue.claim_next(conn)
    assert job_queue.recover_stuck(conn) == 1
    row = job_queue.claim_next(conn)
    assert row is not None  # снова queued
```

- [ ] **Step 2: Запустить — упадёт.** Run: `.venv/Scripts/python -m pytest tests/test_db_queue.py -v` → FAIL.

- [ ] **Step 3: Написать `app/db.py`**

```python
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    filename    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    progress    REAL NOT NULL DEFAULT 0,
    stage       TEXT NOT NULL DEFAULT '',
    error       TEXT,
    settings_json TEXT NOT NULL DEFAULT '{}',
    output_dir  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    data    TEXT NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
```

- [ ] **Step 4: Написать `app/job_queue.py`**

```python
from __future__ import annotations

import os
import sqlite3


def enqueue(conn: sqlite3.Connection, source_path: str, settings_json: str) -> int:
    filename = os.path.basename(source_path)
    cur = conn.execute(
        "INSERT INTO jobs (source_path, filename, settings_json) VALUES (?, ?, ?)",
        (source_path, filename, settings_json),
    )
    conn.commit()
    return int(cur.lastrowid)


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE jobs SET status='processing', stage='', progress=0 WHERE id=?",
        (row["id"],),
    )
    conn.commit()
    return conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()


def update(conn: sqlite3.Connection, job_id: int, *, status=None, progress=None,
           stage=None, error=None, output_dir=None) -> None:
    fields, values = [], []
    for name, val in (("status", status), ("progress", progress), ("stage", stage),
                      ("error", error), ("output_dir", output_dir)):
        if val is not None:
            fields.append(f"{name}=?")
            values.append(val)
    if not fields:
        return
    values.append(job_id)
    conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", values)
    conn.commit()


def get(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def list_jobs(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def recover_stuck(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "UPDATE jobs SET status='queued', stage='', progress=0 WHERE status='processing'"
    )
    conn.commit()
    return cur.rowcount
```

- [ ] **Step 5: Запустить — пройдёт.** Run: `.venv/Scripts/python -m pytest tests/test_db_queue.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/db.py app/job_queue.py tests/test_db_queue.py
git commit -m "feat: SQLite-слой и очередь job'ов (enqueue/claim/update/recovery)"
```

---

## Task 5: ffmpeg tool

**Files:**
- Create: `app/ffmpeg_tool.py`
- Test: `tests/test_ffmpeg_tool.py`

**Interfaces:**
- `ffmpeg_tool.find_ffmpeg() -> str | None` (путь к ffmpeg из PATH через `shutil.which`)
- `ffmpeg_tool.build_extract_cmd(src: str, dst: str, ffmpeg: str="ffmpeg") -> list[str]` (16k mono pcm_s16le)
- `ffmpeg_tool.extract_audio(src: str, dst: str) -> None` (вызывает ffmpeg; кидает `FFmpegError` с понятным текстом при отсутствии ffmpeg / ошибке / отсутствии аудиодорожки)
- `class FFmpegError(Exception)`

- [ ] **Step 1: Тест `tests/test_ffmpeg_tool.py`**

```python
import pytest
from app import ffmpeg_tool


def test_build_extract_cmd_has_required_audio_params():
    cmd = ffmpeg_tool.build_extract_cmd("in.mp4", "out.wav", ffmpeg="ffmpeg")
    assert "-ar" in cmd and "16000" in cmd
    assert "-ac" in cmd and "1" in cmd
    assert "pcm_s16le" in cmd
    assert cmd[-1] == "out.wav"
    assert "in.mp4" in cmd


def test_extract_audio_raises_when_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(ffmpeg_tool, "find_ffmpeg", lambda: None)
    with pytest.raises(ffmpeg_tool.FFmpegError) as e:
        ffmpeg_tool.extract_audio("in.mp4", "out.wav")
    assert "ffmpeg" in str(e.value).lower()
```

- [ ] **Step 2: Запустить — упадёт.** Run: `.venv/Scripts/python -m pytest tests/test_ffmpeg_tool.py -v` → FAIL.

- [ ] **Step 3: Написать `app/ffmpeg_tool.py`**

```python
from __future__ import annotations

import shutil
import subprocess


class FFmpegError(Exception):
    pass


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def build_extract_cmd(src: str, dst: str, ffmpeg: str = "ffmpeg") -> list[str]:
    return [
        ffmpeg, "-y", "-i", src,
        "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        dst,
    ]


def extract_audio(src: str, dst: str) -> None:
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise FFmpegError(
            "ffmpeg не найден в PATH. Установите ffmpeg или укажите путь к нему."
        )
    cmd = build_extract_cmd(src, dst, ffmpeg)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise FFmpegError("Ошибка извлечения аудио: " + " | ".join(tail))
```

- [ ] **Step 4: Запустить — пройдёт.** Run: `.venv/Scripts/python -m pytest tests/test_ffmpeg_tool.py -v` → PASS.

- [ ] **Step 5: Интеграционная проверка (вручную, ffmpeg уже стоит)**

Run:
```bash
ffmpeg -y -f lavfi -i sine=frequency=440:duration=3 -ar 44100 in_test.wav
.venv/Scripts/python -c "from app import ffmpeg_tool; ffmpeg_tool.extract_audio('in_test.wav','out_test.wav'); print('ok')"
```
Expected: `ok`, файл `out_test.wav` создан. Затем удалить `in_test.wav out_test.wav`.

- [ ] **Step 6: Commit**

```bash
git add app/ffmpeg_tool.py tests/test_ffmpeg_tool.py
git commit -m "feat: ffmpeg-инструмент (детект + извлечение 16k mono WAV)"
```

---

## Task 6: Speaker stitching (чистая логика)

**Files:**
- Create: `app/stitch.py`
- Test: `tests/test_stitch.py`

**Interfaces:**
- `stitch.assign_speakers(words: list[Word], turns: list[tuple[float, float, str]]) -> None` — мутирует `Word.speaker` по максимальному перекрытию интервала слова с интервалами диаризации `(start, end, speaker_label)`.
- `stitch.segment_speaker(words: list[Word]) -> str | None` — доминирующий спикер сегмента (по суммарной длительности слов).
- `stitch.humanize_speaker(label: str | None) -> str` — `"SPEAKER_00"` → `"Спикер 1"`, `None` → `"Спикер ?"`.

- [ ] **Step 1: Тест `tests/test_stitch.py`**

```python
from app.models import Word
from app import stitch


def test_assign_speakers_by_overlap():
    words = [Word(0.0, 1.0, "a"), Word(1.2, 2.0, "b")]
    turns = [(0.0, 1.0, "SPEAKER_00"), (1.1, 3.0, "SPEAKER_01")]
    stitch.assign_speakers(words, turns)
    assert words[0].speaker == "SPEAKER_00"
    assert words[1].speaker == "SPEAKER_01"


def test_assign_speakers_no_overlap_leaves_none():
    words = [Word(5.0, 6.0, "x")]
    turns = [(0.0, 1.0, "SPEAKER_00")]
    stitch.assign_speakers(words, turns)
    assert words[0].speaker is None


def test_segment_speaker_dominant():
    words = [Word(0, 1, "a", speaker="SPEAKER_00"),
             Word(1, 3, "b", speaker="SPEAKER_01"),
             Word(3, 3.5, "c", speaker="SPEAKER_01")]
    assert stitch.segment_speaker(words) == "SPEAKER_01"


def test_humanize_speaker():
    assert stitch.humanize_speaker("SPEAKER_00") == "Спикер 1"
    assert stitch.humanize_speaker("SPEAKER_05") == "Спикер 6"
    assert stitch.humanize_speaker(None) == "Спикер ?"
```

- [ ] **Step 2: Запустить — упадёт.** Run: `.venv/Scripts/python -m pytest tests/test_stitch.py -v` → FAIL.

- [ ] **Step 3: Написать `app/stitch.py`**

```python
from __future__ import annotations

from collections import defaultdict

from app.models import Word


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_speakers(words: list[Word], turns: list[tuple[float, float, str]]) -> None:
    for w in words:
        best_label, best_ov = None, 0.0
        for t_start, t_end, label in turns:
            ov = _overlap(w.start, w.end, t_start, t_end)
            if ov > best_ov:
                best_ov, best_label = ov, label
        if best_label is not None:
            w.speaker = best_label


def segment_speaker(words: list[Word]) -> str | None:
    totals: dict[str, float] = defaultdict(float)
    for w in words:
        if w.speaker:
            totals[w.speaker] += max(0.0, w.end - w.start)
    if not totals:
        return None
    return max(totals, key=totals.get)


def humanize_speaker(label: str | None) -> str:
    if not label:
        return "Спикер ?"
    digits = "".join(ch for ch in label if ch.isdigit())
    if digits == "":
        return label
    return f"Спикер {int(digits) + 1}"
```

- [ ] **Step 4: Запустить — пройдёт.** Run: `.venv/Scripts/python -m pytest tests/test_stitch.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/stitch.py tests/test_stitch.py
git commit -m "feat: сшивка слов со спикерами (перекрытие, доминирующий, humanize)"
```

---

## Task 7: Writers

**Files:**
- Create: `app/writers.py`
- Test: `tests/test_writers.py`

**Interfaces:**
- `writers.format_timestamp(seconds: float, sep: str=",") -> str` → `"00:01:02,500"` (sep `,` для SRT, `.` для VTT).
- `writers.write_all(result: TranscriptResult, out_dir: str, formats: list[str], basename: str) -> list[str]` — пишет выбранные форматы (`txt/srt/vtt/json/md/docx`), возвращает список путей.
- Внутренние: `to_txt/to_srt/to_vtt/to_json/to_md(result) -> str`, `to_docx(result, path) -> None`.
- TXT-строка сегмента: `[HH:MM:SS] Спикер N: текст`.

- [ ] **Step 1: Тест `tests/test_writers.py`**

```python
import json
from app.models import Word, Segment, TranscriptResult
from app import writers


def sample():
    w = [Word(0.0, 1.0, "привет", speaker="SPEAKER_00"),
         Word(1.0, 2.0, "мир", speaker="SPEAKER_00")]
    s = Segment(0.0, 2.0, "привет мир", speaker="SPEAKER_00", words=w)
    return TranscriptResult("ru", 2.0, "large-v3-turbo", True, [s])


def test_format_timestamp_srt():
    assert writers.format_timestamp(62.5, ",") == "00:01:02,500"


def test_format_timestamp_vtt():
    assert writers.format_timestamp(62.5, ".") == "00:01:02.500"


def test_to_txt_has_speaker_and_time():
    txt = writers.to_txt(sample())
    assert "[00:00:00]" in txt
    assert "Спикер 1:" in txt
    assert "привет мир" in txt


def test_to_srt_structure():
    srt = writers.to_srt(sample())
    assert "1\n" in srt
    assert "00:00:00,000 --> 00:00:02,000" in srt


def test_to_json_valid():
    data = json.loads(writers.to_json(sample()))
    assert data["language"] == "ru"
    assert data["segments"][0]["text"] == "привет мир"


def test_write_all_creates_files(tmp_path):
    paths = writers.write_all(sample(), str(tmp_path), ["txt", "srt", "json"], "meeting")
    names = sorted(p.split("/")[-1].split("\\")[-1] for p in paths)
    assert names == ["meeting.json", "meeting.srt", "meeting.txt"]
    for p in paths:
        assert open(p, encoding="utf-8").read()
```

- [ ] **Step 2: Запустить — упадёт.** Run: `.venv/Scripts/python -m pytest tests/test_writers.py -v` → FAIL.

- [ ] **Step 3: Написать `app/writers.py`**

```python
from __future__ import annotations

import json
import os

from app.models import TranscriptResult, Segment
from app.stitch import humanize_speaker


def format_timestamp(seconds: float, sep: str = ",") -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _hms(seconds: float) -> str:
    return format_timestamp(seconds, ",")[:8]


def _seg_label(seg: Segment) -> str:
    return humanize_speaker(seg.speaker)


def to_txt(result: TranscriptResult) -> str:
    lines = []
    for seg in result.segments:
        lines.append(f"[{_hms(seg.start)}] {_seg_label(seg)}: {seg.text.strip()}")
    return "\n".join(lines) + "\n"


def to_srt(result: TranscriptResult) -> str:
    blocks = []
    for i, seg in enumerate(result.segments, 1):
        ts = f"{format_timestamp(seg.start, ',')} --> {format_timestamp(seg.end, ',')}"
        text = f"{_seg_label(seg)}: {seg.text.strip()}"
        blocks.append(f"{i}\n{ts}\n{text}\n")
    return "\n".join(blocks)


def to_vtt(result: TranscriptResult) -> str:
    blocks = ["WEBVTT\n"]
    for seg in result.segments:
        ts = f"{format_timestamp(seg.start, '.')} --> {format_timestamp(seg.end, '.')}"
        text = f"{_seg_label(seg)}: {seg.text.strip()}"
        blocks.append(f"{ts}\n{text}\n")
    return "\n".join(blocks)


def to_json(result: TranscriptResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)


def to_md(result: TranscriptResult) -> str:
    lines = [f"# Транскрипция встречи\n",
             f"- Язык: {result.language}",
             f"- Модель: {result.model}",
             f"- Диаризация: {'да' if result.diarized else 'нет'}\n"]
    for seg in result.segments:
        lines.append(f"**[{_hms(seg.start)}] {_seg_label(seg)}:** {seg.text.strip()}\n")
    return "\n".join(lines)


def to_docx(result: TranscriptResult, path: str) -> None:
    from docx import Document
    doc = Document()
    doc.add_heading("Транскрипция встречи", level=1)
    doc.add_paragraph(f"Язык: {result.language} · Модель: {result.model} · "
                      f"Диаризация: {'да' if result.diarized else 'нет'}")
    for seg in result.segments:
        p = doc.add_paragraph()
        p.add_run(f"[{_hms(seg.start)}] {_seg_label(seg)}: ").bold = True
        p.add_run(seg.text.strip())
    doc.save(path)


def write_all(result: TranscriptResult, out_dir: str, formats: list[str],
              basename: str) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []
    text_map = {"txt": to_txt, "srt": to_srt, "vtt": to_vtt, "json": to_json, "md": to_md}
    for fmt in formats:
        path = os.path.join(out_dir, f"{basename}.{fmt}")
        if fmt in text_map:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text_map[fmt](result))
            written.append(path)
        elif fmt == "docx":
            to_docx(result, path)
            written.append(path)
    return written
```

- [ ] **Step 4: Запустить — пройдёт.** Run: `.venv/Scripts/python -m pytest tests/test_writers.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/writers.py tests/test_writers.py
git commit -m "feat: writers (txt/srt/vtt/json/md/docx)"
```

---

## Task 8: Diarize wrapper + engine base + faster-whisper engine

**Files:**
- Create: `app/diarize.py`, `app/engine/base.py`, `app/engine/faster_whisper_engine.py`
- Test: `tests/test_engine_base.py`

**Interfaces:**
- `engine/base.py`:
  - `class TranscriptEngine(Protocol)`: метод `transcribe(audio_path: str, settings: Settings, progress: Callable[[str, float], None]) -> TranscriptResult`
  - `truncate_prompt(vocabulary: str, max_words: int=200) -> str` — обрезает словарь под лимит initial_prompt (чистая, тестируемая).
  - `build_segments(raw_segments) -> list[Segment]` — нормализует выдачу faster-whisper (объекты с `.start/.end/.text/.words[]`) в наши `Segment/Word` (чистая, тестируемая на фейковых объектах).
- `diarize.py`: `diarize_audio(audio_path: str, num_speakers: int|None, hf_token: str|None) -> list[tuple[float,float,str]]` (обёртка pyannote; если токена нет — `raise DiarizationError`). `class DiarizationError(Exception)`.
- `engine/faster_whisper_engine.py`: `class FasterWhisperEngine` реализует `TranscriptEngine`; загружает модель лениво, кэширует; `word_timestamps=True`; при `settings.diarize` зовёт `diarize.diarize_audio` + `stitch`.

- [ ] **Step 1: Тест `tests/test_engine_base.py`** (только чистые функции — без GPU)

```python
from types import SimpleNamespace
from app.engine import base
from app.models import Segment


def test_truncate_prompt_limits_words():
    vocab = " ".join(f"термин{i}" for i in range(300))
    out = base.truncate_prompt(vocab, max_words=200)
    assert len(out.split()) == 200


def test_truncate_prompt_short_passthrough():
    assert base.truncate_prompt("АккордПост ОФД", max_words=200) == "АккордПост ОФД"


def test_build_segments_normalizes():
    raw_word = SimpleNamespace(start=0.0, end=0.5, word="привет", probability=0.9)
    raw_seg = SimpleNamespace(start=0.0, end=0.5, text=" привет", words=[raw_word])
    segs = base.build_segments([raw_seg])
    assert isinstance(segs[0], Segment)
    assert segs[0].text == " привет"
    assert segs[0].words[0].text == "привет"
    assert segs[0].words[0].score == 0.9
```

- [ ] **Step 2: Запустить — упадёт.** Run: `.venv/Scripts/python -m pytest tests/test_engine_base.py -v` → FAIL.

- [ ] **Step 3: Написать `app/engine/base.py`**

```python
from __future__ import annotations

from typing import Callable, Protocol

from app.models import Segment, Settings, TranscriptResult, Word


class TranscriptEngine(Protocol):
    def transcribe(self, audio_path: str, settings: Settings,
                   progress: Callable[[str, float], None]) -> TranscriptResult: ...


def truncate_prompt(vocabulary: str, max_words: int = 200) -> str:
    words = vocabulary.split()
    if len(words) <= max_words:
        return vocabulary.strip()
    return " ".join(words[:max_words])


def build_segments(raw_segments) -> list[Segment]:
    segments: list[Segment] = []
    for rs in raw_segments:
        words: list[Word] = []
        for rw in (getattr(rs, "words", None) or []):
            words.append(Word(
                start=float(rw.start),
                end=float(rw.end),
                text=str(getattr(rw, "word", "")).strip(),
                score=getattr(rw, "probability", None),
            ))
        segments.append(Segment(
            start=float(rs.start),
            end=float(rs.end),
            text=str(rs.text),
            words=words,
        ))
    return segments
```

- [ ] **Step 4: Написать `app/diarize.py`**

```python
from __future__ import annotations


class DiarizationError(Exception):
    pass


_PIPELINE = None


def _get_pipeline(hf_token: str | None):
    global _PIPELINE
    if hf_token is None:
        raise DiarizationError("Диаризация требует HF_TOKEN (pyannote). Укажите токен в .env.")
    if _PIPELINE is None:
        import torch
        from pyannote.audio import Pipeline
        _PIPELINE = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1", use_auth_token=hf_token
        )
        if torch.cuda.is_available():
            _PIPELINE.to(torch.device("cuda"))
    return _PIPELINE


def diarize_audio(audio_path: str, num_speakers: int | None,
                  hf_token: str | None) -> list[tuple[float, float, str]]:
    pipeline = _get_pipeline(hf_token)
    kwargs = {}
    if num_speakers:
        kwargs["num_speakers"] = num_speakers
    annotation = pipeline(audio_path, **kwargs)
    turns: list[tuple[float, float, str]] = []
    for segment, _, label in annotation.itertracks(yield_label=True):
        turns.append((float(segment.start), float(segment.end), str(label)))
    return turns
```

- [ ] **Step 5: Написать `app/engine/faster_whisper_engine.py`**

```python
from __future__ import annotations

from typing import Callable

from app import config
from app.engine.base import build_segments, truncate_prompt
from app.models import Settings, TranscriptResult
from app.stitch import assign_speakers, segment_speaker


class FasterWhisperEngine:
    name = "faster-whisper"

    def __init__(self) -> None:
        self._models: dict[str, object] = {}

    def _get_model(self, model_name: str):
        if model_name not in self._models:
            from faster_whisper import WhisperModel
            self._models[model_name] = WhisperModel(
                model_name, device="cuda", compute_type="float16",
                download_root=str(config.MODELS_DIR),
            )
        return self._models[model_name]

    def transcribe(self, audio_path: str, settings: Settings,
                   progress: Callable[[str, float], None]) -> TranscriptResult:
        progress("transcribe", 0.1)
        model = self._get_model(settings.model)
        prompt = truncate_prompt(settings.vocabulary) or None
        raw_segments, info = model.transcribe(
            audio_path,
            language=settings.language,
            initial_prompt=prompt,
            word_timestamps=True,
            vad_filter=True,
        )
        segments = build_segments(raw_segments)
        progress("transcribe", 0.6)

        diarized = False
        if settings.diarize:
            progress("diarize", 0.65)
            from app.diarize import diarize_audio
            turns = diarize_audio(audio_path, settings.num_speakers, config.HF_TOKEN)
            for seg in segments:
                assign_speakers(seg.words, turns)
                seg.speaker = segment_speaker(seg.words)
            diarized = True
        progress("diarize", 0.9)

        duration = segments[-1].end if segments else 0.0
        return TranscriptResult(
            language=info.language, duration=duration,
            model=settings.model, diarized=diarized, segments=segments,
        )
```

- [ ] **Step 6: Запустить unit-тест — пройдёт.** Run: `.venv/Scripts/python -m pytest tests/test_engine_base.py -v` → PASS.

- [ ] **Step 7: Commit**

```bash
git add app/diarize.py app/engine/base.py app/engine/faster_whisper_engine.py tests/test_engine_base.py
git commit -m "feat: движок faster-whisper + обёртка pyannote + нормализация сегментов"
```

---

## Task 9: Transformers fallback engine + factory

**Files:**
- Create: `app/engine/transformers_engine.py`, `app/engine/factory.py`
- Test: `tests/test_factory.py`

**Interfaces:**
- `engine/transformers_engine.py`: `class TransformersWhisperEngine` реализует `TranscriptEngine` через `transformers` pipeline (`return_timestamps="word"`), та же сшивка с pyannote.
- `engine/factory.py`: `make_engine(backend: str="auto") -> TranscriptEngine`. `"auto"` пытается импортировать/создать `FasterWhisperEngine` (CT2); при `ImportError`/исключении создаёт `TransformersWhisperEngine`. Явные `"faster_whisper"` / `"transformers"` форсируют.

- [ ] **Step 1: Тест `tests/test_factory.py`**

```python
from app.engine import factory


def test_factory_forces_transformers():
    eng = factory.make_engine("transformers")
    assert eng.__class__.__name__ == "TransformersWhisperEngine"


def test_factory_forces_faster_whisper():
    eng = factory.make_engine("faster_whisper")
    assert eng.__class__.__name__ == "FasterWhisperEngine"
```

- [ ] **Step 2: Запустить — упадёт.** Run: `.venv/Scripts/python -m pytest tests/test_factory.py -v` → FAIL.

- [ ] **Step 3: Написать `app/engine/transformers_engine.py`**

```python
from __future__ import annotations

from typing import Callable

from app import config
from app.engine.base import truncate_prompt
from app.models import Segment, Settings, TranscriptResult, Word
from app.stitch import assign_speakers, segment_speaker

_MODEL_MAP = {
    "large-v3-turbo": "openai/whisper-large-v3-turbo",
    "large-v3": "openai/whisper-large-v3",
}


class TransformersWhisperEngine:
    name = "transformers"

    def __init__(self) -> None:
        self._pipes: dict[str, object] = {}

    def _get_pipe(self, model_name: str):
        if model_name not in self._pipes:
            import torch
            from transformers import pipeline
            hf_id = _MODEL_MAP.get(model_name, model_name)
            self._pipes[model_name] = pipeline(
                "automatic-speech-recognition", model=hf_id,
                torch_dtype=torch.float16, device="cuda",
            )
        return self._pipes[model_name]

    def transcribe(self, audio_path: str, settings: Settings,
                   progress: Callable[[str, float], None]) -> TranscriptResult:
        progress("transcribe", 0.1)
        pipe = self._get_pipe(settings.model)
        prompt = truncate_prompt(settings.vocabulary) or None
        generate_kwargs = {"language": settings.language} if settings.language else {}
        if prompt:
            generate_kwargs["prompt"] = prompt
        out = pipe(audio_path, return_timestamps="word", chunk_length_s=30,
                   batch_size=8, generate_kwargs=generate_kwargs)
        words = [Word(start=float(c["timestamp"][0] or 0.0),
                      end=float(c["timestamp"][1] or 0.0),
                      text=c["text"].strip())
                 for c in out.get("chunks", []) if c.get("timestamp")]
        segment = Segment(start=words[0].start if words else 0.0,
                          end=words[-1].end if words else 0.0,
                          text=out.get("text", "").strip(), words=words)
        segments = [segment] if words else []
        progress("transcribe", 0.6)

        diarized = False
        if settings.diarize and segments:
            progress("diarize", 0.65)
            from app.diarize import diarize_audio
            turns = diarize_audio(audio_path, settings.num_speakers, config.HF_TOKEN)
            for seg in segments:
                assign_speakers(seg.words, turns)
                seg.speaker = segment_speaker(seg.words)
            diarized = True
        progress("diarize", 0.9)

        duration = segments[-1].end if segments else 0.0
        return TranscriptResult(language=settings.language or "ru", duration=duration,
                                model=settings.model, diarized=diarized, segments=segments)
```

> Примечание: transformers-движок отдаёт один крупный сегмент (по словам). Для протокола/сшивки этого достаточно; разбивка на реплики опирается на смену спикера в writers при необходимости (вне рамок v1).

- [ ] **Step 4: Написать `app/engine/factory.py`**

```python
from __future__ import annotations

from app.engine.base import TranscriptEngine


def make_engine(backend: str = "auto") -> TranscriptEngine:
    if backend == "transformers":
        from app.engine.transformers_engine import TransformersWhisperEngine
        return TransformersWhisperEngine()
    if backend == "faster_whisper":
        from app.engine.faster_whisper_engine import FasterWhisperEngine
        return FasterWhisperEngine()
    # auto
    try:
        import ctranslate2  # noqa: F401
        from app.engine.faster_whisper_engine import FasterWhisperEngine
        return FasterWhisperEngine()
    except Exception:
        from app.engine.transformers_engine import TransformersWhisperEngine
        return TransformersWhisperEngine()
```

- [ ] **Step 5: Запустить — пройдёт.** Run: `.venv/Scripts/python -m pytest tests/test_factory.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/engine/transformers_engine.py app/engine/factory.py tests/test_factory.py
git commit -m "feat: transformers-fallback движок + фабрика выбора бэкенда"
```

---

## Task 10: Progress broker (SSE)

**Files:**
- Create: `app/progress.py`
- Test: `tests/test_progress.py`

**Interfaces:**
- `class ProgressBroker`: `publish(job_id: int, stage: str, progress: float, status: str) -> None`; `subscribe() -> queue.Queue`; `unsubscribe(q) -> None`. Хранит последнее событие на job для late-subscribers через `snapshot() -> dict`.

- [ ] **Step 1: Тест `tests/test_progress.py`**

```python
from app.progress import ProgressBroker


def test_publish_reaches_subscriber():
    broker = ProgressBroker()
    q = broker.subscribe()
    broker.publish(1, "transcribe", 0.5, "processing")
    evt = q.get_nowait()
    assert evt["job_id"] == 1
    assert evt["stage"] == "transcribe"
    assert evt["progress"] == 0.5


def test_unsubscribe_stops_delivery():
    broker = ProgressBroker()
    q = broker.subscribe()
    broker.unsubscribe(q)
    broker.publish(1, "x", 1.0, "done")
    assert q.empty()
```

- [ ] **Step 2: Запустить — упадёт.** Run: `.venv/Scripts/python -m pytest tests/test_progress.py -v` → FAIL.

- [ ] **Step 3: Написать `app/progress.py`**

```python
from __future__ import annotations

import queue
import threading


class ProgressBroker:
    def __init__(self) -> None:
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, job_id: int, stage: str, progress: float, status: str) -> None:
        evt = {"job_id": job_id, "stage": stage, "progress": progress, "status": status}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(evt)
            except queue.Full:
                pass
```

- [ ] **Step 4: Запустить — пройдёт.** Run: `.venv/Scripts/python -m pytest tests/test_progress.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/progress.py tests/test_progress.py
git commit -m "feat: брокер прогресса для SSE"
```

---

## Task 11: Worker (конвейер)

**Files:**
- Create: `app/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- `worker.process_job(conn, broker, engine, job_row, *, settings_global: Settings, formats: list[str], tmp_dir: str, output_dir: str) -> None` — один job: ffmpeg → engine.transcribe → writers.write_all; обновляет статусы/прогресс/ошибки; merge per-job settings поверх global.
- `class Worker`: `__init__(conn, broker, engine, settings_provider, formats_provider)`; `run_forever(stop_event)` — цикл `claim_next`; `start()` стартует поток.
- `worker.merge_settings(global_s: Settings, override: dict) -> Settings` (чистая, тестируемая).

- [ ] **Step 1: Тест `tests/test_worker.py`** (движок и ffmpeg — фейки)

```python
import json
from app import db, job_queue, worker
from app.models import Settings, TranscriptResult, Segment, Word
from app.progress import ProgressBroker


class FakeEngine:
    def transcribe(self, audio_path, settings, progress):
        progress("transcribe", 0.5)
        w = Word(0.0, 1.0, "тест", speaker="SPEAKER_00")
        s = Segment(0.0, 1.0, "тест", speaker="SPEAKER_00", words=[w])
        return TranscriptResult("ru", 1.0, settings.model, True, [s])


def test_merge_settings_override():
    g = Settings(model="large-v3-turbo", diarize=True)
    merged = worker.merge_settings(g, {"model": "large-v3", "diarize": False})
    assert merged.model == "large-v3"
    assert merged.diarize is False


def test_process_job_writes_outputs_and_marks_done(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    monkeypatch.setattr(worker.ffmpeg_tool, "extract_audio", lambda src, dst: open(dst, "w").close())
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", json.dumps({"diarize": True}))
    row = job_queue.claim_next(conn)
    worker.process_job(conn, ProgressBroker(), FakeEngine(), row,
                       settings_global=Settings(), formats=["txt", "json"],
                       tmp_dir=str(tmp_path / "tmp"), output_dir=str(tmp_path / "out"))
    done = job_queue.get(conn, jid)
    assert done["status"] == "done"
    assert done["progress"] == 1.0
    assert done["output_dir"]


def test_process_job_marks_error_on_ffmpeg_failure(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    def boom(src, dst): raise worker.ffmpeg_tool.FFmpegError("нет аудио")
    monkeypatch.setattr(worker.ffmpeg_tool, "extract_audio", boom)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    row = job_queue.claim_next(conn)
    worker.process_job(conn, ProgressBroker(), FakeEngine(), row,
                       settings_global=Settings(), formats=["txt"],
                       tmp_dir=str(tmp_path / "tmp"), output_dir=str(tmp_path / "out"))
    err = job_queue.get(conn, jid)
    assert err["status"] == "error"
    assert "аудио" in err["error"]
```

- [ ] **Step 2: Запустить — упадёт.** Run: `.venv/Scripts/python -m pytest tests/test_worker.py -v` → FAIL.

- [ ] **Step 3: Написать `app/worker.py`**

```python
from __future__ import annotations

import json
import os
import threading
import time

from app import ffmpeg_tool, job_queue, writers
from app.models import Settings


def merge_settings(global_s: Settings, override: dict) -> Settings:
    base = global_s.to_dict()
    base.update({k: v for k, v in (override or {}).items() if v is not None})
    return Settings.from_dict(base)


def process_job(conn, broker, engine, job_row, *, settings_global: Settings,
                formats: list[str], tmp_dir: str, output_dir: str) -> None:
    job_id = job_row["id"]
    override = json.loads(job_row["settings_json"] or "{}")
    settings = merge_settings(settings_global, override)
    os.makedirs(tmp_dir, exist_ok=True)
    job_out = os.path.join(output_dir, str(job_id))

    def report(stage: str, progress: float) -> None:
        job_queue.update(conn, job_id, stage=stage, progress=progress)
        broker.publish(job_id, stage, progress, "processing")

    try:
        report("audio", 0.02)
        wav = os.path.join(tmp_dir, f"{job_id}.wav")
        ffmpeg_tool.extract_audio(job_row["source_path"], wav)

        result = engine.transcribe(wav, settings, report)

        report("write", 0.95)
        basename = os.path.splitext(job_row["filename"])[0]
        writers.write_all(result, job_out, formats, basename)

        job_queue.update(conn, job_id, status="done", progress=1.0,
                         stage="write", output_dir=job_out)
        broker.publish(job_id, "write", 1.0, "done")
    except Exception as e:
        job_queue.update(conn, job_id, status="error", error=f"{type(e).__name__}: {e}")
        broker.publish(job_id, "", 0.0, "error")
    finally:
        try:
            if os.path.exists(wav):
                os.remove(wav)
        except Exception:
            pass


class Worker:
    def __init__(self, conn, broker, engine, settings_provider, formats_provider,
                 tmp_dir: str, output_dir: str) -> None:
        self.conn = conn
        self.broker = broker
        self.engine = engine
        self.settings_provider = settings_provider
        self.formats_provider = formats_provider
        self.tmp_dir = tmp_dir
        self.output_dir = output_dir
        self._thread: threading.Thread | None = None

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            row = job_queue.claim_next(self.conn)
            if row is None:
                time.sleep(1.0)
                continue
            process_job(self.conn, self.broker, self.engine, row,
                        settings_global=self.settings_provider(),
                        formats=self.formats_provider(),
                        tmp_dir=self.tmp_dir, output_dir=self.output_dir)

    def start(self, stop_event: threading.Event) -> None:
        self._thread = threading.Thread(target=self.run_forever, args=(stop_event,), daemon=True)
        self._thread.start()
```

- [ ] **Step 4: Запустить — пройдёт.** Run: `.venv/Scripts/python -m pytest tests/test_worker.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/worker.py tests/test_worker.py
git commit -m "feat: воркер-конвейер (ffmpeg→engine→writers, статусы, ошибки)"
```

---

## Task 12: Watcher

**Files:**
- Create: `app/watcher.py`
- Test: `tests/test_watcher.py`

**Interfaces:**
- `watcher.is_media(path: str) -> bool` — расширение в наборе видео/аудио.
- `watcher.wait_until_stable(path: str, size_fn: Callable[[str], int], sleep_fn: Callable[[float], None], checks: int=3, interval: float=1.0) -> bool` — размер не меняется `checks` раз подряд (чистая, тестируемая через инъекцию).
- `class InboxWatcher`: оборачивает watchdog `Observer`; на новый стабильный медиафайл вызывает `enqueue_cb(path)`. `start()/stop()`.

- [ ] **Step 1: Тест `tests/test_watcher.py`**

```python
from app import watcher


def test_is_media():
    assert watcher.is_media("a.mp4")
    assert watcher.is_media("b.MKV")
    assert watcher.is_media("c.wav")
    assert not watcher.is_media("d.txt")


def test_wait_until_stable_true_when_size_constant():
    sizes = iter([100, 100, 100, 100])
    ok = watcher.wait_until_stable("f", size_fn=lambda p: next(sizes),
                                   sleep_fn=lambda s: None, checks=3, interval=0)
    assert ok is True


def test_wait_until_stable_false_when_growing():
    sizes = iter([100, 200, 300, 400, 500, 600])
    ok = watcher.wait_until_stable("f", size_fn=lambda p: next(sizes),
                                   sleep_fn=lambda s: None, checks=3, interval=0)
    assert ok is False
```

- [ ] **Step 2: Запустить — упадёт.** Run: `.venv/Scripts/python -m pytest tests/test_watcher.py -v` → FAIL.

- [ ] **Step 3: Написать `app/watcher.py`**

```python
from __future__ import annotations

import os
import time
from typing import Callable

MEDIA_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v",
             ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


def is_media(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in MEDIA_EXT


def wait_until_stable(path: str, size_fn: Callable[[str], int] = os.path.getsize,
                      sleep_fn: Callable[[float], None] = time.sleep,
                      checks: int = 3, interval: float = 1.0) -> bool:
    last = -1
    stable = 0
    for _ in range(checks * 4):
        try:
            cur = size_fn(path)
        except OSError:
            return False
        if cur == last:
            stable += 1
            if stable >= checks:
                return True
        else:
            stable = 0
            last = cur
        sleep_fn(interval)
    return False


class InboxWatcher:
    def __init__(self, inbox_dir: str, enqueue_cb: Callable[[str], None]) -> None:
        self.inbox_dir = inbox_dir
        self.enqueue_cb = enqueue_cb
        self._observer = None

    def _handle(self, path: str) -> None:
        if is_media(path) and wait_until_stable(path):
            self.enqueue_cb(path)

    def start(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        outer = self

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    import threading
                    threading.Thread(target=outer._handle,
                                     args=(event.src_path,), daemon=True).start()

        self._observer = Observer()
        self._observer.schedule(Handler(), self.inbox_dir, recursive=False)
        self._observer.start()

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
```

- [ ] **Step 4: Запустить — пройдёт.** Run: `.venv/Scripts/python -m pytest tests/test_watcher.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add app/watcher.py tests/test_watcher.py
git commit -m "feat: watcher за inbox (стабилизация файла → enqueue)"
```

---

## Task 13: FastAPI app + routes

**Files:**
- Create: `app/api.py`, `app/main.py`
- Test: `tests/test_api.py`

**Interfaces:**
- `api.create_app(conn, broker, settings_state) -> FastAPI`, где `settings_state` — объект с `get_global() -> Settings`, `set_global(Settings)`, `get_formats() -> list[str]`, `set_formats(list)`.
- Роуты:
  - `POST /api/jobs` body `{"path": str, "settings": {...}}` → `{"id": int}` (валидирует существование пути; 400 если нет).
  - `GET /api/jobs` → список job'ов.
  - `GET /api/jobs/{id}` → job.
  - `GET /api/settings` / `PUT /api/settings` → глобальные настройки + форматы.
  - `GET /api/jobs/{id}/download/{fmt}` → файл; `GET /api/jobs/{id}/download_zip` → zip.
  - `GET /api/events` → SSE-поток прогресса.
  - `GET /` → `web/index.html`; статика `/static`.

- [ ] **Step 1: Тест `tests/test_api.py`** (воркер не запускаем — проверяем роуты/очередь)

```python
import json
from fastapi.testclient import TestClient
from app import db, api
from app.models import Settings


class SettingsState:
    def __init__(self):
        self._s = Settings(); self._f = ["txt", "json"]
    def get_global(self): return self._s
    def set_global(self, s): self._s = s
    def get_formats(self): return self._f
    def set_formats(self, f): self._f = f


def make_client(tmp_path):
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    from app.progress import ProgressBroker
    app = api.create_app(conn, ProgressBroker(), SettingsState())
    return TestClient(app), conn


def test_create_job_rejects_missing_path(tmp_path):
    client, _ = make_client(tmp_path)
    r = client.post("/api/jobs", json={"path": "C:/nope/none.mp4", "settings": {}})
    assert r.status_code == 400


def test_create_and_list_job(tmp_path):
    client, _ = make_client(tmp_path)
    f = tmp_path / "a.mp4"; f.write_bytes(b"x")
    r = client.post("/api/jobs", json={"path": str(f), "settings": {"diarize": False}})
    assert r.status_code == 200
    jid = r.json()["id"]
    lst = client.get("/api/jobs").json()
    assert any(j["id"] == jid for j in lst)


def test_get_and_put_settings(tmp_path):
    client, _ = make_client(tmp_path)
    client.put("/api/settings", json={"settings": {"model": "large-v3"}, "formats": ["txt"]})
    got = client.get("/api/settings").json()
    assert got["settings"]["model"] == "large-v3"
    assert got["formats"] == ["txt"]
```

- [ ] **Step 2: Запустить — упадёт.** Run: `.venv/Scripts/python -m pytest tests/test_api.py -v` → FAIL.

- [ ] **Step 3: Написать `app/api.py`**

```python
from __future__ import annotations

import io
import json
import os
import queue
import zipfile

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config, job_queue
from app.models import Settings

WEB_DIR = config.BASE_DIR / "web"


class JobIn(BaseModel):
    path: str
    settings: dict = {}


class SettingsIn(BaseModel):
    settings: dict
    formats: list[str]


def create_app(conn, broker, settings_state) -> FastAPI:
    app = FastAPI(title="Meeting Transcriber")

    @app.post("/api/jobs")
    def create_job(body: JobIn):
        if not os.path.isfile(body.path):
            raise HTTPException(status_code=400, detail="Файл не найден по указанному пути")
        jid = job_queue.enqueue(conn, body.path, json.dumps(body.settings))
        return {"id": jid}

    @app.get("/api/jobs")
    def list_jobs():
        return [dict(r) for r in job_queue.list_jobs(conn)]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        row = job_queue.get(conn, job_id)
        if row is None:
            raise HTTPException(404, "job не найден")
        return dict(row)

    @app.get("/api/settings")
    def get_settings():
        return {"settings": settings_state.get_global().to_dict(),
                "formats": settings_state.get_formats()}

    @app.put("/api/settings")
    def put_settings(body: SettingsIn):
        settings_state.set_global(Settings.from_dict(body.settings))
        settings_state.set_formats(body.formats)
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/download/{fmt}")
    def download(job_id: int, fmt: str):
        row = job_queue.get(conn, job_id)
        if row is None or not row["output_dir"]:
            raise HTTPException(404, "результат недоступен")
        base = os.path.splitext(row["filename"])[0]
        path = os.path.join(row["output_dir"], f"{base}.{fmt}")
        if not os.path.isfile(path):
            raise HTTPException(404, "формат недоступен")
        return FileResponse(path, filename=os.path.basename(path))

    @app.get("/api/jobs/{job_id}/download_zip")
    def download_zip(job_id: int):
        row = job_queue.get(conn, job_id)
        if row is None or not row["output_dir"] or not os.path.isdir(row["output_dir"]):
            raise HTTPException(404, "результат недоступен")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in os.listdir(row["output_dir"]):
                zf.write(os.path.join(row["output_dir"], name), name)
        buf.seek(0)
        return StreamingResponse(
            buf, media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="job_{job_id}.zip"'})

    @app.get("/api/events")
    def events():
        q = broker.subscribe()

        def gen():
            try:
                while True:
                    try:
                        evt = q.get(timeout=15)
                        yield f"data: {json.dumps(evt)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                broker.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    return app
```

- [ ] **Step 4: Написать `app/main.py`**

```python
from __future__ import annotations

import threading

from app import config, db, job_queue
from app.api import create_app
from app.engine.factory import make_engine
from app.models import Settings
from app.progress import ProgressBroker
from app.watcher import InboxWatcher
from app.worker import Worker


class SettingsState:
    def __init__(self) -> None:
        self._settings = Settings()
        self._formats = ["txt", "srt", "vtt", "json", "md", "docx"]

    def get_global(self): return self._settings
    def set_global(self, s): self._settings = s
    def get_formats(self): return list(self._formats)
    def set_formats(self, f): self._formats = list(f)


config.ensure_dirs()
conn = db.connect(config.DB_PATH)
db.init_schema(conn)
recovered = job_queue.recover_stuck(conn)
if recovered:
    print(f"Восстановлено зависших job'ов: {recovered}")

broker = ProgressBroker()
settings_state = SettingsState()
stop_event = threading.Event()

engine = make_engine("auto")
worker = Worker(conn, broker, engine, settings_state.get_global,
                settings_state.get_formats, str(config.TMP_DIR), str(config.OUTPUT_DIR))
worker.start(stop_event)

watcher = InboxWatcher(
    str(config.INBOX_DIR),
    enqueue_cb=lambda path: job_queue.enqueue(conn, path, "{}"),
)
watcher.start()

app = create_app(conn, broker, settings_state)
```

- [ ] **Step 5: Запустить unit-тесты api — пройдёт.** Run: `.venv/Scripts/python -m pytest tests/test_api.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add app/api.py app/main.py tests/test_api.py
git commit -m "feat: FastAPI-роуты (jobs/settings/download/SSE) и сборка приложения"
```

---

## Task 14: Frontend SPA

**Files:**
- Create: `web/index.html`, `web/app.js`, `web/style.css`

**Interfaces:**
- Consumes API: `GET/POST /api/jobs`, `GET/PUT /api/settings`, `GET /api/events`, download-ссылки.
- Produces: SPA — drag-drop/ввод путей, очередь с прогрессом (SSE), панель настроек, кнопки скачивания.

- [ ] **Step 1: Написать `web/index.html`**

```html
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Транскрибатор встреч</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header><h1>Транскрибатор встреч</h1></header>
  <main>
    <section id="settings">
      <h2>Настройки</h2>
      <label>Модель
        <select id="model">
          <option value="large-v3-turbo">large-v3-turbo</option>
          <option value="large-v3">large-v3</option>
        </select>
      </label>
      <label>Язык <input id="language" placeholder="авто (пусто) / ru"></label>
      <label><input type="checkbox" id="diarize" checked> Диаризация</label>
      <label>Число спикеров <input id="num_speakers" type="number" min="1" placeholder="авто"></label>
      <label>Словарь (≤ ~224 токена; смещает распознавание, не гарантирует)
        <textarea id="vocabulary" rows="3" placeholder="АккордПост, ОФД, ..."></textarea>
      </label>
      <fieldset><legend>Форматы вывода</legend>
        <label><input type="checkbox" class="fmt" value="txt" checked> TXT</label>
        <label><input type="checkbox" class="fmt" value="srt" checked> SRT</label>
        <label><input type="checkbox" class="fmt" value="vtt" checked> VTT</label>
        <label><input type="checkbox" class="fmt" value="json" checked> JSON</label>
        <label><input type="checkbox" class="fmt" value="md" checked> MD</label>
        <label><input type="checkbox" class="fmt" value="docx" checked> DOCX</label>
      </fieldset>
      <button id="save-settings">Сохранить настройки</button>
    </section>

    <section id="add">
      <h2>Добавить файлы</h2>
      <div id="drop">Перетащите файлы сюда или вставьте пути ниже</div>
      <textarea id="paths" rows="3" placeholder="C:\videos\meeting1.mp4 (по одному пути на строку)"></textarea>
      <button id="add-paths">В очередь</button>
      <p class="hint">Также можно просто положить файлы в папку <code>inbox/</code>.</p>
    </section>

    <section id="queue">
      <h2>Очередь</h2>
      <div id="jobs"></div>
    </section>
  </main>
  <script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Написать `web/style.css`**

```css
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; margin: 0; background: #0f1115; color: #e6e6e6; }
header { padding: 16px 24px; background: #161a22; border-bottom: 1px solid #262b36; }
h1 { margin: 0; font-size: 20px; }
main { display: grid; grid-template-columns: 320px 1fr; gap: 16px; padding: 16px 24px; }
section { background: #161a22; border: 1px solid #262b36; border-radius: 10px; padding: 16px; }
#queue { grid-column: 1 / -1; }
label { display: block; margin: 8px 0; font-size: 14px; }
input, select, textarea { width: 100%; background: #0f1115; color: #e6e6e6; border: 1px solid #2c3340; border-radius: 6px; padding: 6px; }
input[type=checkbox] { width: auto; }
button { background: #3b82f6; color: #fff; border: 0; border-radius: 6px; padding: 8px 14px; cursor: pointer; margin-top: 8px; }
#drop { border: 2px dashed #2c3340; border-radius: 8px; padding: 24px; text-align: center; color: #9aa4b2; }
#drop.over { border-color: #3b82f6; color: #cfe0ff; }
.job { border: 1px solid #262b36; border-radius: 8px; padding: 12px; margin: 8px 0; }
.bar { height: 8px; background: #0f1115; border-radius: 4px; overflow: hidden; margin: 6px 0; }
.bar > i { display: block; height: 100%; background: #3b82f6; width: 0; transition: width .3s; }
.status-done .bar > i { background: #22c55e; }
.status-error { border-color: #ef4444; }
.hint { color: #9aa4b2; font-size: 13px; }
a { color: #8ab4ff; margin-right: 10px; }
```

- [ ] **Step 3: Написать `web/app.js`**

```javascript
const $ = (id) => document.getElementById(id);

function currentSettings() {
  const ns = $("num_speakers").value;
  return {
    model: $("model").value,
    language: $("language").value.trim() || null,
    diarize: $("diarize").checked,
    num_speakers: ns ? parseInt(ns, 10) : null,
    vocabulary: $("vocabulary").value,
  };
}
function currentFormats() {
  return [...document.querySelectorAll(".fmt:checked")].map((c) => c.value);
}

async function loadSettings() {
  const r = await fetch("/api/settings");
  const { settings, formats } = await r.json();
  $("model").value = settings.model;
  $("language").value = settings.language || "";
  $("diarize").checked = settings.diarize;
  $("num_speakers").value = settings.num_speakers || "";
  $("vocabulary").value = settings.vocabulary || "";
  document.querySelectorAll(".fmt").forEach((c) => (c.checked = formats.includes(c.value)));
}

$("save-settings").onclick = async () => {
  await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings: currentSettings(), formats: currentFormats() }),
  });
  alert("Настройки сохранены");
};

async function enqueue(path) {
  const r = await fetch("/api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, settings: currentSettings() }),
  });
  if (!r.ok) alert("Ошибка добавления: " + path);
}

$("add-paths").onclick = async () => {
  const paths = $("paths").value.split("\n").map((s) => s.trim()).filter(Boolean);
  for (const p of paths) await enqueue(p);
  $("paths").value = "";
  refresh();
};

const drop = $("drop");
["dragover", "dragenter"].forEach((e) =>
  drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((e) =>
  drop.addEventListener(e, () => drop.classList.remove("over")));
drop.addEventListener("drop", async (ev) => {
  ev.preventDefault();
  const lines = [];
  for (const f of ev.dataTransfer.files) lines.push(f.path || f.name);
  $("paths").value = lines.join("\n");
});

function jobCard(j) {
  const pct = Math.round((j.progress || 0) * 100);
  const links = j.status === "done"
    ? `<div>${["txt","srt","vtt","json","md","docx"]
        .map((f) => `<a href="/api/jobs/${j.id}/download/${f}">${f}</a>`).join("")}
       <a href="/api/jobs/${j.id}/download_zip">zip</a></div>` : "";
  const err = j.status === "error" ? `<div class="hint">${j.error || ""}</div>` : "";
  return `<div class="job status-${j.status}" id="job-${j.id}">
      <b>${j.filename}</b> — ${j.status} ${j.stage ? "("+j.stage+")" : ""}
      <div class="bar"><i style="width:${pct}%"></i></div>${links}${err}
    </div>`;
}

async function refresh() {
  const jobs = await (await fetch("/api/jobs")).json();
  $("jobs").innerHTML = jobs.map(jobCard).join("") || "<p class=hint>Очередь пуста</p>";
}

const es = new EventSource("/api/events");
es.onmessage = (e) => {
  const evt = JSON.parse(e.data);
  if (evt.status === "done" || evt.status === "error") refresh();
  else {
    const el = document.querySelector(`#job-${evt.job_id} .bar > i`);
    if (el) el.style.width = Math.round((evt.progress || 0) * 100) + "%";
    else refresh();
  }
};

loadSettings();
refresh();
setInterval(refresh, 5000);
```

- [ ] **Step 4: Smoke-проверка UI**

Run: `.venv/Scripts/python -m uvicorn app.main:app --port 8000`
Открыть `http://127.0.0.1:8000`. Ожидать: страница грузится, настройки читаются, очередь пуста. Остановить (Ctrl+C).

- [ ] **Step 5: Commit**

```bash
git add web/
git commit -m "feat: фронтенд SPA (drag-drop по путям, очередь, настройки, SSE, скачивание)"
```

---

## Task 15: README + end-to-end проверка

**Files:**
- Create: `README.md`

**Interfaces:**
- Produces: инструкция запуска; подтверждённый сквозной прогон на реальном коротком видео.

- [ ] **Step 1: Написать `README.md`**

````markdown
# Meeting Transcriber

Локальный транскрибатор записей встреч: видео/аудио → диаризованная транскрипция с таймкодами (TXT/SRT/VTT/JSON/MD/DOCX) на GPU.

## Требования
- Windows, NVIDIA GPU (проверено на RTX 5070 Ti, 16 ГБ).
- Python 3.12 (отдельно от системного 3.14).
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
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```
Открыть http://127.0.0.1:8000. Либо просто класть файлы в `inbox/`.

## Тесты
```bash
.venv/Scripts/python -m pytest -q
```
````

- [ ] **Step 2: Полный прогон тестов**

Run: `.venv/Scripts/python -m pytest -q`
Expected: все тесты зелёные.

- [ ] **Step 3: End-to-end на реальном файле**

1. Запустить сервер (Task 14 Step 4).
2. Положить короткое видео встречи (1–2 мин, ≥2 говорящих) в `inbox/` ИЛИ добавить путь через UI.
3. Дождаться `done`, скачать zip.
Expected: в `output/<id>/` все выбранные форматы; в TXT — реплики с таймкодами и «Спикер N»; текст осмысленный, русский.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: README + подтверждён сквозной прогон"
```

---

## Self-Review (выполнено при написании плана)

**Spec coverage:**
- §2 движок WhisperX/faster-whisper → Task 8; fallback PyTorch-native → Task 9; GPU-spike → Task 1. ✓
- §2 диаризация pyannote + HF-токен → Task 8 (`diarize.py`), config HF_TOKEN → Task 3. ✓
- §2 деплой нативно/venv 3.12 → Task 0, Global Constraints. ✓
- §3 watched-папка + UI по путям → Task 12 (watcher), Task 13 (POST /api/jobs по path), Task 14 (UI). ✓
- §3 16 kHz mono → Task 5 (build_extract_cmd). ✓
- §2/§5 модель large-v3-turbo дефолт → Task 2 (Settings), Task 14 (select). ✓
- §2 форматы TXT/SRT/VTT/JSON/MD/DOCX → Task 7. ✓
- §2 очередь последовательная, 1 файл → Task 4 + Task 11 (один воркер-поток). ✓
- §4 обработка ошибок (ffmpeg/нет аудио/OOM/рестарт) → Task 5 (FFmpegError), Task 11 (try/except), Task 4 (recover_stuck), Task 13 (main recovery). ✓
- §7 словарь 224 токена → Task 8 (truncate_prompt), Task 14 (предупреждение в UI). ✓
- §9 тестирование (writers/queue/ffmpeg/watcher + интеграционный) → Tasks 4,5,7,12 + Task 15 Step 3. ✓
- §11 YAGNI (нет upload, нет параллелизма, нет find/replace) — соблюдено. ✓

**Placeholder scan:** плейсхолдеров (TBD/TODO/«аналогично Task N») нет; код приведён полностью.

**Type consistency:** `Word(start,end,text,speaker,score)`, `Segment(start,end,text,speaker,words)`, `TranscriptResult(language,duration,model,diarized,segments)`, `Settings(model,language,diarize,num_speakers,vocabulary)`, `TranscriptEngine.transcribe(audio_path,settings,progress)` — единообразны во всех задачах. Сигнатуры `job_queue.*`, `writers.write_all`, `stitch.*` совпадают между объявлением и использованием.
