"""Имена и объединения спикеров (speaker_aliases).

Сырые метки диаризации (SPEAKER_00) живут в JSON-транскрипте на диске и не
переписываются никогда. Алиасы (отображаемое имя) и слияния (объединение
двух меток одного человека) хранятся в БД и применяются **на чтении**:

    сырая метка → человеческая («Спикер 1») → merged_into → отображаемое имя

Склейку соседних реплик объединённых спикеров делает обычный
stitch.merge_speaker_runs дальше по конвейеру — здесь мы только переписываем
метки, поэтому слияние обратимо и не трогает первичные данные.
"""
from __future__ import annotations

import logging
import os
import sqlite3

from app import writers
from app.models import Segment, TranscriptResult, Word
from app.stitch import humanize_speaker

log = logging.getLogger(__name__)

MAX_NAME_LEN = 64

AliasMap = dict[str, dict]   # «Спикер 1» → {"name": str, "merged_into": str | None}


# ── хранилище ────────────────────────────────────────────────────────────────

def get_aliases(conn: sqlite3.Connection, job_id: int) -> AliasMap:
    rows = conn.execute(
        "SELECT speaker, name, merged_into FROM speaker_aliases WHERE job_id=?",
        (job_id,)).fetchall()
    return {r["speaker"]: {"name": r["name"], "merged_into": r["merged_into"]}
            for r in rows}


def set_aliases(conn: sqlite3.Connection, job_id: int, aliases: AliasMap) -> None:
    """Заменяет алиасы встречи целиком. Пустые записи (ни имени, ни слияния)
    не храним — отсутствие строки и есть «без алиаса»."""
    conn.execute("DELETE FROM speaker_aliases WHERE job_id=?", (job_id,))
    for label, info in aliases.items():
        name = (info.get("name") or "").strip()
        merged = info.get("merged_into") or None
        if not name and not merged:
            continue
        conn.execute(
            "INSERT INTO speaker_aliases (job_id, speaker, name, merged_into) "
            "VALUES (?, ?, ?, ?)", (job_id, label, name, merged))
    conn.commit()


