"""Транскрипты в БД и полнотекстовый поиск: стор, бэкфилл, FTS/LIKE, API."""
import json
import os

from fastapi.testclient import TestClient

from app import analyses, db, job_queue, speakers, templates_store, transcripts, worker, writers
from app.api import create_app
from app.models import Segment, Settings, TranscriptResult, Word
from app.progress import ProgressBroker
from fakes import FakeProvider


def make_conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def make_result():
    # Тексты длинные нарочно: анализ отказывается работать с транскриптом
    # короче MIN_TRANSCRIPT_CHARS (200 символов).
    t1 = "обсудили возврат по ОРВ, " + "детали поставки и суммы, " * 4
    t2 = "акт сверки подготовлю до пятницы, " + "подготовлю и пришлю, " * 4
    return TranscriptResult("ru", 40.0, "large-v3", True, [
        Segment(0.0, 10.0, t1, speaker="SPEAKER_00",
                words=[Word(0.0, 10.0, t1, speaker="SPEAKER_00")]),
        Segment(10.0, 25.0, t2, speaker="SPEAKER_01",
                words=[Word(10.0, 25.0, t2, speaker="SPEAKER_01")]),
    ])


def test_fts5_available_after_init(tmp_path):
    """Canary: в этой сборке SQLite FTS5 есть. Если упадёт здесь — поиск
    молча деградирует на LIKE, и это повод разобраться, а не удивляться."""
    conn = make_conn(tmp_path)
    assert transcripts._fts_ok(conn)


def test_save_and_get_roundtrip(tmp_path):
    conn = make_conn(tmp_path)
    transcripts.save(conn, 1, make_result())
    got = transcripts.get(conn, 1)
    assert got is not None
    assert got.language == "ru" and got.diarized
    assert [s.text for s in got.segments] == [s.text for s in make_result().segments]
    assert transcripts.get(conn, 999) is None


def test_delete_job_purges_transcript_and_index(tmp_path):
    conn = make_conn(tmp_path)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    transcripts.save(conn, jid, make_result())
    job_queue.delete(conn, jid)
    assert transcripts.get(conn, jid) is None
    n = conn.execute("SELECT COUNT(*) c FROM search_fts WHERE job_id=?", (jid,)).fetchone()["c"]
    assert n == 0


def test_backfill_fills_from_disk_and_skips_missing(tmp_path):
    conn = make_conn(tmp_path)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_out = str(tmp_path / "out" / str(jid))
    writers.write_all(make_result(), job_out, ["json"], "a")
    job_queue.update(conn, jid, status="done", output_dir=job_out)
    ghost = job_queue.enqueue(conn, "C:/v/b.mp4", "{}")   # done без файла
    job_queue.update(conn, ghost, status="done", output_dir=str(tmp_path / "nope"))
    aid = analyses.enqueue(conn, jid, "summary", "Резюме", "тело", "m")
    analyses.update(conn, aid, status="done", result_md="итоги по возврату ОРВ")

    filled, skipped = transcripts.backfill(conn)
    assert (filled, skipped) == (1, 1)
    assert transcripts.get(conn, jid) is not None
    assert transcripts.get(conn, ghost) is None
    # повторный прогон — новых не заполняет (ghost так и пропускается)
    assert transcripts.backfill(conn)[0] == 0
    # анализ тоже проиндексирован
    hits = [r for r in transcripts.search(conn, "итоги")[0] if r["kind"] == "analysis"]
    assert hits and hits[0]["ref_id"] == aid


# ── поиск ────────────────────────────────────────────────────────────────────

def test_search_finds_replica_by_word(tmp_path):
    conn = make_conn(tmp_path)
    jid = job_queue.enqueue(conn, "C:/v/встреча.webm", "{}")
    transcripts.save(conn, jid, make_result())
    hits = transcripts.search(conn, "возврат")[0]
    assert len(hits) == 1
    h = hits[0]
    assert h["kind"] == "replica" and h["job_id"] == jid
    assert h["filename"] == "встреча.webm"
    assert h["start"] == 0.0 and h["speaker"] == "Спикер 1"
    assert "[возврат]" in h["snippet"].lower()


def test_search_multi_token_is_and(tmp_path):
    conn = make_conn(tmp_path)
    transcripts.save(conn, 1, make_result())
    assert transcripts.search(conn, "возврат акт")[0] == []        # нет реплики с обоими
    assert len(transcripts.search(conn, "акт сверки")[0]) == 1


def test_search_by_speaker_name_after_alias(tmp_path):
    """Поиск по имени находит реплики человека: индекс хранит отображаемые метки."""
    conn = make_conn(tmp_path)
    transcripts.save(conn, 1, make_result())
    speakers.set_aliases(conn, 1, {"Спикер 2": {"name": "Игорь"}})
    transcripts.index_replicas(conn, 1, make_result())
    hits = [r for r in transcripts.search(conn, "Игорь")[0] if r["kind"] == "replica"]
    assert len(hits) == 1 and hits[0]["speaker"] == "Игорь"


def test_search_analyses_not_crowded_out_by_replicas(tmp_path):
    """Частый термин: реплик больше лимита, анализы — длинные, по BM25 ниже.
    Лимит считается по видам отдельно, поэтому анализ всё равно в выдаче
    (реальный кейс: 101 реплика vs 3 анализа по имени спикера)."""
    conn = make_conn(tmp_path)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    many = [Segment(float(i), float(i) + 1, f"реплика про Антона номер {i}",
                    speaker="SPEAKER_00") for i in range(80)]
    transcripts.save(conn, jid, TranscriptResult("ru", 80.0, "m", True, many))
    aid = analyses.enqueue(conn, jid, "daily", "Дейлик", "тело", "m")
    analyses.update(conn, aid, status="done", result_md="# Итоги\n## Антон\n- сделал всё")
    transcripts.index_analysis(conn, aid, jid, "# Итоги\n## Антон\n- сделал всё")

    hits = transcripts.search(conn, "Антон", limit=50)[0]
    assert any(r["kind"] == "analysis" and r["ref_id"] == aid for r in hits)


