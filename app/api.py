from __future__ import annotations

import io
import json
import os
import queue
import zipfile

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config, job_queue
from app.models import Settings

WEB_DIR = config.BASE_DIR / "web"


class JobIn(BaseModel):
    path: str
    settings: dict = {}


class SettingsIn(BaseModel):
    settings: dict
    formats: list[str]


def create_app(conn, broker, settings_state) -> FastAPI:
    app = FastAPI(title="Meeting Transcriber")

    @app.post("/api/jobs")
    def create_job(body: JobIn):
        if not os.path.isfile(body.path):
            raise HTTPException(status_code=400, detail="Файл не найден по указанному пути")
        jid = job_queue.enqueue(conn, body.path, json.dumps(body.settings))
        return {"id": jid}

    @app.get("/api/jobs")
    def list_jobs():
        return [dict(r) for r in job_queue.list_jobs(conn)]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        row = job_queue.get(conn, job_id)
        if row is None:
            raise HTTPException(404, "job не найден")
        return dict(row)

    @app.get("/api/settings")
    def get_settings():
        return {"settings": settings_state.get_global().to_dict(),
                "formats": settings_state.get_formats()}

    @app.put("/api/settings")
    def put_settings(body: SettingsIn):
        settings_state.set_global(Settings.from_dict(body.settings))
        settings_state.set_formats(body.formats)
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/download/{fmt}")
    def download(job_id: int, fmt: str):
        row = job_queue.get(conn, job_id)
        if row is None or not row["output_dir"]:
            raise HTTPException(404, "результат недоступен")
        base = os.path.splitext(row["filename"])[0]
        path = os.path.join(row["output_dir"], f"{base}.{fmt}")
        if not os.path.isfile(path):
            raise HTTPException(404, "формат недоступен")
        return FileResponse(path, filename=os.path.basename(path))

    @app.get("/api/jobs/{job_id}/download_zip")
    def download_zip(job_id: int):
        row = job_queue.get(conn, job_id)
        if row is None or not row["output_dir"] or not os.path.isdir(row["output_dir"]):
            raise HTTPException(404, "результат недоступен")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in os.listdir(row["output_dir"]):
                zf.write(os.path.join(row["output_dir"], name), name)
        buf.seek(0)
        return StreamingResponse(
            buf, media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="job_{job_id}.zip"'})

    @app.get("/api/events")
    def events():
        q = broker.subscribe()

        def gen():
            try:
                while True:
                    try:
                        evt = q.get(timeout=15)
                        yield f"data: {json.dumps(evt)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                broker.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    return app
