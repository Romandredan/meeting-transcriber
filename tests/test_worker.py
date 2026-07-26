import json
import os

from app import analyses, db, job_queue, worker, writers
from app.models import Settings, TranscriptResult, Segment, Word
from app.progress import ProgressBroker
from fakes import FakeProvider


class FakeEngine:
    def __init__(self):
        self.unloaded = 0

    def transcribe(self, audio_path, settings, progress):
        progress("transcribe", 0.5)
        w = Word(0.0, 1.0, "тест", speaker="SPEAKER_00")
        s = Segment(0.0, 1.0, "тест", speaker="SPEAKER_00", words=[w])
        return TranscriptResult("ru", 1.0, settings.model, True, [s])

    def unload(self):
        self.unloaded += 1


def test_move_to_processed_moves_inbox_file_preserving_subpath(tmp_path):
    inbox = tmp_path / "inbox"; processed = tmp_path / "processed"
    inbox.mkdir(); processed.mkdir()
    f = inbox / "sub" / "a.mp4"; f.parent.mkdir(); f.write_bytes(b"x")
    dst = worker.move_to_processed(str(f), str(inbox), str(processed))
    assert dst is not None
    assert not f.exists()
    assert (processed / "sub" / "a.mp4").exists()  # подпапка сохранена


def test_move_to_processed_leaves_external_file_untouched(tmp_path):
    inbox = tmp_path / "inbox"; processed = tmp_path / "processed"
    inbox.mkdir(); processed.mkdir()
    ext = tmp_path / "elsewhere" / "b.mp4"; ext.parent.mkdir(); ext.write_bytes(b"x")
    dst = worker.move_to_processed(str(ext), str(inbox), str(processed))
    assert dst is None
    assert ext.exists()  # оригинал пользователя (добавлен по пути) не трогаем


def test_move_to_processed_no_overwrite(tmp_path):
    inbox = tmp_path / "inbox"; processed = tmp_path / "processed"
    inbox.mkdir(); processed.mkdir()
    (processed / "a.mp4").write_bytes(b"old")  # уже есть файл с таким именем
    f = inbox / "a.mp4"; f.write_bytes(b"new")
    dst = worker.move_to_processed(str(f), str(inbox), str(processed))
    assert dst is not None
    assert (processed / "a.mp4").read_bytes() == b"old"  # не перезаписан
    assert (processed / "a.1.mp4").exists()  # новый ушёл с суффиксом


def test_merge_settings_override():
    g = Settings(model="large-v3-turbo", diarize=True)
    merged = worker.merge_settings(g, {"model": "large-v3", "diarize": False})
    assert merged.model == "large-v3"
    assert merged.diarize is False


