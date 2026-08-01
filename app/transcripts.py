"""Хранение транскриптов в БД и полнотекстовый поиск (FTS5).

Инвариант «БД — источник истины»: worker пишет транскрипт сюда первым делом,
файлы в output/ — best-effort копии. Сегменты хранятся без words (детализация
по словам нужна только JSON-файлу на диске).

Поиск — FTS5-таблица search_fts (миграция 2) двух видов строк: реплики
(kind='replica') и результаты анализов (kind='analysis'). Если FTS5 в сборке
SQLite недоступен — молчаливая деградация на LIKE-перебор, интерфейс тот же.

Спикеры в индексе — отображаемые (через speakers.apply_view): поиск по имени
находит реплики этого человека. Поэтому PUT speakers обязан переиндексировать
реплики встречи (index_replicas).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3

from app.models import TranscriptResult
from app import speakers as spk
from app.stitch import humanize_speaker

log = logging.getLogger(__name__)

SNIP_OPEN, SNIP_CLOSE = "[", "]"   # маркеры совпадения в сниппете (UI → <mark>)


# ── хранилище ────────────────────────────────────────────────────────────────

def save(conn: sqlite3.Connection, job_id: int, result: TranscriptResult) -> None:
    """Сохраняет транскрипт в БД и индексирует реплики. Вызывается worker'ом
    ДО записи файлов: БД — истина, диск — копия."""
    segments = [{"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker}
                for s in result.segments]
    conn.execute(
        "INSERT OR REPLACE INTO transcripts "
        "(job_id, language, duration, model, diarized, segments_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, result.language, result.duration, result.model,
         int(result.diarized), json.dumps(segments, ensure_ascii=False)))
    index_replicas(conn, job_id, result)
    conn.commit()


def get(conn: sqlite3.Connection, job_id: int) -> TranscriptResult | None:
    row = conn.execute("SELECT * FROM transcripts WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        return None
    data = {"language": row["language"], "duration": row["duration"],
            "model": row["model"], "diarized": bool(row["diarized"]),
            "segments": json.loads(row["segments_json"])}
    return TranscriptResult.from_dict(data)


def update_segment_span(conn: sqlite3.Connection, job_id: int, start: float,
                        end: float, text: str) -> bool:
    """Ручная правка реплики (план 6b): заменяет прогон сырых сегментов,
    покрывающий абзац [start, end], одним сегментом с отредактированным
    текстом (спикер и границы — от прогона).

    Редактируем мы АБЗАЦ, а храним сырые сегменты: склейка — представление.
    Свёртка прогона в один сегмент — осознанная цена: субтитры в этом месте
    станут одним куском, зато текст везде (БД, файлы, анализ, FTS) одинаковый.
    Возвращает False, если транскрипта в БД нет."""
    row = conn.execute("SELECT segments_json FROM transcripts WHERE job_id=?",
                       (job_id,)).fetchone()
    if row is None:
        return False
    segs = json.loads(row["segments_json"])
    span = [i for i, s in enumerate(segs)
            if s.get("start", 0.0) >= start - 0.5 and s.get("start", 0.0) < end - 0.5]
    if not span:
        raise IndexError(f"нет реплики с границами {start}–{end}")
    merged = {"start": segs[span[0]]["start"], "end": segs[span[-1]]["end"],
              "text": text, "speaker": segs[span[0]].get("speaker")}
    segs[span[0]:span[-1] + 1] = [merged]
    conn.execute("UPDATE transcripts SET segments_json=? WHERE job_id=?",
                 (json.dumps(segs, ensure_ascii=False), job_id))
    conn.commit()
    return True


def purge_job(conn: sqlite3.Connection, job_id: int) -> None:
    """Удаление встречи: транскрипт и все строки индекса (без commit — вызывается
    из job_queue.delete в его транзакции)."""
    conn.execute("DELETE FROM transcripts WHERE job_id=?", (job_id,))
    if _fts_ok(conn):
        conn.execute("DELETE FROM search_fts WHERE job_id=?", (job_id,))


def delete_analysis_rows(conn: sqlite3.Connection, analysis_id: int) -> None:
    if _fts_ok(conn):
        conn.execute("DELETE FROM search_fts WHERE kind='analysis' AND ref_id=?",
                     (analysis_id,))
        conn.commit()


# ── индексация ───────────────────────────────────────────────────────────────

def _fts_ok(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_fts'"
    ).fetchone()
    return row is not None


def index_replicas(conn: sqlite3.Connection, job_id: int,
                   result: TranscriptResult) -> None:
    """Переиндексирует реплики встречи (при сохранении и после PUT speakers).

    В индекс идут отображаемые метки (apply_view: слияние → имя), чтобы поиск
    по имени находил реплики человека."""
    if not _fts_ok(conn):
        return
    view = spk.apply_view(result, spk.get_aliases(conn, job_id))
    conn.execute("DELETE FROM search_fts WHERE kind='replica' AND job_id=?", (job_id,))
    for i, s in enumerate(view.segments):
        conn.execute(
            "INSERT INTO search_fts (kind, job_id, ref_id, start, speaker, text) "
            "VALUES ('replica', ?, ?, ?, ?, ?)",
            (job_id, i, s.start, s.speaker or "", s.text))
    conn.commit()


def index_analysis(conn: sqlite3.Connection, analysis_id: int, job_id: int,
                   result_md: str) -> None:
    if not _fts_ok(conn):
        return
    conn.execute("DELETE FROM search_fts WHERE kind='analysis' AND ref_id=?",
                 (analysis_id,))
    conn.execute(
        "INSERT INTO search_fts (kind, job_id, ref_id, start, speaker, text) "
        "VALUES ('analysis', ?, ?, 0, '', ?)", (job_id, analysis_id, result_md))
    conn.commit()


def backfill(conn: sqlite3.Connection) -> tuple[int, int]:
    """Заполняет transcripts для старых встреч из JSON-файлов на диске.

    Best-effort по встрече: битый/отсутствующий файл пропускаем, остальным
    это не мешает. Возвращает (заполнено, пропущено)."""
    rows = conn.execute(
        "SELECT j.id, j.filename, j.output_dir FROM jobs j "
        "WHERE j.status='done' AND j.output_dir IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM transcripts t WHERE t.job_id=j.id)"
    ).fetchall()
    filled = skipped = 0
    for row in rows:
        base = os.path.splitext(row["filename"])[0]
        path = os.path.join(row["output_dir"], f"{base}.json")
        try:
            with open(path, encoding="utf-8") as f:
                result = TranscriptResult.from_dict(json.load(f))
            save(conn, row["id"], result)
            filled += 1
        except Exception as e:
            log.warning("Бэкфилл транскрипта для job %s пропущен (%s): %s",
                        row["id"], path, e)
            skipped += 1
    # Индексируем готовые анализы.
    if _fts_ok(conn):
        for a in conn.execute(
                "SELECT id, job_id, result_md FROM analyses "
                "WHERE status='done' AND result_md IS NOT NULL").fetchall():
            index_analysis(conn, a["id"], a["job_id"], a["result_md"])
    if filled or skipped:
        log.info("Бэкфилл транскриптов: заполнено %d, пропущено %d", filled, skipped)
    return filled, skipped


# ── поиск ────────────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _snippet_around(text: str, query: str, width: int = 120) -> str:
    """Сниппет для LIKE-режима: фрагмент вокруг первого вхождения с маркерами."""
    pos = text.lower().find(query.lower())
    if pos < 0:
        return text[:width]
    start = max(0, pos - width // 3)
    frag = text[start:start + width]
    rel = pos - start
    frag = frag[:rel] + SNIP_OPEN + frag[rel:rel + len(query)] + SNIP_CLOSE + frag[rel + len(query):]
    return ("…" if start > 0 else "") + frag + ("…" if start + width < len(text) else "")


def search(conn: sqlite3.Connection, query: str, limit: int = 50,
           analysis_limit: int = 20) -> tuple[list[dict], dict]:
    """Полнотекстовый поиск по репликам и анализам.

    Возвращает (строки, счётчики): строки — kind, job_id, ref_id, start,
    speaker, snippet (+ filename из jobs), ограниченные лимитами ПО ВИДАМ;
    счётчики — {"analysis": n, "replica": m} с ПОЛНЫМ числом совпадений,
    чтобы UI честно показывал «показано X из Y» и предлагал «показать все».
    Лимиты по видам, а не общий: по BM25 длинные анализы системно уступают
    коротким репликам, и при общем лимите частый термин (например, имя
    спикера) вытеснял анализы за пределы выдачи (реальный случай: 101 реплика
    vs 3 анализа по «Антон»)."""
    tokens = _TOKEN_RE.findall(query or "")
    if not tokens:
        return [], {"analysis": 0, "replica": 0}
    totals = {"analysis": 0, "replica": 0}
    rows: list[dict] = []
    if _fts_ok(conn):
        # Каждый токен — отдельная фраза, AND: «ОРВ акт» найдёт обе формы
        # рядом, а синтаксического мусора от пользовательского ввода нет.
        fts_query = " AND ".join(f'"{t}"' for t in tokens)
        try:
            for r in conn.execute(
                    "SELECT kind, COUNT(*) c FROM search_fts "
                    "WHERE search_fts MATCH ? GROUP BY kind", (fts_query,)):
                totals[r["kind"]] = r["c"]
            for kind, klimit in (("analysis", analysis_limit), ("replica", limit)):
                cur = conn.execute(
                    "SELECT kind, job_id, ref_id, start, speaker, "
                    "snippet(search_fts, 5, ?, ?, '…', 24) AS snip "
                    "FROM search_fts WHERE search_fts MATCH ? AND kind=? "
                    "ORDER BY rank LIMIT ?",
                    (SNIP_OPEN, SNIP_CLOSE, fts_query, kind, klimit))
                rows.extend(dict(r) for r in cur.fetchall())
        except sqlite3.OperationalError:
            rows = []   # синтаксис запроса всё же не прошёл — уходим в LIKE
            totals = {"analysis": 0, "replica": 0}
    if not rows and not any(totals.values()):
        for t in conn.execute("SELECT job_id, segments_json FROM transcripts").fetchall():
            aliases = spk.get_aliases(conn, t["job_id"])
            for i, s in enumerate(json.loads(t["segments_json"])):
                if all(tok.lower() in (s.get("text") or "").lower() for tok in tokens):
                    totals["replica"] += 1
                    if totals["replica"] <= limit:
                        shown = ""
                        if s.get("speaker"):
                            canon = spk.canonical(humanize_speaker(s["speaker"]), aliases)
                            shown = spk.display_name(canon, aliases)
                        rows.append({"kind": "replica", "job_id": t["job_id"],
                                     "ref_id": i, "start": s.get("start", 0.0),
                                     "speaker": shown,
                                     "snip": _snippet_around(s.get("text") or "", tokens[0])})
        for a in conn.execute(
                "SELECT id, job_id, result_md FROM analyses "
                "WHERE status='done' AND result_md IS NOT NULL").fetchall():
            if all(tok.lower() in a["result_md"].lower() for tok in tokens):
                totals["analysis"] += 1
                if totals["analysis"] <= analysis_limit:
                    rows.append({"kind": "analysis", "job_id": a["job_id"],
                                 "ref_id": a["id"], "start": 0.0, "speaker": "",
                                 "snip": _snippet_around(a["result_md"], tokens[0])})
    names = {j["id"]: (j["title"] or j["filename"])
             for j in conn.execute("SELECT id, filename, title FROM jobs")}
    for r in rows:
        r["filename"] = names.get(r["job_id"], f"#{r['job_id']}")
        r["snippet"] = r.pop("snip", "")
    return rows, totals