def test_search_like_fallback_when_fts_missing(tmp_path):
    conn = make_conn(tmp_path)
    transcripts.save(conn, 1, make_result())
    conn.execute("DROP TABLE search_fts")
    conn.commit()
    assert not transcripts._fts_ok(conn)
    hits = transcripts.search(conn, "возврат")[0]
    assert len(hits) == 1 and "[возврат]" in hits[0]["snippet"].lower()


def test_search_empty_and_garbage_query(tmp_path):
    conn = make_conn(tmp_path)
    transcripts.save(conn, 1, make_result())
    assert transcripts.search(conn, "")[0] == []
    assert transcripts.search(conn, "  !!!  ")[0] == []
    assert transcripts.search(conn, "несуществующеслово")[0] == []


# ── API и worker ─────────────────────────────────────────────────────────────

class _SettingsState:
    def get_global(self): return Settings()
    def set_global(self, s): pass
    def get_formats(self): return ["txt", "json"]
    def set_formats(self, f): pass


def make_client(tmp_path):
    conn = make_conn(tmp_path)
    return TestClient(create_app(conn, ProgressBroker(), _SettingsState())), conn


def done_job(conn, tmp_path):
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_out = str(tmp_path / "out" / str(jid))
    writers.write_all(make_result(), job_out, ["txt", "json"], "a")
    job_queue.update(conn, jid, status="done", output_dir=job_out)
    transcripts.save(conn, jid, make_result())
    return jid, job_out


def test_search_endpoint(tmp_path):
    client, conn = make_client(tmp_path)
    jid, _ = done_job(conn, tmp_path)
    r = client.get("/api/search", params={"q": "возврат"})
    assert r.status_code == 200
    body = r.json()
    assert body["hits"] and body["hits"][0]["job_id"] == jid
    assert body["hits"][0]["filename"] == "a.mp4"
    assert body["total"] == {"analysis": 0, "replica": 1}


def test_search_totals_exceed_shown(tmp_path):
    """Счётчики — полные, даже когда выдача урезана лимитом («X из Y»)."""
    conn = make_conn(tmp_path)
    many = [Segment(float(i), float(i) + 1, f"реплика про Антона номер {i}",
                    speaker="SPEAKER_00") for i in range(80)]
    transcripts.save(conn, 1, TranscriptResult("ru", 80.0, "m", True, many))
    hits, totals = transcripts.search(conn, "Антон", limit=50)
    assert len(hits) == 50
    assert totals["replica"] == 80


def test_transcript_endpoint_reads_from_db_when_file_gone(tmp_path):
    """JSON на диске удалили — просмотр живёт за счёт БД."""
    client, conn = make_client(tmp_path)
    jid, job_out = done_job(conn, tmp_path)
    os.remove(os.path.join(job_out, "a.json"))
    r = client.get(f"/api/jobs/{jid}/transcript")
    assert r.status_code == 200
    assert r.json()["count"] == 2


def test_analysis_allowed_with_db_transcript_only(tmp_path):
    """Файла нет, но транскрипт в БД — анализ ставится в очередь."""
    client, conn = make_client(tmp_path)
    templates_store.seed_defaults(conn)
    jid, job_out = done_job(conn, tmp_path)
    os.remove(os.path.join(job_out, "a.json"))
    r = client.post(f"/api/jobs/{jid}/analyses", json={"label": "protocol"})
    assert r.status_code == 200


class _FakeEngine:
    def __init__(self, result=None):
        self.result = result
        self.unloaded = 0
    def transcribe(self, wav, settings, report):
        return self.result
    def unload(self):
        self.unloaded += 1


def test_process_job_saves_transcript_to_db(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    monkeypatch.setattr(worker.ffmpeg_tool, "extract_audio",
                        lambda src, dst: open(dst, "w").close())
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    row = job_queue.claim_next(conn)
    worker.process_job(conn, ProgressBroker(), _FakeEngine(make_result()), row,
                       settings_global=Settings(), formats=["txt"],
                       tmp_dir=str(tmp_path / "tmp"), output_dir=str(tmp_path / "out"))
    assert job_queue.get(conn, jid)["status"] == "done"
    assert transcripts.get(conn, jid) is not None


def test_process_analysis_reads_transcript_from_db(tmp_path):
    """JSON удалён, но в БД транскрипт есть — анализ выполняется."""
    conn = make_conn(tmp_path)
    jid, job_out = done_job(conn, tmp_path)
    os.remove(os.path.join(job_out, "a.json"))
    aid = analyses.enqueue(conn, jid, "protocol", "Протокол", "тело", "m")
    row = analyses.claim_next(conn)
    provider = FakeProvider(["# Протокол"])
    worker.process_analysis(conn, ProgressBroker(), _FakeEngine(), provider, row,
                            settings_global=Settings(), output_dir=str(tmp_path / "out"))
    assert analyses.get(conn, aid)["status"] == "done"
    # и результат проиндексирован
    hits = [r for r in transcripts.search(conn, "Протокол")[0] if r["kind"] == "analysis"]
    assert hits and hits[0]["ref_id"] == aid
