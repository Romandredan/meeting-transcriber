from app import db, job_queue


def make_conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def test_enqueue_and_get(tmp_path):
    conn = make_conn(tmp_path)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    row = job_queue.get(conn, jid)
    assert row["status"] == "queued"
    assert row["filename"] == "a.mp4"


def test_claim_next_sets_processing_and_is_fifo(tmp_path):
    conn = make_conn(tmp_path)
    a = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    b = job_queue.enqueue(conn, "C:/v/b.mp4", "{}")
    first = job_queue.claim_next(conn)
    assert first["id"] == a
    assert first["status"] == "processing"
    second = job_queue.claim_next(conn)
    assert second["id"] == b
    assert job_queue.claim_next(conn) is None


def test_update_fields(tmp_path):
    conn = make_conn(tmp_path)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_queue.update(conn, jid, status="done", progress=1.0, stage="write", output_dir="output/1")
    row = job_queue.get(conn, jid)
    assert row["status"] == "done"
    assert row["progress"] == 1.0
    assert row["output_dir"] == "output/1"


def test_recover_stuck(tmp_path):
    conn = make_conn(tmp_path)
    job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_queue.claim_next(conn)
    assert job_queue.recover_stuck(conn) == 1
    row = job_queue.claim_next(conn)
    assert row is not None  # снова queued


def test_has_active(tmp_path):
    conn = make_conn(tmp_path)
    assert job_queue.has_active(conn, "C:/v/a.mp4") is False
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    assert job_queue.has_active(conn, "C:/v/a.mp4") is True   # queued
    job_queue.update(conn, jid, status="error", error="x")
    assert job_queue.has_active(conn, "C:/v/a.mp4") is False  # error → можно заново
    job_queue.update(conn, jid, status="done")
    assert job_queue.has_active(conn, "C:/v/a.mp4") is True   # done считается


def test_schema_creates_templates_and_analyses(tmp_path):
    conn = make_conn(tmp_path)
    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "settings", "templates", "analyses"} <= names


def test_init_schema_is_idempotent(tmp_path):
    """Повторный init_schema на существующей БД не падает и не трогает данные."""
    conn = make_conn(tmp_path)
    conn.execute("INSERT INTO templates (label, display_name, prompt_body) "
                 "VALUES ('protocol', 'Протокол', 'текст')")
    conn.commit()
    db.init_schema(conn)
    assert conn.execute("SELECT COUNT(*) c FROM templates").fetchone()["c"] == 1


def test_jobs_have_processed_path_column(tmp_path):
    """Миграция 3: колонка processed_path есть и в новой, и в старой БД."""
    conn = make_conn(tmp_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
    assert "processed_path" in cols
    # Старая БД (только базовая схема, user_version=0) догоняется миграцией.
    import sqlite3 as _sq
    old = _sq.connect(str(tmp_path / "old.db"))
    old.executescript(db.SCHEMA)
    old.commit()
    db.init_schema(old)
    cols = [r[1] for r in old.execute("PRAGMA table_info(jobs)")]
    assert "processed_path" in cols


def test_backfill_processed_paths(tmp_path):
    conn = make_conn(tmp_path)
    processed = tmp_path / "processed"
    processed.mkdir()
    (processed / "a.mp4").write_bytes(b"x")
    moved = job_queue.enqueue(conn, str(tmp_path / "inbox" / "a.mp4"), "{}")  # источника нет
    job_queue.update(conn, moved, status="done")
    alive = tmp_path / "b.mp4"; alive.write_bytes(b"x")                        # источник на месте
    kept = job_queue.enqueue(conn, str(alive), "{}")
    job_queue.update(conn, kept, status="done")

    assert job_queue.backfill_processed_paths(conn, str(processed)) == 1
    assert job_queue.get(conn, moved)["processed_path"] == str(processed / "a.mp4")
    assert job_queue.get(conn, kept)["processed_path"] is None
