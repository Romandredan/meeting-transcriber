import json
from app import db, job_queue, worker
from app.models import Settings, TranscriptResult, Segment, Word
from app.progress import ProgressBroker


class FakeEngine:
    def transcribe(self, audio_path, settings, progress):
        progress("transcribe", 0.5)
        w = Word(0.0, 1.0, "тест", speaker="SPEAKER_00")
        s = Segment(0.0, 1.0, "тест", speaker="SPEAKER_00", words=[w])
        return TranscriptResult("ru", 1.0, settings.model, True, [s])


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
