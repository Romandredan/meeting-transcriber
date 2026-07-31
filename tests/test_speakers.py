"""Имена и объединения спикеров: стор, конвейер чтения, валидация, API, анализ."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from app import analyses, db, job_queue, speakers, worker, writers
from app.api import create_app
from app.models import Segment, Settings, TranscriptResult, Word
from app.progress import ProgressBroker
from fakes import FakeProvider


def make_conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def make_result(two_speakers=True):
    segs = [
        Segment(0.0, 10.0, "привет, начнём", speaker="SPEAKER_00",
                words=[Word(0.0, 10.0, "привет, начнём", speaker="SPEAKER_00")]),
    ]
    if two_speakers:
        segs.append(Segment(10.0, 25.0, "да, давайте", speaker="SPEAKER_01",
                            words=[Word(10.0, 25.0, "да, давайте", speaker="SPEAKER_01")]))
        segs.append(Segment(25.0, 40.0, "по плану так", speaker="SPEAKER_01",
                            words=[Word(25.0, 40.0, "по плану так", speaker="SPEAKER_01")]))
    return TranscriptResult("ru", 40.0, "large-v3", True, segs)


# ── стор ─────────────────────────────────────────────────────────────────────

def test_aliases_roundtrip(tmp_path):
    conn = make_conn(tmp_path)
    speakers.set_aliases(conn, 1, {
        "Спикер 1": {"name": "Роман", "merged_into": None},
        "Спикер 2": {"name": "", "merged_into": "Спикер 1"},
        "Спикер 3": {"name": "", "merged_into": None},   # пустое — не храним
    })
    got = speakers.get_aliases(conn, 1)
    assert got == {
        "Спикер 1": {"name": "Роман", "merged_into": None},
        "Спикер 2": {"name": "", "merged_into": "Спикер 1"},
    }


def test_delete_job_cascades_aliases(tmp_path):
    conn = make_conn(tmp_path)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    speakers.set_aliases(conn, jid, {"Спикер 1": {"name": "Роман"}})
    job_queue.delete(conn, jid)
    assert speakers.get_aliases(conn, jid) == {}


# ── конвейер чтения ──────────────────────────────────────────────────────────

def test_raw_speakers_sorted_by_number():
    result = make_result()
    result.segments.append(Segment(40.0, 50.0, "x", speaker="SPEAKER_10"))
    assert speakers.raw_speakers(result) == ["Спикер 1", "Спикер 2", "Спикер 11"]


def test_apply_view_without_aliases_humanizes_raw_labels():
    """Без алиасов сырые SPEAKER_00 превращаются в «Спикер 1» — сырой вид
    наружу (UI, файлы) не отдаём нигде."""
    result = make_result()
    view = speakers.apply_view(result, {})
    assert [s.speaker for s in view.segments] == ["Спикер 1", "Спикер 2", "Спикер 2"]
    assert result.segments[0].speaker == "SPEAKER_00"   # исходник не тронут


def test_apply_view_renames_and_merges():
    result = make_result()
    view = speakers.apply_view(result, {
        "Спикер 1": {"name": "Роман"},
        "Спикер 2": {"name": "", "merged_into": "Спикер 1"},
    })
    assert [s.speaker for s in view.segments] == ["Роман", "Роман", "Роман"]
    assert view.segments[1].words[0].speaker == "Роман"
    # Исходный объект не тронут — сырые метки на месте.
    assert result.segments[1].speaker == "SPEAKER_01"


def test_apply_view_merge_without_name_keeps_target_label():
    result = make_result()
    view = speakers.apply_view(result, {
        "Спикер 2": {"name": "", "merged_into": "Спикер 1"},
    })
    assert [s.speaker for s in view.segments] == ["Спикер 1"] * 3


def test_view_passes_through_writers_with_names():
    """Имя с цифрой не должно превращаться обратно в «Спикер N» при рендере
    (humanize_speaker идемпотентен на человеческих метках)."""
    result = make_result()
    view = speakers.apply_view(result, {"Спикер 1": {"name": "Роман II"}})
    txt = writers.to_txt(view)
    assert "Роман II: привет" in txt
    assert "Спикер 3" not in txt


def test_merged_speakers_stitch_into_one_paragraph():
    """Слитые спикеры склеиваются merge_speaker_runs в один абзац протокола."""
    result = make_result()
    view = speakers.apply_view(result, {
        "Спикер 2": {"name": "", "merged_into": "Спикер 1"},
    })
    txt = writers.to_txt(view)
    assert txt.count("Спикер 1:") == 1   # три реплики → один абзац


def test_validate_aliases_errors():
    known = ["Спикер 1", "Спикер 2"]
    assert speakers.validate_aliases({"Спикер 9": {"name": "x"}}, known)
    assert speakers.validate_aliases({"Спикер 1": {"name": "я" * 65}}, known)
    assert speakers.validate_aliases(
        {"Спикер 1": {"merged_into": "Спикер 1"}}, known)
    assert speakers.validate_aliases(
        {"Спикер 1": {"merged_into": "Спикер 9"}}, known)
    # цепочка: цель сама слита
    assert speakers.validate_aliases(
        {"Спикер 1": {"merged_into": "Спикер 2"},
         "Спикер 2": {"merged_into": "Спикер 1"}}, known)
    assert speakers.validate_aliases(
        {"Спикер 2": {"merged_into": "Спикер 1"},
         "Спикер 1": {"name": "Роман"}}, known) is None


def test_speaker_stats():
    stats = speakers.speaker_stats(make_result())
    assert [s["label"] for s in stats] == ["Спикер 1", "Спикер 2"]
    assert stats[0]["utterances"] == 1
    assert stats[1]["utterances"] == 2
    assert stats[1]["seconds"] == 30.0
    assert stats[0]["preview"] == "привет, начнём"


def test_rerender_rewrites_existing_formats_only(tmp_path):
    out = tmp_path / "out"
    writers.write_all(make_result(), str(out), ["txt", "json"], "a")
    aliases = {"Спикер 1": {"name": "Роман"}}
    speakers.rerender_outputs(make_result(), str(out), "a", aliases)
    assert "Роман: привет" in (out / "a.txt").read_text(encoding="utf-8")
    # JSON остался сырым, небывшие форматы не появились.
    assert "SPEAKER_00" in (out / "a.json").read_text(encoding="utf-8")
    assert not (out / "a.md").exists()


def test_rerender_missing_dir_is_noop(tmp_path):
    speakers.rerender_outputs(make_result(), str(tmp_path / "nope"), "a", {})


# ── API ──────────────────────────────────────────────────────────────────────

class _SettingsState:
    def get_global(self): return Settings()
    def set_global(self, s): pass
    def get_formats(self): return ["txt", "json"]
    def set_formats(self, f): pass


def make_client(tmp_path):
    conn = make_conn(tmp_path)
    app = create_app(conn, ProgressBroker(), _SettingsState())
    return TestClient(app), conn


def done_job_with_files(conn, tmp_path, formats=("txt", "json")):
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_out = str(tmp_path / "out" / str(jid))
    writers.write_all(make_result(), job_out, list(formats), "a")
    job_queue.update(conn, jid, status="done", output_dir=job_out)
    return jid, job_out


def test_get_speakers_returns_stats_and_empty_aliases(tmp_path):
    client, conn = make_client(tmp_path)
    jid, _ = done_job_with_files(conn, tmp_path)
    rows = client.get(f"/api/jobs/{jid}/speakers").json()["speakers"]
    assert [r["label"] for r in rows] == ["Спикер 1", "Спикер 2"]
    assert rows[0]["name"] == "" and rows[0]["merged_into"] is None
    assert rows[1]["utterances"] == 2


def test_get_speakers_empty_without_diarization(tmp_path):
    client, conn = make_client(tmp_path)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_out = str(tmp_path / "out" / str(jid))
    seg = Segment(0.0, 10.0, "монолог", speaker=None)
    writers.write_all(TranscriptResult("ru", 10.0, "m", False, [seg]),
                      job_out, ["json"], "a")
    job_queue.update(conn, jid, status="done", output_dir=job_out)
    assert client.get(f"/api/jobs/{jid}/speakers").json()["speakers"] == []


def test_transcript_returns_stitched_replicas(tmp_path):
    """Панель получает те же склеенные абзацы, что TXT: две соседние реплики
    одного спикера — одна строка, а не секундные обрывки Whisper."""
    client, conn = make_client(tmp_path)
    jid, _ = done_job_with_files(conn, tmp_path)
    t = client.get(f"/api/jobs/{jid}/transcript").json()
    # SPEAKER_01 идёт два сегмента подряд → одна склеенная реплика.
    assert t["count"] == 2
    assert [s["speaker"] for s in t["segments"]] == ["Спикер 1", "Спикер 2"]
    assert t["segments"][1]["text"] == "да, давайте по плану так"
    assert t["segments"][1]["start"] == 10.0 and t["segments"][1]["end"] == 40.0


def test_put_speakers_then_transcript_shows_names(tmp_path):
    client, conn = make_client(tmp_path)
    jid, _ = done_job_with_files(conn, tmp_path)
    r = client.put(f"/api/jobs/{jid}/speakers", json={"aliases": [
        {"label": "Спикер 1", "name": "Роман"},
        {"label": "Спикер 2", "merged_into": "Спикер 1"},
    ]})
    assert r.status_code == 200
    t = client.get(f"/api/jobs/{jid}/transcript").json()
    assert t["speakers"] == ["Роман"]
    assert {s["speaker"] for s in t["segments"]} == {"Роман"}
    # И в сводке для строки очереди — один спикер после слияния.
    assert client.get(f"/api/jobs/{jid}/transcript", params={"meta": True}).json()["speakers"] == ["Роман"]


def test_put_speakers_rerenders_txt_on_disk(tmp_path):
    client, conn = make_client(tmp_path)
    jid, job_out = done_job_with_files(conn, tmp_path)
    client.put(f"/api/jobs/{jid}/speakers", json={"aliases": [
        {"label": "Спикер 1", "name": "Роман"}]})
    txt = open(os.path.join(job_out, "a.txt"), encoding="utf-8").read()
    assert "Роман: привет" in txt


def test_put_speakers_validation_error_keeps_old_aliases(tmp_path):
    client, conn = make_client(tmp_path)
    jid, _ = done_job_with_files(conn, tmp_path)
    r = client.put(f"/api/jobs/{jid}/speakers", json={"aliases": [
        {"label": "Спикер 1", "name": "Роман"},
        {"label": "Спикер 2", "merged_into": "Спикер 9"}]})
    assert r.status_code == 400
    assert "Спикер 9" in r.json()["detail"]
    assert speakers.get_aliases(conn, jid) == {}


def test_put_speakers_requires_transcript(tmp_path):
    client, conn = make_client(tmp_path)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_queue.update(conn, jid, status="done", output_dir=str(tmp_path / "nope"))
    r = client.put(f"/api/jobs/{jid}/speakers", json={"aliases": []})
    assert r.status_code == 404


# ── вход анализа ─────────────────────────────────────────────────────────────

class _FakeEngine:
    def __init__(self): self.unloaded = 0
    def unload(self): self.unloaded += 1


def test_analysis_input_uses_speaker_names(tmp_path):
    """LLM получает реплики уже с именами («Роман: …»), а не с метками."""
    conn = make_conn(tmp_path)
    jid, job_out = done_job_with_files(conn, tmp_path)
    speakers.set_aliases(conn, jid, {
        "Спикер 1": {"name": "Роман"},
        "Спикер 2": {"name": "", "merged_into": "Спикер 1"},
    })
    aid = analyses.enqueue(conn, jid, "protocol", "Протокол", "тело", "m")
    row = analyses.claim_next(conn)
    provider = FakeProvider(["# П"])
    # Реплики короткие — удлиним, чтобы пройти MIN_TRANSCRIPT_CHARS.
    out_json = os.path.join(job_out, "a.json")
    data = json.loads(open(out_json, encoding="utf-8").read())
    for s in data["segments"]:
        s["text"] = s["text"] + " " + "слово " * 60
    open(out_json, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))

    worker.process_analysis(conn, ProgressBroker(), _FakeEngine(), provider, row,
                            settings_global=Settings(), output_dir=str(tmp_path / "out"))
    assert analyses.get(conn, aid)["status"] == "done"
    user_text = provider.calls[0][1]
    assert "Роман:" in user_text
    assert "Спикер 1:" not in user_text and "Спикер 2:" not in user_text
