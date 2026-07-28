from __future__ import annotations

import os
import sqlite3
import threading


def enqueue(conn: sqlite3.Connection, source_path: str, settings_json: str) -> int:
    filename = os.path.basename(source_path)
    cur = conn.execute(
        "INSERT INTO jobs (source_path, filename, settings_json) VALUES (?, ?, ?)",
        (source_path, filename, settings_json),
    )
    conn.commit()
    return int(cur.lastrowid)


def has_active(conn: sqlite3.Connection, source_path: str) -> bool:
    """Есть ли уже job по этому источнику в работе/готовый (queued/processing/done).

    Используется watcher'ом, чтобы не ставить повторно файл, который ещё лежит в inbox
    (например, при MOVE_PROCESSED=false или при стартовом скане). 'error' не считаем —
    такой файл можно поставить заново. 'cancelled' считаем: иначе отменённый файл из
    inbox watcher поставил бы в очередь заново, и отмена не была бы окончательной."""
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE source_path=? AND status IN "
        "('queued','processing','done','cancelled') LIMIT 1",
        (source_path,),
    ).fetchone()
    return row is not None


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


class JobCancelled(Exception):
    """Пользователь отменил расшифровку — не ошибка обработки."""


# Отмена «на лету»: очередь работ живёт в БД, а флаг отмены — в памяти процесса.
# Так и должно быть: воркер и API — один процесс, а после перезапуска сервера
# отменять уже нечего (recover_stuck вернёт зависшее 'processing' в очередь).
_cancel_requests: set[int] = set()
_cancel_lock = threading.Lock()


def request_cancel(job_id: int) -> None:
    with _cancel_lock:
        _cancel_requests.add(int(job_id))


def cancel_requested(job_id: int) -> bool:
    with _cancel_lock:
        return int(job_id) in _cancel_requests


def clear_cancel(job_id: int) -> None:
    with _cancel_lock:
        _cancel_requests.discard(int(job_id))


def delete(conn: sqlite3.Connection, job_id: int) -> None:
    """Убирает встречу из списка вместе с её анализами.

    Файлы в output/ НЕ трогаем: результат мог быть уже разослан, а строка в
    списке — только представление. Чистку диска делает отдельный вызов из API
    с purge=true."""
    conn.execute("DELETE FROM analyses WHERE job_id=?", (job_id,))
    conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    conn.commit()
