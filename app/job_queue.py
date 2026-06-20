from __future__ import annotations

import os
import sqlite3


def enqueue(conn: sqlite3.Connection, source_path: str, settings_json: str) -> int:
    filename = os.path.basename(source_path)
    cur = conn.execute(
        "INSERT INTO jobs (source_path, filename, settings_json) VALUES (?, ?, ?)",
        (source_path, filename, settings_json),
    )
    conn.commit()
    return int(cur.lastrowid)


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
