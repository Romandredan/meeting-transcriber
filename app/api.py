from __future__ import annotations

import io
import json
import os
import queue
import re
import shutil
import sqlite3
import zipfile
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import analyses, config, job_queue, llm, speakers, templates_store, transcripts
from app.models import Settings, TranscriptResult
from app.stitch import merge_speaker_runs
from app.watcher import is_media

WEB_DIR = config.BASE_DIR / "web"

# Метка идёт в имя файла результата на диске (f"{basename}.{label}.md") — поэтому
# только латиница, цифры, дефис и подчёркивание, без пробелов и разделителей пути.
_LABEL_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")


def _validate_label(label: str) -> None:
    if not _LABEL_RE.match(label):
        raise HTTPException(
            400,
            f"метка «{label}» недопустима: разрешены только латинские буквы, цифры, "
            f"дефис и подчёркивание, от 1 до 32 символов — метка используется в имени "
            f"файла результата на диске")


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


class SpeakerAliasIn(BaseModel):
    label: str
    name: str = ""
    merged_into: str | None = None


class SpeakersIn(BaseModel):
    aliases: list[SpeakerAliasIn]


def _load_result(row) -> TranscriptResult:
    """Сырой TranscriptResult из {basename}.json в output/<id>/.

    HTTPException с русским текстом — показывается пользователю как есть."""
    if not row["output_dir"]:
        raise HTTPException(404, "расшифровка недоступна")
    base = os.path.splitext(row["filename"])[0]
    path = os.path.join(row["output_dir"], f"{base}.json")
    if not os.path.isfile(path):
        raise HTTPException(404, "нет файла транскрипта — формат JSON был выключен")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return TranscriptResult.from_dict(data)


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

    @app.post("/api/jobs/upload")
    async def upload_job(request: Request, filename: str):
        """Приём файла из веб-интерфейса (drag&drop / «Выбрать на диске»).

        Браузер по соображениям безопасности не отдаёт полный путь к файлу,
        поэтому содержимое передаётся телом запроса. Сохраняем в inbox — оттуда
        файл живёт обычным циклом (после расшифровки уедет в processed).
        Имя идёт query-параметром: так тело читается потоком и не нужен
        python-multipart (новые зависимости не добавляем).
        """
        # Только имя файла: из браузера может прийти «путь» вида C:\fakepath\x.mp4.
        name = re.split(r"[\\/]", filename)[-1].strip()
        if not name:
            raise HTTPException(400, "Не передано имя файла")
        if not is_media(name):
            raise HTTPException(400, f"«{name}» — не медиафайл, такое расшифровать нельзя")
        inbox = config.INBOX_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        # Пишем под немедиа-суффиксом и переименовываем в конце: watcher реагирует
        # только на медиа-расширения, и недописанный файл мимо него пройдёт.
        tmp_target = inbox / (name + ".uploading")
        size = 0
        with open(tmp_target, "wb") as fh:
            async for chunk in request.stream():
                fh.write(chunk)
                size += len(chunk)
        if size == 0:
            tmp_target.unlink(missing_ok=True)
            raise HTTPException(400, f"«{name}»: получен пустой файл")
        # Не перезаписываем лежащее в inbox — суффикс, как в move_to_processed.
        target = inbox / name
        if target.exists():
            base, ext = os.path.splitext(name)
            i = 1
            while (inbox / f"{base}.{i}{ext}").exists():
                i += 1
            target = inbox / f"{base}.{i}{ext}"
        os.replace(tmp_target, target)
        # Дедуп с watcher'ом: он увидит переименование и проверит has_active, но
        # проверяем и сами — при INBOX_QUIET_SECONDS=0 гонка реальна.
        if not job_queue.has_active(conn, str(target)):
            jid = job_queue.enqueue(conn, str(target), "{}")
        else:
            row = conn.execute("SELECT id FROM jobs WHERE source_path=?", (str(target),)).fetchone()
            jid = int(row["id"]) if row else None
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
        # diarize_available — есть ли HF_TOKEN: без него диаризация молча
        # пропускается, и фронт должен предупредить об этом явно, а не дарить
        # пользователю транскрипт «без разделения» без объяснений.
        return {"settings": settings_state.get_global().to_dict(),
                "formats": settings_state.get_formats(),
                "diarize_available": bool(config.HF_TOKEN)}

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

    # ── управление очередью ──────────────────────────────────────────────────

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: int):
        """Снимает встречу с очереди или просит воркер остановиться.

        Работа в очереди отменяется мгновенно (её ещё никто не взял). Работа в
        процессе — кооперативно: воркер увидит флаг на ближайшей стадии, поэтому
        UI показывает «отменяем», а не «отменено»."""
        row = job_queue.get(conn, job_id)
        if row is None:
            raise HTTPException(404, "встреча не найдена")
        if row["status"] == "queued":
            job_queue.update(conn, job_id, status="cancelled", stage="", progress=0.0, error="")
            broker.publish(job_id, "", 0.0, "cancelled")
            return {"ok": True, "status": "cancelled"}
        if row["status"] == "processing":
            job_queue.request_cancel(job_id)
            return {"ok": True, "status": "cancelling"}
        raise HTTPException(400, "отменять можно только встречу в очереди или в работе")

    @app.post("/api/jobs/{job_id}/requeue")
    def requeue_job(job_id: int):
        """Повтор после ошибки или возврат отменённой встречи в очередь."""
        row = job_queue.get(conn, job_id)
        if row is None:
            raise HTTPException(404, "встреча не найдена")
        if row["status"] == "processing":
            raise HTTPException(400, "встреча уже расшифровывается")
        if not os.path.isfile(row["source_path"]):
            raise HTTPException(
                400, f"исходный файл больше не доступен: {row['source_path']} — "
                     f"он мог быть перемещён в processed/ после успешной расшифровки")
        job_queue.clear_cancel(job_id)
        job_queue.update(conn, job_id, status="queued", stage="", progress=0.0, error="")
        broker.publish(job_id, "", 0.0, "queued")
        return {"ok": True}

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: int, purge: bool = False):
        """Убирает встречу из списка. purge=true — вместе с папкой результатов."""
        row = job_queue.get(conn, job_id)
        if row is None:
            raise HTTPException(404, "встреча не найдена")
        if row["status"] == "processing":
            raise HTTPException(400, "сначала отмените расшифровку")
        if purge and row["output_dir"] and os.path.isdir(row["output_dir"]):
            shutil.rmtree(row["output_dir"], ignore_errors=True)
        job_queue.delete(conn, job_id)
        return {"ok": True}

    # ── данные для раскрытой карточки ────────────────────────────────────────

    @app.get("/api/jobs/{job_id}/transcript")
    def job_transcript(job_id: int, meta: bool = False, limit: int = 3000):
        """Расшифровка для панели «Транскрипция» — читается с диска, из того же
        {basename}.json, что потом уходит в анализ.

        Спикеры отдаются с применёнными алиасами (слияние → имя): панель видит
        то же, что попадёт в анализ и перегенерированные файлы. Сырые метки
        остаются только в JSON на диске.

        meta=true — только сводка (язык, длительность, модель, спикеры, число
        реплик): список /api/jobs её не несёт, а строке очереди она нужна."""
        row = job_queue.get(conn, job_id)
        if row is None:
            raise HTTPException(404, "встреча не найдена")
        # Транскрипт — из БД (источник истины); файл на диске — fallback для
        # встреч, расшифрованных до появления таблицы transcripts.
        result = transcripts.get(conn, job_id) or _load_result(row)
        result = speakers.apply_view(result, speakers.get_aliases(conn, job_id))
        # Панель показывает те же склеенные реплики, что TXT/MD/DOCX и вход
        # анализа (merge_speaker_runs с MERGE_MAX_SECONDS): сырые секундные
        # сегменты Whisper читать мучительно, а второй формат текста заводить
        # не надо — иначе представления со временем разъедутся.
        segments = (merge_speaker_runs(result.segments, max_seconds=config.MERGE_MAX_SECONDS)
                    if result.diarized else result.segments)
        # Итоговый список спикеров — в порядке первого появления в записи
        # (после слияний их может стать меньше, чем нашла диаризация).
        seen: dict[str, None] = {}
        for s in segments:
            if s.speaker:
                seen.setdefault(s.speaker)
        out = {
            "language": result.language,
            "duration": result.duration,
            "model": result.model,
            "diarized": result.diarized,
            "speakers": list(seen),
            "count": len(segments),
        }
        if meta:
            return out
        # Слова не отдаём: панели нужны только реплики, а words раздувают ответ
        # часовой встречи в десятки мегабайт.
        out["segments"] = [
            {"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker}
            for s in segments[:limit]
        ]
        out["truncated"] = len(segments) > limit
        return out

    @app.get("/api/jobs/{job_id}/speakers")
    def get_speakers(job_id: int):
        """Строки панели «Указать имена»: сырые метки + статистика для
        идентификации (число реплик, время речи, превью) + текущие алиасы."""
        row = job_queue.get(conn, job_id)
        if row is None:
            raise HTTPException(404, "встреча не найдена")
        result = transcripts.get(conn, job_id) or _load_result(row)
        if not result.diarized:
            return {"speakers": []}
        aliases = speakers.get_aliases(conn, job_id)
        rows = []
        for st in speakers.speaker_stats(result):
            info = aliases.get(st["label"]) or {}
            rows.append({**st, "seconds": round(st["seconds"]),
                         "name": info.get("name") or "",
                         "merged_into": info.get("merged_into")})
        return {"speakers": rows}

    @app.put("/api/jobs/{job_id}/speakers")
    def put_speakers(job_id: int, body: SpeakersIn):
        """Сохраняет имена/объединения спикеров и перегенерирует читаемые файлы.

        Алиасы — истина в БД; перегенерация TXT/SRT/VTT/MD/DOCX — best-effort
        (сбой записи файла не отменяет сохранение)."""
        row = job_queue.get(conn, job_id)
        if row is None:
            raise HTTPException(404, "встреча не найдена")
        result = transcripts.get(conn, job_id) or _load_result(row)
        known = speakers.raw_speakers(result)
        aliases = {a.label: {"name": a.name, "merged_into": a.merged_into}
                   for a in body.aliases}
        error = speakers.validate_aliases(aliases, known)
        if error:
            raise HTTPException(400, f"Имена спикеров не сохранены: {error}")
        speakers.set_aliases(conn, job_id, aliases)
        # Поисковый индекс хранит отображаемые метки — переиндексируем,
        # чтобы поиск по имени находил реплики этого человека.
        transcripts.index_replicas(conn, job_id, result)
        base = os.path.splitext(row["filename"])[0]
        speakers.rerender_outputs(result, row["output_dir"], base, aliases)
        return {"ok": True}

    @app.get("/api/search")
    def global_search(q: str = "", limit: int = 50, all: bool = False):
        """Полнотекстовый поиск по репликам всех встреч и результатам анализов
        (FTS5, при недоступности — LIKE-перебор).

        Лимиты по видам: реплики — limit, анализы — 20 (all=true снимает оба
        до разумного потолка). total — полные счётчики совпадений, чтобы UI
        показывал «показано X из Y»."""
        hits, totals = transcripts.search(
            conn, q, limit=500 if all else limit,
            analysis_limit=200 if all else 20)
        return {"hits": hits, "total": totals}

    @app.get("/api/jobs/{job_id}/files")
    def job_files(job_id: int):
        """Какие форматы реально лежат на диске — чтобы UI не рисовал ссылки на
        файлы, которых нет (форматы можно выключать в настройках)."""
        row = job_queue.get(conn, job_id)
        if row is None:
            raise HTTPException(404, "встреча не найдена")
        out_dir = row["output_dir"]
        if not out_dir or not os.path.isdir(out_dir):
            return {"formats": []}
        base = os.path.splitext(row["filename"])[0]
        formats = [fmt for fmt in ("txt", "srt", "vtt", "json", "md", "docx")
                   if os.path.isfile(os.path.join(out_dir, f"{base}.{fmt}"))]
        return {"formats": formats}

    # Флаг читаем в момент вызова create_app: при ANALYZE_ENABLED=false роутов
    # анализа не существует вовсе, и установленная Ollama не требуется.
    if config.ANALYZE_ENABLED:

        @app.get("/api/templates")
        def list_templates():
            return [dict(r) for r in templates_store.list_templates(conn)]

        @app.post("/api/templates")
        def create_template(body: TemplateIn):
            _validate_label(body.label)
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
            _validate_label(body.label)
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
            if analyses.has_active(conn, job_id, tpl["label"]):
                raise HTTPException(
                    400, f"анализ этой встречи по шаблону «{tpl['label']}» уже в очереди")
            job_out = job["output_dir"] or ""
            basename = os.path.splitext(job["filename"])[0]
            # Транскрипт может жить только в БД (файл почистили) — это не
            # повод отказывать: источник истины — transcripts, диск лишь копия.
            if (transcripts.get(conn, job_id) is None
                    and not os.path.isfile(os.path.join(job_out, f"{basename}.json"))):
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
            transcripts.delete_analysis_rows(conn, analysis_id)
            return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    return app
