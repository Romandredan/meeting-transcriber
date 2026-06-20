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
        except (OSError, StopIteration):
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
