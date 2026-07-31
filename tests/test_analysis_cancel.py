"""Отмена анализа: мгновенная из очереди, кооперативная в работе."""
from fastapi.testclient import TestClient

from app import analyses, db, job_queue, templates_store, transcripts, worker
from app.api import create_app
from app.models import Segment, Settings, TranscriptResult
from app.progress import ProgressBroker
from fakes import FakeProvider


def make_conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    templates_store.seed_defaults(conn)
    return conn


class _SettingsState:
    def get_global(self): return Settings()
    def set_global(self, s): pass
    def get_formats(self): return ["txt", "json"]
    def set_formats(self, f): pass


def make_client(tmp_path):
    conn = make_conn(tmp_path)
    return TestClient(create_app(conn, ProgressBroker(), _SettingsState())), conn


def _job_with_transcript(conn, tmp_path):
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_out = str(tmp_path / "out" / str(jid))
    text = "длинный разговор про работу " * 20
    transcripts.save(conn, jid, TranscriptResult(
        "ru", 40.0, "m", True, [Segment(0.0, 40.0, text, speaker="SPEAKER_00")]))
    job_queue.update(conn, jid, status="done", output_dir=job_out)
    return jid


class _FakeEngine:
    def unload(self): pass


def test_cancel_queued_analysis_is_instant(tmp_path):
    client, conn = make_client(tmp_path)
    jid = _job_with_transcript(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "daily", "Дейлик", "тело", "m")
    r = client.post(f"/api/analyses/{aid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    row = analyses.get(conn, aid)
    assert row["status"] == "cancelled"


def test_cancel_processing_is_cooperative(tmp_path):
    """В работе — флаг; worker останавливается на ближайшей стадии (первом же
    report), LLM даже не вызывается."""
    conn = make_conn(tmp_path)
    jid = _job_with_transcript(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "daily", "Дейлик", "тело", "m")
    row = analyses.claim_next(conn)
    assert row["status"] == "processing"
    analyses.request_cancel(aid)
    provider = FakeProvider(["# П"])
    worker.process_analysis(conn, ProgressBroker(), _FakeEngine(), provider, row,
                            settings_global=Settings(), output_dir=str(tmp_path / "out"))
    done = analyses.get(conn, aid)
    assert done["status"] == "cancelled"
    assert provider.calls == []               # до LLM не дошло
    assert not analyses.cancel_requested(aid)  # флаг очищен — не переедет на повтор


def test_cancel_done_or_missing_rejected(tmp_path):
    client, conn = make_client(tmp_path)
    jid = _job_with_transcript(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "daily", "Дейлик", "тело", "m")
    analyses.update(conn, aid, status="done", result_md="готово")
    assert client.post(f"/api/analyses/{aid}/cancel").status_code == 400
    assert client.post("/api/analyses/9999/cancel").status_code == 404


def test_requeue_after_cancel_works(tmp_path):
    """Отменённый анализ можно поставить заново — статус cancelled не блокирует."""
    client, conn = make_client(tmp_path)
    jid = _job_with_transcript(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "daily", "Дейлик", "тело", "m")
    client.post(f"/api/analyses/{aid}/cancel")
    r = client.post(f"/api/jobs/{jid}/analyses", json={"label": "daily"})
    assert r.status_code == 200
