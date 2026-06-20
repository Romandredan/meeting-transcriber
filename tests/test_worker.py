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
