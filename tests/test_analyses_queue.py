from app import analyses, db


def make_conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def add(conn, job_id=1, label="protocol"):
    return analyses.enqueue(conn, job_id, label, "Протокол", "тело промпта", "qwen3:14b")


def test_enqueue_and_get(tmp_path):
    conn = make_conn(tmp_path)
    aid = add(conn)
    row = analyses.get(conn, aid)
    assert row["status"] == "queued"
    assert row["label"] == "protocol"
    assert row["prompt_snapshot"] == "тело промпта"
    assert row["model"] == "qwen3:14b"


def test_claim_next_sets_processing_and_is_fifo(tmp_path):
    conn = make_conn(tmp_path)
    a = add(conn, label="protocol")
    b = add(conn, label="summary")
    first = analyses.claim_next(conn)
    assert first["id"] == a
    assert first["status"] == "processing"
    assert analyses.claim_next(conn)["id"] == b
    assert analyses.claim_next(conn) is None


def test_update_fields(tmp_path):
    conn = make_conn(tmp_path)
    aid = add(conn)
    analyses.update(conn, aid, status="done", progress=1.0, stage="reduce",
                    chunks=7, result_md="# Протокол")
    row = analyses.get(conn, aid)
    assert row["status"] == "done"
    assert row["progress"] == 1.0
    assert row["stage"] == "reduce"
    assert row["chunks"] == 7
    assert row["result_md"] == "# Протокол"


def test_update_accepts_zero_progress(tmp_path):
    """0 — валидное значение, а не «не передано»."""
    conn = make_conn(tmp_path)
    aid = add(conn)
    analyses.update(conn, aid, progress=0.5)
    analyses.update(conn, aid, progress=0)
    assert analyses.get(conn, aid)["progress"] == 0


def test_list_for_job_newest_first_and_scoped(tmp_path):
    conn = make_conn(tmp_path)
    old = add(conn, job_id=1)
    new = add(conn, job_id=1)
    add(conn, job_id=2)
    rows = analyses.list_for_job(conn, 1)
    assert [r["id"] for r in rows] == [new, old]


def test_delete_removes_row(tmp_path):
    conn = make_conn(tmp_path)
    aid = add(conn)
    analyses.delete(conn, aid)
    assert analyses.get(conn, aid) is None


def test_recover_stuck(tmp_path):
    conn = make_conn(tmp_path)
    add(conn)
    analyses.claim_next(conn)
    assert analyses.recover_stuck(conn) == 1
    assert analyses.claim_next(conn) is not None  # снова queued


def test_has_active_true_for_queued(tmp_path):
    conn = make_conn(tmp_path)
    add(conn, job_id=1, label="protocol")
    assert analyses.has_active(conn, 1, "protocol") is True


def test_has_active_true_for_processing(tmp_path):
    conn = make_conn(tmp_path)
    add(conn, job_id=1, label="protocol")
    analyses.claim_next(conn)
    assert analyses.has_active(conn, 1, "protocol") is True


def test_has_active_false_when_done(tmp_path):
    """done НЕ считается активным — повторный прогон той же метки штатный сценарий."""
    conn = make_conn(tmp_path)
    aid = add(conn, job_id=1, label="protocol")
    analyses.update(conn, aid, status="done", result_md="# готово")
    assert analyses.has_active(conn, 1, "protocol") is False


def test_has_active_false_when_error(tmp_path):
    conn = make_conn(tmp_path)
    aid = add(conn, job_id=1, label="protocol")
    analyses.update(conn, aid, status="error", error="сбой")
    assert analyses.has_active(conn, 1, "protocol") is False


def test_has_active_false_for_different_label_or_job(tmp_path):
    conn = make_conn(tmp_path)
    add(conn, job_id=1, label="protocol")
    assert analyses.has_active(conn, 1, "summary") is False
    assert analyses.has_active(conn, 2, "protocol") is False
