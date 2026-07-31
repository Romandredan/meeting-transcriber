"""Ручное редактирование: анализа (6a) и реплик транскрипта (6b)."""
import json
import os

from fastapi.testclient import TestClient

from app import analyses, db, job_queue, templates_store, transcripts, worker, writers
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


def make_result():
    t1 = "обсудили возврат по ОРВ, " + "детали поставки и суммы, " * 4
    t2 = "акт сверки подготовлю до пятницы, " + "подготовлю и пришлю, " * 4
    return TranscriptResult("ru", 40.0, "large-v3", True, [
        Segment(0.0, 10.0, t1, speaker="SPEAKER_00"),
        Segment(10.0, 25.0, t2, speaker="SPEAKER_01"),
    ])


def done_job(conn, tmp_path):
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_out = str(tmp_path / "out" / str(jid))
    writers.write_all(make_result(), job_out, ["txt", "json"], "a")
    job_queue.update(conn, jid, status="done", output_dir=job_out)
    transcripts.save(conn, jid, make_result())
    return jid, job_out


# ── 6a: правка анализа ───────────────────────────────────────────────────────

def test_edit_analysis_saves_marks_and_reindexes(tmp_path):
    client, conn = make_client(tmp_path)
    jid, _ = done_job(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "daily", "Дейлик", "тело", "m")
    analyses.update(conn, aid, status="done", result_md="# Старый текст")
    transcripts.index_analysis(conn, aid, jid, "# Старый текст")

    r = client.put(f"/api/analyses/{aid}", json={"result_md": "# Новый текст про ревизию"})
    assert r.status_code == 200
    row = analyses.get(conn, aid)
    assert row["result_md"] == "# Новый текст про ревизию"
    assert row["edited"] == 1
    # FTS: новое находится, старое — нет (точное совпадение токена, без
    # стемминга: ищем именно «ревизию», как в тексте!)
    assert transcripts.search(conn, "ревизию")[1]["analysis"] == 1
    assert transcripts.search(conn, "Старый")[1]["analysis"] == 0


def test_edit_analysis_validates(tmp_path):
    client, conn = make_client(tmp_path)
    jid, _ = done_job(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "daily", "Дейлик", "тело", "m")
    analyses.update(conn, aid, status="done", result_md="текст")
    assert client.put(f"/api/analyses/{aid}", json={"result_md": "   "}).status_code == 400
    assert client.put("/api/analyses/9999", json={"result_md": "x"}).status_code == 404
    assert analyses.get(conn, aid)["edited"] == 0


def test_regeneration_resets_edited_flag(tmp_path):
    conn = make_conn(tmp_path)
    jid, _ = done_job(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "daily", "Дейлик", "тело", "m")
    analyses.update(conn, aid, status="done", result_md="правленое", edited=1)
    analyses.update(conn, aid, status="queued")   # перегенерация
    row = analyses.claim_next(conn)

    class _E:
        def unload(self): pass
    worker.process_analysis(conn, ProgressBroker(), _E(), FakeProvider(["# Свежий"]), row,
                            settings_global=Settings(), output_dir=str(tmp_path / "out"))
    done = analyses.get(conn, aid)
    assert done["status"] == "done" and done["edited"] == 0


# ── 6b: правка реплик транскрипта ────────────────────────────────────────────

def test_patch_replica_updates_db_index_and_files(tmp_path):
    client, conn = make_client(tmp_path)
    jid, job_out = done_job(conn, tmp_path)
    r = client.patch(f"/api/jobs/{jid}/transcript",
                     json={"start": 0.0, "end": 10.0,
                           "text": "обсудили возврат по ОРВ с Ольгой"})
    assert r.status_code == 200
    got = transcripts.get(conn, jid)
    assert "с Ольгой" in got.segments[0].text
    # FTS: новое слово находится
    assert transcripts.search(conn, "Ольгой")[1]["replica"] == 1
    # TXT на диске перегенерирован с правкой
    txt = open(os.path.join(job_out, "a.txt"), encoding="utf-8").read()
    assert "с Ольгой" in txt


def test_patch_replica_collapses_stitched_span(tmp_path):
    """Абзац из нескольких сырых сегментов одного спикера сворачивается в
    один сегмент с отредактированным текстом (границы и спикер — от прогона)."""
    client, conn = make_client(tmp_path)
    jid, _ = done_job(conn, tmp_path)
    segs = json.loads(conn.execute(
        "SELECT segments_json FROM transcripts WHERE job_id=?", (jid,)).fetchone()[0])
    # Добавим третий сегмент того же спикера подряд: абзац 0–10 склеен из двух.
    segs.insert(1, {"start": 5.0, "end": 10.0, "text": "продолжение мысли",
                    "speaker": "SPEAKER_00"})
    segs[0]["end"] = 5.0
    conn.execute("UPDATE transcripts SET segments_json=? WHERE job_id=?",
                 (json.dumps(segs, ensure_ascii=False), jid))
    conn.commit()
    r = client.patch(f"/api/jobs/{jid}/transcript",
                     json={"start": 0.0, "end": 10.0, "text": "одна правленая реплика"})
    assert r.status_code == 200
    got = transcripts.get(conn, jid)
    assert len(got.segments) == 2
    assert got.segments[0].text == "одна правленая реплика"
    assert got.segments[0].start == 0.0 and got.segments[0].end == 10.0
    assert got.segments[0].speaker == "SPEAKER_00"


def test_patch_replica_validation(tmp_path):
    client, conn = make_client(tmp_path)
    jid, _ = done_job(conn, tmp_path)
    assert client.patch(f"/api/jobs/{jid}/transcript",
                        json={"start": 999.0, "end": 1000.0, "text": "x"}).status_code == 400
    assert client.patch(f"/api/jobs/{jid}/transcript",
                        json={"start": 0.0, "end": 10.0, "text": "  "}).status_code == 400
    assert client.patch("/api/jobs/9999/transcript",
                        json={"start": 0.0, "end": 1.0, "text": "x"}).status_code == 404
    # исходный текст не пострадал
    assert transcripts.search(conn, "возврат")[1]["replica"] == 1


def test_patch_replica_requires_db_transcript(tmp_path):
    client, conn = make_client(tmp_path)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_queue.update(conn, jid, status="done", output_dir=str(tmp_path / "out"))
    r = client.patch(f"/api/jobs/{jid}/transcript",
                     json={"start": 0.0, "end": 1.0, "text": "x"})
    assert r.status_code == 400
    assert "транскрипт" in r.json()["detail"]


def test_analysis_uses_edited_transcript(tmp_path):
    """Вход анализа — из БД: правка реплики видна LLM."""
    conn = make_conn(tmp_path)
    jid, _ = done_job(conn, tmp_path)
    # правим напрямую в БД (то, что делает PATCH)
    result = transcripts.get(conn, jid)
    segs = json.loads(conn.execute(
        "SELECT segments_json FROM transcripts WHERE job_id=?", (jid,)).fetchone()[0])
    segs[0]["text"] = "секретное слово эльплибо " + "очень важные детали, " * 6
    conn.execute("UPDATE transcripts SET segments_json=? WHERE job_id=?",
                 (json.dumps(segs, ensure_ascii=False), jid))
    conn.commit()

    aid = analyses.enqueue(conn, jid, "protocol", "Протокол", "тело", "m")
    row = analyses.claim_next(conn)
    provider = FakeProvider(["# П"])

    class _E:
        def unload(self): pass
    worker.process_analysis(conn, ProgressBroker(), _E(), provider, row,
                            settings_global=Settings(), output_dir=str(tmp_path / "out"))
    assert "эльплибо" in provider.calls[0][1]
