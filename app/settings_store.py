from __future__ import annotations

import json

from app import config
from app.models import Settings

DEFAULT_FORMATS = ["txt", "srt", "vtt", "json", "md", "docx"]


class SettingsStore:
    """Глобальные настройки + выбранные форматы с сохранением в таблице settings (id=1).

    Переживает перезапуск сервера: при создании читает сохранённый блоб,
    при каждом изменении — пишет обратно. Интерфейс совместим с тем, что
    ожидает api.create_app/worker (get_global/set_global/get_formats/set_formats).
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        # Модель по умолчанию — из .env (WHISPER_MODEL): у клиента со слабой
        # машиной дефолт меняется без правки кода.
        self._settings = Settings(model=config.WHISPER_MODEL)
        self._formats = list(DEFAULT_FORMATS)
        self._load()

    def _load(self) -> None:
        row = self._conn.execute("SELECT data FROM settings WHERE id=1").fetchone()
        if row is not None:
            data = json.loads(row["data"])
            self._settings = Settings.from_dict(data.get("settings", {}))
            self._formats = data.get("formats", list(DEFAULT_FORMATS))

    def _persist(self) -> None:
        blob = json.dumps({"settings": self._settings.to_dict(), "formats": self._formats})
        self._conn.execute(
            "INSERT INTO settings (id, data) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (blob,),
        )
        self._conn.commit()

    def get_global(self) -> Settings:
        return self._settings

    def set_global(self, s: Settings) -> None:
        self._settings = s
        self._persist()

    def get_formats(self) -> list[str]:
        return list(self._formats)

    def set_formats(self, f: list[str]) -> None:
        self._formats = list(f)
        self._persist()