def delete_for_job(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("DELETE FROM speaker_aliases WHERE job_id=?", (job_id,))


# ── метки и представление ────────────────────────────────────────────────────

def _label_key(label: str) -> tuple[int, str]:
    digits = "".join(ch for ch in label if ch.isdigit())
    return (int(digits) if digits else 0, label)


def raw_speakers(result: TranscriptResult) -> list[str]:
    """Человеческие метки всех спикеров транскрипта, по номерам («Спикер 1», …)."""
    labels = {humanize_speaker(s.speaker) for s in result.segments if s.speaker}
    return sorted(labels, key=_label_key)


def canonical(label: str, aliases: AliasMap) -> str:
    """Метка с учётом слияния (один уровень — цепочки запрещены валидацией)."""
    info = aliases.get(label)
    if info and info.get("merged_into"):
        return info["merged_into"]
    return label


def display_name(label: str, aliases: AliasMap) -> str:
    """Отображаемое имя канонической метки (или сама метка, если имени нет)."""
    info = aliases.get(label)
    if info and info.get("name"):
        return info["name"]
    return label


def apply_view(result: TranscriptResult, aliases: AliasMap) -> TranscriptResult:
    """Новый TranscriptResult, где сегменты подписаны финальными именами.

    Конвейер чтения: сырая метка → человеческая («Спикер 1») → merged_into →
    имя. Склейка абзацев — дальше, в merge_speaker_runs (она сработает сама,
    потому что слитые спикеры получают одинаковую отображаемую метку).
    Без алиасов просто «очеловечивает» сырые метки SPEAKER_00 → «Спикер 1»:
    сырой вид наружу (в UI и файлы) не отдаём нигде."""
    if not result.diarized:
        return result
    segments: list[Segment] = []
    for s in result.segments:
        if not s.speaker:
            segments.append(s)
            continue
        shown = display_name(canonical(humanize_speaker(s.speaker), aliases), aliases)
        words = [Word(w.start, w.end, w.text,
                      display_name(canonical(humanize_speaker(w.speaker), aliases), aliases)
                      if w.speaker else None,
                      w.score)
                 for w in s.words]
        segments.append(Segment(s.start, s.end, s.text, shown, words))
    return TranscriptResult(result.language, result.duration, result.model,
                            result.diarized, segments)


def speaker_stats(result: TranscriptResult) -> list[dict]:
    """Строки панели «Указать имена»: по каждой сырой метке — число реплик,
    суммарное время речи и превью первой реплики (чтобы понять, кто это)."""
    stats: dict[str, dict] = {}
    for s in result.segments:
        if not s.speaker:
            continue
        label = humanize_speaker(s.speaker)
        st = stats.setdefault(label, {"label": label, "utterances": 0,
                                      "seconds": 0.0, "preview": ""})
        st["utterances"] += 1
        st["seconds"] += max(0.0, s.end - s.start)
        if not st["preview"] and s.text.strip():
            st["preview"] = s.text.strip()[:140]
    return [stats[label] for label in sorted(stats, key=_label_key)]


def validate_aliases(aliases: AliasMap, known_labels: list[str]) -> str | None:
    """Проверяет алиасы перед сохранением. Возвращает текст ошибки (по-русски,
    показывается пользователю) или None, если всё в порядке."""
    known = set(known_labels)
    for label, info in aliases.items():
        if label not in known:
            return f"спикера «{label}» нет в этой встрече"
        name = (info.get("name") or "").strip()
        if len(name) > MAX_NAME_LEN:
            return (f"имя для «{label}» длиннее {MAX_NAME_LEN} символов — "
                    f"сократите, пожалуйста")
        merged = info.get("merged_into")
        if merged:
            if merged not in known:
                return f"«{label}» объединяется с «{merged}», но такого спикера нет"
            if merged == label:
                return f"«{label}» нельзя объединить самого с собой"
            # Один уровень слияний: цель сама никуда не слита — иначе цепочки
            # (и потенциальные циклы) усложнили бы чтение без пользы.
            target = aliases.get(merged) or {}
            if target.get("merged_into"):
                return (f"«{merged}» уже объединён с «{target['merged_into']}» — "
                        f"цепочки объединений не поддерживаются")
    return None


# ── перегенерация файлов ─────────────────────────────────────────────────────

# Форматы, которые пересобираются с именами. JSON не трогаем: он сырой источник,
# из которого всё перестраивается. SRT/VTT — тоже перегенерируем: субтитры с
# префиксом «Спикер N» должны показывать те же имена, что и протокол.
_RERENDER_FORMATS = ("txt", "srt", "vtt", "md", "docx")


def rerender_outputs(result: TranscriptResult, out_dir: str, basename: str,
                     aliases: AliasMap) -> None:
    """Пересобирает читаемые форматы в output/<id>/ с учётом имён и слияний.

    Best-effort по инварианту «БД — истина, файлы — копии»: алиасы уже
    сохранены, поэтому сбой записи только логируем. Трогаем только файлы,
    которые уже существуют, — отключённые пользователем форматы не создаём."""
    if not os.path.isdir(out_dir):
        return
    view = apply_view(result, aliases)
    for fmt in _RERENDER_FORMATS:
        path = os.path.join(out_dir, f"{basename}.{fmt}")
        if not os.path.isfile(path):
            continue
        try:
            if fmt == "docx":
                writers.to_docx(view, path)
            else:
                text = {"txt": writers.to_txt, "srt": writers.to_srt,
                        "vtt": writers.to_vtt, "md": writers.to_md}[fmt](view)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
        except Exception as e:
            log.warning("Не удалось пересобрать %s с именами спикеров: %s", path, e)
