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


if __name__ == "__main__":
    import uvicorn
    print(f"Открой http://{config.APP_HOST}:{config.APP_PORT}")
    uvicorn.run(app, host=config.APP_HOST, port=config.APP_PORT)
