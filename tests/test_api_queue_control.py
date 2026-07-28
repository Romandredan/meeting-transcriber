import json
import os

from app import analyses, db, api, job_queue
from app.models import Segment, Settings, TranscriptResult

from tests.test_api import SettingsState, make_client   # переиспользуем фикстуры


def _done_job(client, conn, tmp_path, name="rec.webm", segments=None):
    """Готовая встреча с транскриптом на диске — как после успешного worker'а."""
    src = tmp_path / name
    src.write_bytes(b"x")
    jid = client.post("/api/jobs", json={"path": str(src), "settings": {}}).json()["id"]
    out = tmp_path / "out" / str(jid)
    out.mkdir(parents=True)
    result = TranscriptResult(
        language="ru", duration=95.0, model="large-v3-turbo", diarized=True,
        segments=segments or [
            Segment(start=0.0, end=4.0, text="Первая реплика", speaker="Спикер 1"),
            Segment(start=4.0, end=9.0, text="Вторая реплика", speaker="Спикер 2"),
        ])
    base = os.path.splitext(name)[0]
    (out / f"{base}.json").write_text(json.dumps(result.to_dict()), encoding="utf-8")
    (out / f"{base}.txt").write_text("текст", encoding="utf-8")
    job_queue.update(conn, jid, status="done", progress=1.0, output_dir=str(out))
    return jid


def test_cancel_queued_job_marks_cancelled(tmp_path):
    client, conn = make_client(tmp_path)
    f = tmp_path / "a.mp4"; f.write_bytes(b"x")
    jid = client.post("/api/jobs", json={"path": str(f), "settings": {}}).json()["id"]
    r = client.post(f"/api/jobs/{jid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert job_queue.get(conn, jid)["status"] == "cancelled"


def test_cancel_processing_only_requests(tmp_path):
    client, conn = make_client(tmp_path)
    f = tmp_path / "a.mp4"; f.write_bytes(b"x")
    jid = client.post("/api/jobs", json={"path": str(f), "settings": {}}).json()["id"]
    job_queue.update(conn, jid, status="processing")
    r = client.post(f"/api/jobs/{jid}/cancel")
    assert r.json()["status"] == "cancelling"
    assert job_queue.cancel_requested(jid)
    # Статус меняет воркер, а не API: работа ещё идёт.
    assert job_queue.get(conn, jid)["status"] == "processing"
    job_queue.clear_cancel(jid)


def test_cancel_done_job_rejected(tmp_path):
    client, conn = make_client(tmp_path)
    jid = _done_job(client, conn, tmp_path)
    assert client.post(f"/api/jobs/{jid}/cancel").status_code == 400


def test_requeue_returns_job_to_queue(tmp_path):
    client, conn = make_client(tmp_path)
    f = tmp_path / "a.mp4"; f.write_bytes(b"x")
    jid = client.post("/api/jobs", json={"path": str(f), "settings": {}}).json()["id"]
    job_queue.update(conn, jid, status="error", error="CUDA out of memory")
    assert client.post(f"/api/jobs/{jid}/requeue").status_code == 200
    row = job_queue.get(conn, jid)
    assert row["status"] == "queued" and not row["error"]


def test_requeue_without_source_file_rejected(tmp_path):
    client, conn = make_client(tmp_path)
    f = tmp_path / "gone.mp4"; f.write_bytes(b"x")
    jid = client.post("/api/jobs", json={"path": str(f), "settings": {}}).json()["id"]
    job_queue.update(conn, jid, status="error", error="boom")
    f.unlink()
    assert client.post(f"/api/jobs/{jid}/requeue").status_code == 400


def test_delete_job_removes_analyses_but_keeps_files(tmp_path):
    client, conn = make_client(tmp_path)
    jid = _done_job(client, conn, tmp_path)
    analyses.enqueue(conn, jid, "protocol", "Протокол", "промпт", "qwen3:14b")
    out_dir = job_queue.get(conn, jid)["output_dir"]
    assert client.delete(f"/api/jobs/{jid}").status_code == 200
    assert job_queue.get(conn, jid) is None
    assert analyses.list_for_job(conn, jid) == []
    assert os.path.isdir(out_dir)         # результаты на диске остались


def test_delete_job_with_purge_removes_output(tmp_path):
    client, conn = make_client(tmp_path)
    jid = _done_job(client, conn, tmp_path)
    out_dir = job_queue.get(conn, jid)["output_dir"]
    assert client.delete(f"/api/jobs/{jid}?purge=true").status_code == 200
    assert not os.path.isdir(out_dir)


def test_transcript_meta_and_segments(tmp_path):
    client, conn = make_client(tmp_path)
    jid = _done_job(client, conn, tmp_path)
    meta = client.get(f"/api/jobs/{jid}/transcript?meta=1").json()
    assert meta["language"] == "ru" and meta["count"] == 2
    assert meta["speakers"] == ["Спикер 1", "Спикер 2"]
    assert "segments" not in meta
    full = client.get(f"/api/jobs/{jid}/transcript").json()
    assert [s["text"] for s in full["segments"]] == ["Первая реплика", "Вторая реплика"]


def test_transcript_missing_json_is_404(tmp_path):
    client, conn = make_client(tmp_path)
    jid = _done_job(client, conn, tmp_path)
    out = job_queue.get(conn, jid)["output_dir"]
    os.remove(os.path.join(out, "rec.json"))
    assert client.get(f"/api/jobs/{jid}/transcript").status_code == 404


def test_files_lists_only_existing_formats(tmp_path):
    client, conn = make_client(tmp_path)
    jid = _done_job(client, conn, tmp_path)
    assert client.get(f"/api/jobs/{jid}/files").json()["formats"] == ["txt", "json"]
