import json
import os

import pytest
from fastapi.testclient import TestClient

from app import analyses, config, db, api, job_queue, templates_store, writers
from app.models import Segment, Settings, TranscriptResult, Word


class SettingsState:
    def __init__(self):
        self._s = Settings(); self._f = ["txt", "json"]
    def get_global(self): return self._s
    def set_global(self, s): self._s = s
    def get_formats(self): return self._f
    def set_formats(self, f): self._f = f


def make_client(tmp_path):
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    from app.progress import ProgressBroker
    app = api.create_app(conn, ProgressBroker(), SettingsState())
    return TestClient(app), conn


def test_create_job_rejects_missing_path(tmp_path):
    client, _ = make_client(tmp_path)
    r = client.post("/api/jobs", json={"path": "C:/nope/none.mp4", "settings": {}})
    assert r.status_code == 400


def test_create_and_list_job(tmp_path):
    client, _ = make_client(tmp_path)
    f = tmp_path / "a.mp4"; f.write_bytes(b"x")
    r = client.post("/api/jobs", json={"path": str(f), "settings": {"diarize": False}})
    assert r.status_code == 200
    jid = r.json()["id"]
    lst = client.get("/api/jobs").json()
    assert any(j["id"] == jid for j in lst)


def test_create_job_strips_quotes_and_whitespace(tmp_path):
    client, _ = make_client(tmp_path)
    f = tmp_path / "rec.webm"; f.write_bytes(b"x")
    # Путь как из «Копировать как путь» Проводника: в кавычках и с пробелами.
    r = client.post("/api/jobs", json={"path": f'  "{f}"  ', "settings": {}})
    assert r.status_code == 200


def _upload(client, name, data=b"audio-bytes"):
    return client.post("/api/jobs/upload", params={"filename": name}, content=data)


def test_upload_saves_to_inbox_and_enqueues(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    client, conn = make_client(tmp_path)
    # Имя вида C:\fakepath\x.mp4 браузер может прислать как «путь» — берём только имя.
    r = _upload(client, "C:\\fakepath\\встреча.mp4")
    assert r.status_code == 200
    saved = config.INBOX_DIR / "встреча.mp4"
    assert saved.read_bytes() == b"audio-bytes"
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (r.json()["id"],)).fetchone()
    assert row["source_path"] == str(saved)