def test_process_job_writes_outputs_and_marks_done(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    monkeypatch.setattr(worker.ffmpeg_tool, "extract_audio", lambda src, dst: open(dst, "w").close())
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", json.dumps({"diarize": True}))
    row = job_queue.claim_next(conn)
    worker.process_job(conn, ProgressBroker(), FakeEngine(), row,
                       settings_global=Settings(), formats=["txt", "json"],
                       tmp_dir=str(tmp_path / "tmp"), output_dir=str(tmp_path / "out"))
    done = job_queue.get(conn, jid)
    assert done["status"] == "done"
    assert done["progress"] == 1.0
    assert done["output_dir"]


def test_process_job_marks_error_on_ffmpeg_failure(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    def boom(src, dst): raise worker.ffmpeg_tool.FFmpegError("нет аудио")
    monkeypatch.setattr(worker.ffmpeg_tool, "extract_audio", boom)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    row = job_queue.claim_next(conn)
    worker.process_job(conn, ProgressBroker(), FakeEngine(), row,
                       settings_global=Settings(), formats=["txt"],
                       tmp_dir=str(tmp_path / "tmp"), output_dir=str(tmp_path / "out"))
    err = job_queue.get(conn, jid)
    assert err["status"] == "error"
    assert "аудио" in err["error"]


def _job_with_transcript(conn, tmp_path, text="я" * 400):
    """Готовый job с записанным на диск JSON-транскриптом."""
    out = tmp_path / "out"
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_out = str(out / str(jid))
    seg = Segment(0.0, 10.0, text, speaker="SPEAKER_00",
                  words=[Word(0.0, 10.0, text, speaker="SPEAKER_00")])
    writers.write_all(TranscriptResult("ru", 10.0, "large-v3", True, [seg]),
                      job_out, ["json"], "a")
    job_queue.update(conn, jid, status="done", output_dir=job_out)
    return jid, str(out)


def test_process_analysis_writes_md_and_marks_done(tmp_path):
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    jid, out_dir = _job_with_transcript(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "protocol", "Протокол", "тело шаблона", "qwen3:14b")
    row = analyses.claim_next(conn)
    engine, provider = FakeEngine(), FakeProvider(["# Протокол\nтекст"])
    worker.process_analysis(conn, ProgressBroker(), engine, provider, row,
                            settings_global=Settings(), output_dir=out_dir)
    done = analyses.get(conn, aid)
    assert done["status"] == "done"
    assert done["progress"] == 1.0
    assert done["result_md"] == "# Протокол\nтекст"
    assert os.path.isfile(os.path.join(out_dir, str(jid), "a.protocol.md"))


def test_process_analysis_unloads_whisper_before_llm_and_ollama_after(tmp_path):
    """Whisper обязан уйти из VRAM до LLM, Ollama — освободить карту после."""
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    jid, out_dir = _job_with_transcript(conn, tmp_path)
    analyses.enqueue(conn, jid, "protocol", "Протокол", "тело", "m")
    row = analyses.claim_next(conn)
    engine, provider = FakeEngine(), FakeProvider(["# Протокол"])
    worker.process_analysis(conn, ProgressBroker(), engine, provider, row,
                            settings_global=Settings(), output_dir=out_dir)
    assert engine.unloaded == 1
    assert provider.unloaded == 1


def test_process_analysis_passes_vocabulary_as_glossary(tmp_path):
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    jid, out_dir = _job_with_transcript(conn, tmp_path)
    analyses.enqueue(conn, jid, "protocol", "Протокол", "тело", "m")
    row = analyses.claim_next(conn)
    provider = FakeProvider(["# Протокол"])
    worker.process_analysis(conn, ProgressBroker(), FakeEngine(), provider, row,
                            settings_global=Settings(vocabulary="АккордПост"),
                            output_dir=out_dir)
    assert "АккордПост" in provider.calls[0][0]


def test_process_analysis_marks_error_when_transcript_missing(tmp_path):
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    job_queue.update(conn, jid, status="done", output_dir=str(tmp_path / "nope"))
    aid = analyses.enqueue(conn, jid, "protocol", "Протокол", "тело", "m")
    row = analyses.claim_next(conn)
    worker.process_analysis(conn, ProgressBroker(), FakeEngine(), FakeProvider(), row,
                            settings_global=Settings(), output_dir=str(tmp_path / "out"))
    err = analyses.get(conn, aid)
    assert err["status"] == "error"
    assert "транскрипт" in err["error"]


def test_process_analysis_marks_error_and_writes_nothing_on_llm_failure(tmp_path):
    """Отказ модели не оставляет на диске правдоподобного огрызка."""
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    jid, out_dir = _job_with_transcript(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "protocol", "Протокол", "тело", "m")
    row = analyses.claim_next(conn)
    provider = FakeProvider([RuntimeError("демон упал")] * 3)
    worker.process_analysis(conn, ProgressBroker(), FakeEngine(), provider, row,
                            settings_global=Settings(), output_dir=out_dir)
    assert analyses.get(conn, aid)["status"] == "error"
    assert not os.path.isfile(os.path.join(out_dir, str(jid), "a.protocol.md"))


def test_analyses_are_claimed_only_when_jobs_queue_is_empty(tmp_path):
    """Транскрибация в приоритете: GPU один, и ждать расшифровки хуже, чем анализа.
    Порядок в run_forever нитями не проверить — фиксируем само решение."""
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    jid = job_queue.enqueue(conn, "C:/v/a.mp4", "{}")
    analyses.enqueue(conn, jid, "protocol", "Протокол", "тело", "m")
    assert job_queue.claim_next(conn) is not None   # сначала job
    assert job_queue.claim_next(conn) is None       # очередь jobs пуста
    assert analyses.claim_next(conn) is not None    # и только теперь анализ


def test_process_analysis_publishes_events_with_analysis_id(tmp_path):
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    jid, out_dir = _job_with_transcript(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "protocol", "Протокол", "тело", "m")
    row = analyses.claim_next(conn)
    broker = ProgressBroker(); q = broker.subscribe()
    worker.process_analysis(conn, broker, FakeEngine(), FakeProvider(["# П"]), row,
                            settings_global=Settings(), output_dir=out_dir)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert events and all(e["analysis_id"] == aid for e in events)
    assert events[-1]["status"] == "done"
