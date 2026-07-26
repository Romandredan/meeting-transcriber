from __future__ import annotations

import threading

from app import analyses, config, db, job_queue, llm, templates_store
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

# Стадия analyze: провайдера нет — воркер не заглядывает в очередь анализов,
# Ollama не требуется, приложение работает ровно как раньше.
provider = None
if config.ANALYZE_ENABLED:
    seeded = templates_store.seed_defaults(conn)
    if seeded:
        print(f"Добавлено дефолтных шаблонов анализа: {seeded}")
    recovered_a = analyses.recover_stuck(conn)
    if recovered_a:
        print(f"Восстановлено зависших анализов: {recovered_a}")
    provider = llm.make_provider()

broker = ProgressBroker()
settings_state = SettingsStore(conn)  # переживает перезапуск (таблица settings)
stop_event = threading.Event()

engine = make_engine("auto")
worker = Worker(conn, broker, engine, settings_state.get_global,
                settings_state.get_formats, str(config.TMP_DIR), str(config.OUTPUT_DIR),
                inbox_dir=str(config.INBOX_DIR),
                processed_dir=str(config.PROCESSED_DIR) if config.MOVE_PROCESSED else None,
                provider=provider)
worker.start(stop_event)

def _enqueue_from_inbox(path: str) -> None:
    # Дедуп: не ставим файл, по которому уже есть job в работе/готовый
    # (важно при стартовом скане и MOVE_PROCESSED=false).
    if not job_queue.has_active(conn, path):
        job_queue.enqueue(conn, path, "{}")


watcher = InboxWatcher(
    str(config.INBOX_DIR),
    enqueue_cb=_enqueue_from_inbox,
    quiet_seconds=config.INBOX_QUIET_SECONDS,
    poll_seconds=config.INBOX_POLL_SECONDS,
)
watcher.start()

app = create_app(conn, broker, settings_state)


if __name__ == "__main__":
    import uvicorn
    print(f"Открой http://{config.APP_HOST}:{config.APP_PORT}")
    uvicorn.run(app, host=config.APP_HOST, port=config.APP_PORT)
