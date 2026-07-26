from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


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


# .env читаем ДО вычисления путей, чтобы каталоги можно было переопределить в нём.
load_env()


def _dir(env_name: str, default: Path) -> Path:
    """Каталог из переменной окружения или дефолт (под BASE_DIR)."""
    value = os.environ.get(env_name)
    return Path(value).expanduser() if value else default


def _flag(env_name: str, default: bool) -> bool:
    value = os.environ.get(env_name)
    if value is None:
        return default
    return value.strip().lower() not in ("false", "0", "no", "off", "")


# Пользовательские каталоги (можно переопределить в .env абсолютными путями).
INBOX_DIR = _dir("INBOX_DIR", BASE_DIR / "inbox")          # папка для отслеживания
OUTPUT_DIR = _dir("OUTPUT_DIR", BASE_DIR / "output")        # куда писать результаты
PROCESSED_DIR = _dir("PROCESSED_DIR", BASE_DIR / "processed")  # куда уносить обработанные из inbox
TMP_DIR = _dir("TMP_DIR", BASE_DIR / "tmp")
MODELS_DIR = _dir("MODELS_DIR", BASE_DIR / "models")
DB_PATH = _dir("DB_PATH", BASE_DIR / "data.db")

HF_TOKEN = os.environ.get("HF_TOKEN") or None
APP_HOST = os.environ.get("APP_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("APP_PORT", "8473"))  # нечастый дефолт; меняется через .env

# Переносить ли исходник из inbox в processed после успешной обработки.
# Выключи (MOVE_PROCESSED=false), если отслеживаешь свою «живую» папку записей
# и не хочешь, чтобы оригиналы перемещались.
MOVE_PROCESSED = _flag("MOVE_PROCESSED", True)

# Watcher: файл из inbox ставится в очередь только когда размер И mtime
# не менялись INBOX_QUIET_SECONDS подряд (защита от захвата ещё пишущегося/
# стримящегося файла). Пока файл растёт — ждём без раннего отказа.
INBOX_QUIET_SECONDS = float(os.environ.get("INBOX_QUIET_SECONDS", "15"))
INBOX_POLL_SECONDS = float(os.environ.get("INBOX_POLL_SECONDS", "2"))

# Склейка реплик одного спикера (TXT/MD/DOCX): максимальная длина блока в секундах.
# Длинный монолог режется на под-блоки по этому порогу. 0 — без ограничения.
MERGE_MAX_SECONDS = float(os.environ.get("MERGE_MAX_SECONDS", "90"))

def _base_url(env_name: str, default: str) -> str:
    """URL без хвостового слэша — иначе склейка даёт '//api/chat'."""
    return os.environ.get(env_name, default).strip().rstrip("/")


# --- Стадия analyze (структурирование транскрипта локальной LLM через Ollama) ---
# ANALYZE_ENABLED=false полностью выключает фичу: роуты не регистрируются,
# воркер не заглядывает в очередь анализов, UI не показывает блок анализов.
ANALYZE_ENABLED = _flag("ANALYZE_ENABLED", True)
LLM_BASE_URL = _base_url("LLM_BASE_URL", "http://localhost:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3:14b")
# Окно контекста. 32768 рассчитано под 16 ГБ VRAM при KV-кэше q8_0 (см. спеку §6).
LLM_NUM_CTX = int(os.environ.get("LLM_NUM_CTX", "32768"))
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))
# Таймаут ПО ПРОСТОЮ, а не по общему времени: счётчик сбрасывается на каждом
# полученном токене. Медленный, но живой ответ (счёт в ОЗУ) не убивается.
LLM_IDLE_TIMEOUT = float(os.environ.get("LLM_IDLE_TIMEOUT", "180"))

# Через сколько секунд простоя (обе очереди — jobs и analyses — пусты) выгружать
# все модели из видеопамяти. Программа работает как фоновая служба с автозапуском
# при входе в систему и может часами простаивать между встречами, всё это время
# держа несколько гигабайт VRAM занятыми. 0 — не выгружать вовсе.
IDLE_UNLOAD_SECONDS = float(os.environ.get("IDLE_UNLOAD_SECONDS", "300"))


def ensure_dirs() -> None:
    for d in (INBOX_DIR, PROCESSED_DIR, OUTPUT_DIR, TMP_DIR, MODELS_DIR):
        d.mkdir(parents=True, exist_ok=True)
