from __future__ import annotations

import sqlite3


def enqueue(conn: sqlite3.Connection, job_id: int, label: str, display_name: str,
            prompt_snapshot: str, model: str) -> int:
    """Ставит анализ в очередь со СНИМКОМ промпта.

    Снимок, а не ссылка на шаблон: правка шаблона потом не должна менять ответ на
    вопрос «каким текстом это было сделано» — без него история версий бесполезна."""
    cur = conn.execute(
        "INSERT INTO analyses (job_id, label, display_name, prompt_snapshot, model) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, label, display_name, prompt_snapshot, model),
    )
    conn.commit()
    return int(cur.lastrowid)


def has_active(conn: sqlite3.Connection, job_id: int, label: str) -> bool:
    """Есть ли уже анализ этой встречи по этой метке в очереди/работе.

    По образцу job_queue.has_active. В отличие от job'ов, 'done' НЕ считается
    активным: повторный прогон той же метки — штатный сценарий «Ещё раз», ради
    которого и заведена история версий."""
    row = conn.execute(
        "SELECT 1 FROM analyses WHERE job_id=? AND label=? AND status IN "
        "('queued','processing') LIMIT 1",
        (job_id, label),
    ).fetchone()
    return row is not None


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM analyses WHERE status='queued' ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE analyses SET status='processing', stage='', progress=0 WHERE id=?",
        (row["id"],),
    )
    conn.commit()
    return conn.execute("SELECT * FROM analyses WHERE id=?", (row["id"],)).fetchone()


def update(conn: sqlite3.Connection, analysis_id: int, *, status=None, stage=None,
           progress=None, chunks=None, result_md=None, error=None, edited=None) -> None:
    fields, values = [], []
    for name, val in (("status", status), ("stage", stage), ("progress", progress),
                      ("chunks", chunks), ("result_md", result_md), ("error", error),
                      ("edited", edited)):
        if val is not None:
            fields.append(f"{name}=?")
            values.append(val)
    if not fields:
        return
    values.append(analysis_id)
    conn.execute(f"UPDATE analyses SET {', '.join(fields)} WHERE id=?", values)
    conn.commit()


def get(conn: sqlite3.Connection, analysis_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()


def list_for_job(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    """История анализов встречи, новые первыми: первая строка со status='done'
    по метке и есть «текущая версия» этой метки."""
    return conn.execute(
        "SELECT * FROM analyses WHERE job_id=? ORDER BY id DESC", (job_id,)
    ).fetchall()


def delete(conn: sqlite3.Connection, analysis_id: int) -> None:
    conn.execute("DELETE FROM analyses WHERE id=?", (analysis_id,))
    conn.commit()


def recover_stuck(conn: sqlite3.Connection) -> int:
    """Сервер упал во время анализа — вернуть 'processing' в очередь."""
    cur = conn.execute(
        "UPDATE analyses SET status='queued', stage='', progress=0 WHERE status='processing'"
    )
    conn.commit()
    return cur.rowcount
