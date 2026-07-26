from __future__ import annotations

import io
import json
import os
import queue
import sqlite3
import zipfile
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import analyses, config, job_queue, llm, templates_store
from app.models import Settings

WEB_DIR = config.BASE_DIR / "web"


class JobIn(BaseModel):
    path: str
    settings: dict = {}


class SettingsIn(BaseModel):
    settings: dict
    formats: list[str]


class TemplateIn(BaseModel):
    label: str
    display_name: str
    description: str = ""
    prompt_body: str
    enabled: bool = True


class AnalysisIn(BaseModel):
    label: str


def create_app(conn, broker, settings_state) -> FastAPI:
    app = FastAPI(title="Meeting Transcriber")

    @app.post("/api/jobs")
    def create_job(body: JobIn):
        # Нормализуем путь: убираем обрамляющие кавычки (Проводник «Копировать как путь»)
        # и лишние пробелы/переводы строк.
        path = body.path.strip().strip('"').strip("'").strip()
        if not os.path.isfile(path):
            raise HTTPException(status_code=400,
                                detail=f"Файл не найден: {path} — укажите полный путь к существующему файлу")
        jid = job_queue.enqueue(conn, path, json.dumps(body.settings))
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

    @app.get("/api/llm/health")
    def llm_health():
        # Роут есть всегда: фронт по нему решает, показывать ли блок анализов.
        if not config.ANALYZE_ENABLED:
            return {"enabled": False}
        return {"enabled": True, **llm.make_provider().health()}

    # Флаг читаем в момент вызова create_app: при ANALYZE_ENABLED=false роутов
    # анализа не существует вовсе, и установленная Ollama не требуется.
    if config.ANALYZE_ENABLED:

        @app.get("/api/templates")
        def list_templates():
            return [dict(r) for r in templates_store.list_templates(conn)]

        @app.post("/api/templates")
        def create_template(body: TemplateIn):
            try:
                tid = templates_store.create(conn, body.label, body.display_name,
                                             body.description, body.prompt_body,
                                             body.enabled)
            except sqlite3.IntegrityError:
                raise HTTPException(400, f"метка «{body.label}» уже занята")
            return {"id": tid}

        @app.put("/api/templates/{template_id}")
        def update_template(template_id: int, body: TemplateIn):
            if templates_store.get(conn, template_id) is None:
                raise HTTPException(404, "шаблон не найден")
            try:
                templates_store.update(conn, template_id, label=body.label,
                                       display_name=body.display_name,
                                       description=body.description,
                                       prompt_body=body.prompt_body,
                                       enabled=body.enabled)
            except sqlite3.IntegrityError:
                raise HTTPException(400, f"метка «{body.label}» уже занята")
            return {"ok": True}

        @app.delete("/api/templates/{template_id}")
        def delete_template(template_id: int):
            templates_store.delete(conn, template_id)
            return {"ok": True}

        @app.post("/api/jobs/{job_id}/analyses")
        def create_analysis(job_id: int, body: AnalysisIn):
            job = job_queue.get(conn, job_id)
            if job is None:
                raise HTTPException(404, "встреча не найдена")
            if job["status"] != "done":
                raise HTTPException(400, "встреча ещё не расшифрована")
            tpl = templates_store.get_by_label(conn, body.label)
            if tpl is None or not tpl["enabled"]:
                raise HTTPException(400, f"шаблон «{body.label}» недоступен")
            job_out = job["output_dir"] or ""
            basename = os.path.splitext(job["filename"])[0]
            if not os.path.isfile(os.path.join(job_out, f"{basename}.json")):
                raise HTTPException(400, "нет файла транскрипта — расшифруйте встречу заново")
            aid = analyses.enqueue(conn, job_id, tpl["label"], tpl["display_name"],
                                   tpl["prompt_body"], config.LLM_MODEL)
            return {"id": aid}

        @app.get("/api/jobs/{job_id}/analyses")
        def list_analyses(job_id: int):
            return [dict(r) for r in analyses.list_for_job(conn, job_id)]

        @app.get("/api/analyses/{analysis_id}/download")
        def download_analysis(analysis_id: int):
            row = analyses.get(conn, analysis_id)
            if row is None or row["status"] != "done" or not row["result_md"]:
                raise HTTPException(404, "результат недоступен")
            # Из БД, а не с диска: на диске лежит только последняя версия по метке,
            # а качать можно и старую.
            job = job_queue.get(conn, row["job_id"])
            base = os.path.splitext(job["filename"])[0] if job else f"analysis_{analysis_id}"
            name = f"{base}.{row['label']}.md"
            return Response(
                content=row["result_md"], media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition":
                         f'attachment; filename="analysis_{analysis_id}.md"; '
                         f"filename*=UTF-8''{quote(name)}"})

        @app.delete("/api/analyses/{analysis_id}")
        def delete_analysis(analysis_id: int):
            analyses.delete(conn, analysis_id)
            return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    return app
