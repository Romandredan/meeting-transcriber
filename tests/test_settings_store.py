from app import db
from app.models import Settings
from app.settings_store import SettingsStore


def test_settings_persist_across_restart(tmp_path):
    p = tmp_path / "s.db"
    conn1 = db.connect(p); db.init_schema(conn1)
    store1 = SettingsStore(conn1)
    store1.set_global(Settings(model="large-v3", vocabulary="АккордПост ОФД", diarize=False))
    store1.set_formats(["txt", "json"])
    conn1.close()

    # «Перезапуск сервера»: новое соединение к той же БД, новый store.
    conn2 = db.connect(p); db.init_schema(conn2)
    store2 = SettingsStore(conn2)
    assert store2.get_global().vocabulary == "АккордПост ОФД"
    assert store2.get_global().model == "large-v3"
    assert store2.get_global().diarize is False
    assert store2.get_formats() == ["txt", "json"]


def test_settings_store_defaults_when_empty(tmp_path):
    p = tmp_path / "s2.db"
    conn = db.connect(p); db.init_schema(conn)
    store = SettingsStore(conn)
    assert store.get_global().model == "large-v3-turbo"
    assert store.get_formats() == ["txt", "srt", "vtt", "json", "md", "docx"]
