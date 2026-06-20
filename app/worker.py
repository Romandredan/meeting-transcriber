from __future__ import annotations

import json
import os
import threading
import time

from app import ffmpeg_tool, job_queue, writers
from app.models import Settings


def merge_settings(global_s: Settings, override: dict) -> Settings:
    base = global_s.to_dict()
    base.update({k: v for k, v in (override or {}).items() if v is not None})
    return Settings.from_dict(base)


def process_job(conn, broker, engine, job_row, *, settings_global: Settings,
                formats: list[str], tmp_dir: str, output_dir: str) -> None:
    job_id = job_row["id"]
    override = json.loads(job_row["settings_json"] or "{}")
    settings = merge_settings(settings_global, override)
    os.makedirs(tmp_dir, exist_ok=True)
    job_out = os.path.join(output_dir, str(job_id))

    def report(stage: str, progress: float) -> None:
        job_queue.update(conn, job_id, stage=stage, progress=progress)
        broker.publish(job_id, stage, progress, "processing")

    try:
        report("audio", 0.02)
        wav = os.path.join(tmp_dir, f"{job_id}.wav")
        ffmpeg_tool.extract_audio(job_row["source_path"], wav)

        result = engine.transcribe(wav, settings, report)

        report("write", 0.95)
        basename = os.path.splitext(job_row["filename"])[0]
        writers.write_all(result, job_out, formats, basename)

        job_queue.update(conn, job_id, status="done", progress=1.0,
                         stage="write", output_dir=job_out)
        broker.publish(job_id, "write", 1.0, "done")
    except Exception as e:
        job_queue.update(conn, job_id, status="error", error=f"{type(e).__name__}: {e}")
        broker.publish(job_id, "", 0.0, "error")
    finally:
        try:
            if os.path.exists(wav):
                os.remove(wav)
        except Exception:
            pass


class Worker:
    def __init__(self, conn, broker, engine, settings_provider, formats_provider,
                 tmp_dir: str, output_dir: str) -> None:
        self.conn = conn
        self.broker = broker
        self.engine = engine
        self.settings_provider = settings_provider
        self.formats_provider = formats_provider
        self.tmp_dir = tmp_dir
        self.output_dir = output_dir
        self._thread: threading.Thread | None = None

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            row = job_queue.claim_next(self.conn)
            if row is None:
                time.sleep(1.0)
                continue
            process_job(self.conn, self.broker, self.engine, row,
                        settings_global=self.settings_provider(),
                        formats=self.formats_provider(),
                        tmp_dir=self.tmp_dir, output_dir=self.output_dir)

    def start(self, stop_event: threading.Event) -> None:
        self._thread = threading.Thread(target=self.run_forever, args=(stop_event,), daemon=True)
        self._thread.start()
