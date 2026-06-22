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
