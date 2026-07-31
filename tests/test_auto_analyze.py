"""Авто-анализ после расшифровки: классификация, автопостановка, валидация."""
import json

from fastapi.testclient import TestClient

from app import analyses, analyze, db, job_queue, templates_store, worker
from app.api import create_app
from app.models import Segment, Settings, TranscriptResult
from app.progress import ProgressBroker
from fakes import FakeProvider


def make_conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    templates_store.seed_defaults(conn)
    return conn


TEMPLATES = [
    {"label": "protocol", "display_name": "Протокол встречи", "description": "решения и задачи"},
    {"label": "daily", "display_name": "Итоги дейлика", "description": "сделано/планы/блокеры"},
]


def test_classify_picks_label_from_answer():
    provider = FakeProvider(["daily"])
    assert analyze.classify_meeting(provider, "что вчера сделал", TEMPLATES) == "daily"


def test_classify_tolerates_framing():
    """Модель обрамляет ответ — вытаскиваем метку как отдельное слово."""
    provider = FakeProvider(['Метка: "protocol".'])
    assert analyze.classify_meeting(provider, "протокол", TEMPLATES) == "protocol"


def test_classify_none_and_garbage():
    assert analyze.classify_meeting(FakeProvider(["none"]), "текст", TEMPLATES) is None
    assert analyze.classify_meeting(FakeProvider(["не знаю"]), "текст", TEMPLATES) is None
    assert analyze.classify_meeting(FakeProvider([""]), "текст", TEMPLATES) is None
    assert analyze.classify_meeting(FakeProvider([RuntimeError("упал")]), "текст", TEMPLATES) is None
    assert analyze.classify_meeting(FakeProvider(["daily"]), "текст", []) is None


def test_classify_does_not_match_substring():
    """Метка внутри другого слова — не совпадение («daily» в «dailys»)."""
    provider = FakeProvider(["dailysync"])
    assert analyze.classify_meeting(provider, "текст", TEMPLATES) is None


# ── автопостановка ───────────────────────────────────────────────────────────

class _FakeEngine:
    def __init__(self, result=None):
        self.result = result
        self.unloaded = 0
    def transcribe(self, wav, settings, report):
        return self.result
    def unload(self):
        self.unloaded += 1


def make_result():
    text = "обсудили планы и блокеры " * 10
    return TranscriptResult("ru", 40.0, "large-v3", True, [
        Segment(0.0, 40.0, text, speaker="SPEAKER_00"),
    ])


def run_job(conn, tmp_path, monkeypatch, settings, provider=None):
    from app import transcripts
    monkeypatch.setattr(worker.ffmpeg_tool, "extract_audio",
                        lambda src, dst: open(dst, "w").close())
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    row = job_queue.claim_next(conn)
    engine = _FakeEngine(make_result())
    worker.process_job(conn, ProgressBroker(), engine, row,
                       settings_global=settings, formats=["json"],
                       tmp_dir=str(tmp_path / "tmp"), output_dir=str(tmp_path / "out"),
                       provider=provider)
    return jid, engine


def queued_labels(conn, jid):
    return [r["label"] for r in analyses.list_for_job(conn, jid)]


def test_auto_analyze_off_by_default(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    jid, _ = run_job(conn, tmp_path, monkeypatch, Settings(), FakeProvider())
    assert queued_labels(conn, jid) == []


def test_auto_analyze_fixed_template(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    jid, _ = run_job(conn, tmp_path, monkeypatch,
                     Settings(auto_analyze="protocol"), FakeProvider())
    assert queued_labels(conn, jid) == ["protocol"]
    row = analyses.list_for_job(conn, jid)[0]
    assert row["prompt_snapshot"]            # снимок промпта, как при ручном запуске
    assert job_queue.get(conn, jid)["status"] == "done"


def test_auto_analyze_classify_mode(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    jid, engine = run_job(conn, tmp_path, monkeypatch,
                          Settings(auto_analyze="auto"), FakeProvider(["daily"]))
    assert queued_labels(conn, jid) == ["daily"]
    assert engine.unloaded >= 1   # Whisper выгружен ДО вызова LLM-классификатора


def test_auto_analyze_classify_uncertain_skips(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    jid, _ = run_job(conn, tmp_path, monkeypatch,
                     Settings(auto_analyze="auto"), FakeProvider(["none"]))
    assert queued_labels(conn, jid) == []
    assert job_queue.get(conn, jid)["status"] == "done"   # расшифровка не пострадала


def test_auto_analyze_without_provider_is_silent(tmp_path, monkeypatch):
    """ANALYZE_ENABLED=false (provider=None) — молча пропускаем, как диаризацию
    без HF_TOKEN."""
    conn = make_conn(tmp_path)
    jid, _ = run_job(conn, tmp_path, monkeypatch,
                     Settings(auto_analyze="protocol"), provider=None)
    assert queued_labels(conn, jid) == []
    assert job_queue.get(conn, jid)["status"] == "done"


def test_auto_analyze_no_duplicate_while_active(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    jid, _ = run_job(conn, tmp_path, monkeypatch,
                     Settings(auto_analyze="protocol"), FakeProvider())
    # Повторный вызов (например, повторный тик) не должен поставить дубль.
    worker.maybe_auto_analyze(conn, _FakeEngine(), FakeProvider(), jid,
                              Settings(auto_analyze="protocol"))
    assert queued_labels(conn, jid) == ["protocol"]


def test_auto_analyze_per_job_override(tmp_path, monkeypatch):
    """Per-job settings_json перекрывает глобальную настройку."""
    conn = make_conn(tmp_path)
    monkeypatch.setattr(worker.ffmpeg_tool, "extract_audio",
                        lambda src, dst: open(dst, "w").close())
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", json.dumps({"auto_analyze": "daily"}))
    row = job_queue.claim_next(conn)
    worker.process_job(conn, ProgressBroker(), _FakeEngine(make_result()), row,
                       settings_global=Settings(auto_analyze="protocol"), formats=["json"],
                       tmp_dir=str(tmp_path / "tmp"), output_dir=str(tmp_path / "out"),
                       provider=FakeProvider())
    assert queued_labels(conn, jid) == ["daily"]


# ── валидация настройки через API ────────────────────────────────────────────

class _SettingsState:
    def __init__(self):
        self._s = Settings()
    def get_global(self): return self._s
    def set_global(self, s): self._s = s
    def get_formats(self): return ["txt", "json"]
    def set_formats(self, f): pass


def test_put_settings_auto_analyze_validation(tmp_path):
    conn = make_conn(tmp_path)
    client = TestClient(create_app(conn, ProgressBroker(), _SettingsState()))
    ok = client.put("/api/settings", json={
        "settings": Settings(auto_analyze="protocol").to_dict(), "formats": ["txt"]})
    assert ok.status_code == 200
    ok2 = client.put("/api/settings", json={
        "settings": Settings(auto_analyze="auto").to_dict(), "formats": ["txt"]})
    assert ok2.status_code == 200
    bad = client.put("/api/settings", json={
        "settings": Settings(auto_analyze="несуществующий").to_dict(), "formats": ["txt"]})
    assert bad.status_code == 400
    assert "недоступен" in bad.json()["detail"]
