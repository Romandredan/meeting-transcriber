import json
import os

from app import analyses, config, db, job_queue, worker, writers
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


def test_process_analysis_keeps_result_when_md_write_fails(tmp_path, monkeypatch):
    """Сбой записи .md на диск — не повод терять результат: источник истины БД."""
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    jid, out_dir = _job_with_transcript(conn, tmp_path)
    aid = analyses.enqueue(conn, jid, "protocol", "Протокол", "тело шаблона", "qwen3:14b")
    row = analyses.claim_next(conn)
    md_path = os.path.join(out_dir, str(jid), "a.protocol.md")
    real_open = open

    def fake_open(file, *a, **kw):
        if os.fspath(file) == md_path:
            raise OSError("диск полон")
        return real_open(file, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    engine, provider = FakeEngine(), FakeProvider(["# Протокол\nтекст"])
    worker.process_analysis(conn, ProgressBroker(), engine, provider, row,
                            settings_global=Settings(), output_dir=out_dir)
    done = analyses.get(conn, aid)
    assert done["status"] == "done"
    assert done["progress"] == 1.0
    assert done["result_md"] == "# Протокол\nтекст"
    assert not os.path.isfile(md_path)  # запись не удалась — и это ожидаемо


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
    # Класс исключения — деталь реализации (все проверки process_analysis бросают
    # RuntimeError), пользователю она не нужна и не должна утекать в текст.
    assert not err["error"].startswith("RuntimeError")


def test_process_analysis_marks_error_with_russian_text_when_job_missing(tmp_path):
    """job_queue.get вернул None (встреча удалена/не существует) — текст по-русски,
    без технического префикса RuntimeError."""
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    aid = analyses.enqueue(conn, 999999, "protocol", "Протокол", "тело", "m")
    row = analyses.claim_next(conn)
    worker.process_analysis(conn, ProgressBroker(), FakeEngine(), FakeProvider(), row,
                            settings_global=Settings(), output_dir=str(tmp_path / "out"))
    err = analyses.get(conn, aid)
    assert err["status"] == "error"
    assert "встреча не найдена" in err["error"]
    assert not err["error"].startswith("RuntimeError")


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


def test_process_analysis_marks_error_with_russian_text_on_corrupted_json(tmp_path):
    """json.JSONDecodeError.__str__ — английский текст; пользователю должен
    достаться русский, без утечки исходного сообщения библиотеки."""
    conn = db.connect(tmp_path / "t.db"); db.init_schema(conn)
    jid, out_dir = _job_with_transcript(conn, tmp_path)
    json_path = os.path.join(out_dir, str(jid), "a.json")
    with open(json_path, "w", encoding="utf-8") as f:
        f.write("это не json{{{")
    aid = analyses.enqueue(conn, jid, "protocol", "Протокол", "тело", "m")
    row = analyses.claim_next(conn)
    worker.process_analysis(conn, ProgressBroker(), FakeEngine(), FakeProvider(), row,
                            settings_global=Settings(), output_dir=out_dir)
    err = analyses.get(conn, aid)
    assert err["status"] == "error"
    assert "повреждён" in err["error"]
    assert "заново" in err["error"]
    assert "Expecting" not in err["error"]  # не утекает англ. текст json.JSONDecodeError
    assert not err["error"].startswith("RuntimeError")


def _make_worker(tmp_path, engine, provider=None):
    return worker.Worker(None, ProgressBroker(), engine, lambda: Settings(),
                         lambda: ["txt"], str(tmp_path / "tmp"), str(tmp_path / "out"),
                         provider=provider)


def test_idle_tick_does_nothing_before_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IDLE_UNLOAD_SECONDS", 300)
    engine, provider = FakeEngine(), FakeProvider()
    w = _make_worker(tmp_path, engine, provider)
    assert w.idle_tick(w._last_active + 100) is False
    assert engine.unloaded == 0
    assert provider.unloaded == 0


def test_idle_tick_unloads_engine_and_provider_after_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IDLE_UNLOAD_SECONDS", 300)
    engine, provider = FakeEngine(), FakeProvider()
    w = _make_worker(tmp_path, engine, provider)
    assert w.idle_tick(w._last_active + 300) is True
    assert engine.unloaded == 1
    assert provider.unloaded == 1


def test_idle_tick_does_not_unload_twice_in_same_idle_period(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IDLE_UNLOAD_SECONDS", 300)
    engine, provider = FakeEngine(), FakeProvider()
    w = _make_worker(tmp_path, engine, provider)
    assert w.idle_tick(w._last_active + 300) is True
    assert w.idle_tick(w._last_active + 400) is False
    assert engine.unloaded == 1
    assert provider.unloaded == 1


def test_idle_tick_disabled_when_setting_is_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IDLE_UNLOAD_SECONDS", 0)
    engine, provider = FakeEngine(), FakeProvider()
    w = _make_worker(tmp_path, engine, provider)
    assert w.idle_tick(w._last_active + 100000) is False
    assert engine.unloaded == 0
    assert provider.unloaded == 0


def test_idle_tick_without_provider_unloads_engine_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "IDLE_UNLOAD_SECONDS", 300)
    engine = FakeEngine()
    w = _make_worker(tmp_path, engine, provider=None)
    assert w.idle_tick(w._last_active + 300) is True
    assert engine.unloaded == 1
