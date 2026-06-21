import json
from fastapi.testclient import TestClient
from app import db, api
from app.models import Settings


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


def test_get_and_put_settings(tmp_path):
    client, _ = make_client(tmp_path)
    client.put("/api/settings", json={"settings": {"model": "large-v3"}, "formats": ["txt"]})
    got = client.get("/api/settings").json()
    assert got["settings"]["model"] == "large-v3"
    assert got["formats"] == ["txt"]
