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
APP_HOST = os.environ.get("APP_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("APP_PORT", "8473"))  # нечастый дефолт; меняется через .env


def ensure_dirs() -> None:
    for d in (INBOX_DIR, OUTPUT_DIR, TMP_DIR, MODELS_DIR):
        d.mkdir(parents=True, exist_ok=True)