def test_upload_rejects_non_media(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    client, _ = make_client(tmp_path)
    r = _upload(client, "notes.txt")
    assert r.status_code == 400
    assert not list(config.INBOX_DIR.glob("*.uploading"))


def test_upload_rejects_empty_body(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    client, _ = make_client(tmp_path)
    r = _upload(client, "a.mp4", data=b"")
    assert r.status_code == 400
    # Временный файл подчищен, в очередь ничего не встало.
    assert not list(config.INBOX_DIR.iterdir())
    assert client.get("/api/jobs").json() == []


def test_upload_does_not_overwrite_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    config.INBOX_DIR.mkdir()
    (config.INBOX_DIR / "a.mp4").write_bytes(b"old")
    client, _ = make_client(tmp_path)
    r = _upload(client, "a.mp4", data=b"new")
    assert r.status_code == 200
    assert (config.INBOX_DIR / "a.mp4").read_bytes() == b"old"
    assert (config.INBOX_DIR / "a.1.mp4").read_bytes() == b"new"


def test_get_and_put_settings(tmp_path):
    client, _ = make_client(tmp_path)
    client.put("/api/settings", json={"settings": {"model": "large-v3"}, "formats": ["txt"]})
    got = client.get("/api/settings").json()
    assert got["settings"]["model"] == "large-v3"
    assert got["formats"] == ["txt"]


def test_settings_report_diarize_availability(tmp_path, monkeypatch):
    # Без HF_TOKEN диаризация молча пропускается — фронт обязан знать об этом
    # заранее и предупредить, а не выдать транскрипт «без разделения».
    monkeypatch.setattr(config, "HF_TOKEN", None)
    client, _ = make_client(tmp_path)
    assert client.get("/api/settings").json()["diarize_available"] is False
    monkeypatch.setattr(config, "HF_TOKEN", "hf_xxx")
    assert client.get("/api/settings").json()["diarize_available"] is True


@pytest.fixture(autouse=True)
def analyze_enabled(monkeypatch):
    """Роуты анализа не должны зависеть от .env пользователя: если он поставит
    ANALYZE_ENABLED=false, десяток тестов молча получит 404 вместо внятного падения.
    Тест на выключенную фичу переопределяет флаг у себя внутри."""
    monkeypatch.setattr(config, "ANALYZE_ENABLED", True)


def done_job_with_transcript(conn, tmp_path, jid_path="C:/v/a.mp4"):
    """Готовая встреча с JSON-транскриптом на диске."""
    jid = job_queue.enqueue(conn, jid_path, "{}")
    job_out = str(tmp_path / "out" / str(jid))
    seg = Segment(0.0, 5.0, "я" * 400, speaker="SPEAKER_00",
                  words=[Word(0.0, 5.0, "я" * 400, speaker="SPEAKER_00")])
    writers.write_all(TranscriptResult("ru", 5.0, "large-v3", True, [seg]),
                      job_out, ["json"], "a")
    job_queue.update(conn, jid, status="done", output_dir=job_out)
    return jid


def test_templates_crud(tmp_path):
    client, conn = make_client(tmp_path)
    r = client.post("/api/templates", json={
        "label": "custom", "display_name": "Своё", "description": "",
        "prompt_body": "тело", "enabled": True})
    assert r.status_code == 200
    tid = r.json()["id"]
    assert any(t["label"] == "custom" for t in client.get("/api/templates").json())
    client.put(f"/api/templates/{tid}", json={
        "label": "custom", "display_name": "Своё 2", "description": "",
        "prompt_body": "тело", "enabled": False})
    got = [t for t in client.get("/api/templates").json() if t["id"] == tid][0]
    assert got["display_name"] == "Своё 2"
    assert got["enabled"] == 0
    assert client.delete(f"/api/templates/{tid}").status_code == 200
    assert all(t["id"] != tid for t in client.get("/api/templates").json())


@pytest.mark.parametrize("label", ["", "with space", "a/b", "a\\b", "a" * 33, "имя"])
def test_create_template_rejects_invalid_label(tmp_path, label):
    client, _ = make_client(tmp_path)
    r = client.post("/api/templates", json={
        "label": label, "display_name": "Т", "description": "",
        "prompt_body": "тело", "enabled": True})
    assert r.status_code == 400
    assert isinstance(r.json()["detail"], str)


def test_create_template_accepts_valid_label(tmp_path):
    client, _ = make_client(tmp_path)
    r = client.post("/api/templates", json={
        "label": "my-label_2", "display_name": "Т", "description": "",
        "prompt_body": "тело", "enabled": True})
    assert r.status_code == 200


def test_update_template_rejects_invalid_label(tmp_path):
    client, conn = make_client(tmp_path)
    tid = templates_store.create(conn, "custom", "Своё", "", "тело")
    r = client.put(f"/api/templates/{tid}", json={
        "label": "bad label", "display_name": "Своё", "description": "",
        "prompt_body": "тело", "enabled": True})
    assert r.status_code == 400


def test_create_template_rejects_duplicate_label_via_api(tmp_path):
    """Дубль метки — штатный отказ 400 с русским текстом, а не 500 от sqlite3.IntegrityError."""
    client, _ = make_client(tmp_path)
    body = {"label": "dup", "display_name": "А", "description": "",
            "prompt_body": "тело", "enabled": True}
    assert client.post("/api/templates", json=body).status_code == 200
    r = client.post("/api/templates", json={**body, "display_name": "Б"})
    assert r.status_code == 400
    assert "dup" in r.json()["detail"]


def test_post_analysis_enqueues(tmp_path):
    client, conn = make_client(tmp_path)
    templates_store.seed_defaults(conn)
    jid = done_job_with_transcript(conn, tmp_path)
    r = client.post(f"/api/jobs/{jid}/analyses", json={"label": "protocol"})
    assert r.status_code == 200
    row = analyses.get(conn, r.json()["id"])
    assert row["status"] == "queued"
    assert row["display_name"] == "Протокол встречи"
    assert row["prompt_snapshot"]      # снимок промпта записан


def test_post_analysis_rejects_unfinished_job(tmp_path):
    client, conn = make_client(tmp_path)
    templates_store.seed_defaults(conn)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")   # остался queued
    r = client.post(f"/api/jobs/{jid}/analyses", json={"label": "protocol"})
    assert r.status_code == 400


def test_post_analysis_rejects_disabled_label(tmp_path):
    client, conn = make_client(tmp_path)
    templates_store.seed_defaults(conn)
    tpl = templates_store.get_by_label(conn, "summary")
    templates_store.update(conn, tpl["id"], enabled=False)
    jid = done_job_with_transcript(conn, tmp_path)
    assert client.post(f"/api/jobs/{jid}/analyses", json={"label": "summary"}).status_code == 400
    assert client.post(f"/api/jobs/{jid}/analyses", json={"label": "нет"}).status_code == 400


def test_post_analysis_rejects_missing_transcript(tmp_path):
    client, conn = make_client(tmp_path)
    templates_store.seed_defaults(conn)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_queue.update(conn, jid, status="done", output_dir=str(tmp_path / "nope"))
    r = client.post(f"/api/jobs/{jid}/analyses", json={"label": "protocol"})
    assert r.status_code == 400
    assert "транскрипт" in r.json()["detail"]


def test_post_analysis_rejects_duplicate_while_active(tmp_path):
    """Двойной клик не должен ставить в очередь два одинаковых анализа."""
    client, conn = make_client(tmp_path)
    templates_store.seed_defaults(conn)
    jid = done_job_with_transcript(conn, tmp_path)
    first = client.post(f"/api/jobs/{jid}/analyses", json={"label": "protocol"})
    assert first.status_code == 200
    second = client.post(f"/api/jobs/{jid}/analyses", json={"label": "protocol"})
    assert second.status_code == 400
    assert "protocol" in second.json()["detail"]


def test_post_analysis_allows_rerun_after_done(tmp_path):
    """'done' не блокирует — повторный прогон той же метки штатный сценарий."""
    client, conn = make_client(tmp_path)
    templates_store.seed_defaults(conn)
    jid = done_job_with_transcript(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "protocol", "Протокол", "п", "m")
    analyses.update(conn, aid, status="done", result_md="готово")
    r = client.post(f"/api/jobs/{jid}/analyses", json={"label": "protocol"})
    assert r.status_code == 200


def test_list_analyses_returns_history_newest_first(tmp_path):
    client, conn = make_client(tmp_path)
    jid = done_job_with_transcript(conn, tmp_path)
    old = analyses.enqueue(conn, jid, "protocol", "Протокол", "п", "m")
    analyses.update(conn, old, status="done", result_md="старый")
    new = analyses.enqueue(conn, jid, "protocol", "Протокол", "п", "m")
    analyses.update(conn, new, status="done", result_md="новый")
    got = client.get(f"/api/jobs/{jid}/analyses").json()
    assert [a["id"] for a in got] == [new, old]
    assert got[0]["result_md"] == "новый"


def test_download_old_version_comes_from_db(tmp_path):
    """На диске лежит только последняя версия — старую отдаём из result_md."""
    client, conn = make_client(tmp_path)
    jid = done_job_with_transcript(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "protocol", "Протокол", "п", "m")
    analyses.update(conn, aid, status="done", result_md="# Старая версия")
    r = client.get(f"/api/analyses/{aid}/download")
    assert r.status_code == 200
    assert "Старая версия" in r.text


def test_download_unfinished_analysis_404(tmp_path):
    client, conn = make_client(tmp_path)
    jid = done_job_with_transcript(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "protocol", "Протокол", "п", "m")
    assert client.get(f"/api/analyses/{aid}/download").status_code == 404


def test_delete_analysis(tmp_path):
    client, conn = make_client(tmp_path)
    jid = done_job_with_transcript(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "protocol", "Протокол", "п", "m")
    assert client.delete(f"/api/analyses/{aid}").status_code == 200
    assert analyses.get(conn, aid) is None


def test_llm_health_reports_provider_state(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path)
    monkeypatch.setattr(api.llm, "make_provider", lambda: _HealthStub())
    h = client.get("/api/llm/health").json()
    assert h["enabled"] is True
    assert h["ok"] is True
    assert h["warning"] == "частично в VRAM"


class _HealthStub:
    def health(self):
        return {"ok": True, "model": "qwen3:14b", "installed": True,
                "warning": "частично в VRAM", "error": None}


def test_analyze_disabled_hides_routes(tmp_path, monkeypatch):
    """ANALYZE_ENABLED=false: роуты не регистрируются, health честно говорит «выключено»."""
    monkeypatch.setattr(config, "ANALYZE_ENABLED", False)
    client, conn = make_client(tmp_path)
    assert client.get("/api/templates").status_code == 404
    jid = done_job_with_transcript(conn, tmp_path)
    assert client.post(f"/api/jobs/{jid}/analyses", json={"label": "protocol"}).status_code == 404
    assert client.get("/api/llm/health").json() == {"enabled": False}
