from __future__ import annotations

import os
import time
from typing import Callable

MEDIA_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v",
             ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


def is_media(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in MEDIA_EXT


def _signature(path: str) -> tuple[int, float]:
    """Размер и время изменения — растущий/пишущийся файл меняет хотя бы одно."""
    st = os.stat(path)
    return (st.st_size, st.st_mtime)


def wait_until_stable(path: str,
                      sig_fn: Callable[[str], tuple] = _signature,
                      sleep_fn: Callable[[float], None] = time.sleep,
                      quiet_seconds: float = 15.0,
                      poll_seconds: float = 2.0,
                      max_wait: float = 86400.0) -> bool:
    """True, когда (размер, mtime) не меняются `quiet_seconds` подряд — файл дописан.

    Пока файл растёт (копирование/стриминговая запись), сигнатура меняется и таймер
    тишины сбрасывается — поэтому мы НЕ хватаем ещё пишущийся файл и НЕ сдаёмся рано
    на больших/долгих записях. False — если файл исчез или превышен `max_wait`.
    """
    last_sig = None
    stable_for = 0.0
    waited = 0.0
    while waited <= max_wait:
        try:
            sig = sig_fn(path)
        except (OSError, StopIteration):
            return False  # файл исчез/недоступен — не ставим в очередь
        if sig == last_sig:
            stable_for += poll_seconds
            if stable_for >= quiet_seconds:
                return True
        else:
            stable_for = 0.0
            last_sig = sig
        sleep_fn(poll_seconds)
        waited += poll_seconds
    return False


class InboxWatcher:
    def __init__(self, inbox_dir: str, enqueue_cb: Callable[[str], None],
                 quiet_seconds: float = 15.0, poll_seconds: float = 2.0) -> None:
        self.inbox_dir = inbox_dir
        self.enqueue_cb = enqueue_cb
        self.quiet_seconds = quiet_seconds
        self.poll_seconds = poll_seconds
        self._observer = None
        import threading
        self._inflight: set[str] = set()       # пути, уже взятые в обработку (дедуп событий)
        self._lock = threading.Lock()

    def _dispatch(self, path: str) -> None:
        """Запускает обработку пути в отдельном потоке, не допуская дублей."""
        import os
        import threading
        if not path:
            return
        norm = os.path.abspath(path)
        with self._lock:
            if norm in self._inflight:
                return
            self._inflight.add(norm)
        threading.Thread(target=self._handle, args=(norm,), daemon=True).start()

    def _handle(self, path: str) -> None:
        try:
            if is_media(path) and wait_until_stable(
                    path, quiet_seconds=self.quiet_seconds, poll_seconds=self.poll_seconds):
                self.enqueue_cb(path)
        finally:
            with self._lock:
                self._inflight.discard(path)

    def scan_existing(self) -> None:
        """Ставит в очередь медиафайлы, уже лежащие в inbox на момент старта."""
        import os
        for root, _dirs, files in os.walk(self.inbox_dir):
            for name in files:
                self._dispatch(os.path.join(root, name))

    def start(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        outer = self

        class Handler(FileSystemEventHandler):
            # Ловим и создание, и перемещение/переименование в inbox: Проводник при
            # копировании с другого диска нередко генерирует moved, а не created.
            def on_created(self, event):
                if not event.is_directory:
                    outer._dispatch(event.src_path)

            def on_moved(self, event):
                if not event.is_directory:
                    outer._dispatch(getattr(event, "dest_path", None))

        self._observer = Observer()
        self._observer.schedule(Handler(), self.inbox_dir, recursive=True)
        self._observer.start()
        # Подхватываем файлы, уже лежащие в inbox до запуска сервера.
        self.scan_existing()

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
