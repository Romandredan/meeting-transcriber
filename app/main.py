from __future__ import annotations

import threading

from app import config, db, job_queue
from app.api import create_app
from app.engine.factory import make_engine
from app.progress import ProgressBroker
from app.settings_store import SettingsStore
from app.watcher import InboxWatcher
from app.worker import Worker


config.ensure_dirs()
conn = db.connect(config.DB_PATH)
db.init_schema(conn)
recovered = job_queue.recover_stuck(conn)
if recovered:
    print(f"Восстановлено зависших job'ов: {recovered}")

broker = ProgressBroker()
settings_state = SettingsStore(conn)  # переживает перезапуск (таблица settings)
stop_event = threading.Event()

engine = make_engine("auto")
worker = Worker(conn, broker, engine, settings_state.get_global,
                settings_state.get_formats, str(config.TMP_DIR), str(config.OUTPUT_DIR),
                inbox_dir=str(config.INBOX_DIR),
                processed_dir=str(config.PROCESSED_DIR) if config.MOVE_PROCESSED else None)
worker.start(stop_event)

watcher = InboxWatcher(
    str(config.INBOX_DIR),
    enqueue_cb=lambda path: job_queue.enqueue(conn, path, "{}"),
    quiet_seconds=config.INBOX_QUIET_SECONDS,
    poll_seconds=config.INBOX_POLL_SECONDS,
)
watcher.start()

app = create_app(conn, broker, settings_state)


if __name__ == "__main__":
    import uvicorn
    print(f"Открой http://{config.APP_HOST}:{config.APP_PORT}")
    uvicorn.run(app, host=config.APP_HOST, port=config.APP_PORT)
