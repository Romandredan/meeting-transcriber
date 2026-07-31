from __future__ import annotations

import re
from collections import defaultdict

from app.models import Segment, Word


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def merge_speaker_runs(segments: list[Segment], max_seconds: float = 0.0) -> list[Segment]:
    """Склеивает подряд идущие сегменты одного спикера в одну реплику.

    Таймкод объединённой реплики — от начала первого до конца последнего сегмента;
    текст конкатенируется, слова сохраняются (детализация не теряется). Возвращает
    новый список (входные сегменты не мутируются). Сегменты с speaker=None тоже
    схлопываются по равенству None — поэтому применять имеет смысл к диаризованному
    результату (для читаемого протокола TXT/MD/DOCX).

    `max_seconds` > 0 ограничивает длину блока: если добавление сегмента вывело бы
    блок за порог, начинается новый блок того же спикера (длинный монолог режется на
    под-блоки со своими таймкодами, чтобы не было «стены текста»). 0 — без ограничения.
    """
    merged: list[Segment] = []
    for seg in segments:
        if merged and merged[-1].speaker == seg.speaker:
            prev = merged[-1]
            within_cap = max_seconds <= 0 or (seg.end - prev.start) <= max_seconds
            if within_cap:
                prev.end = seg.end
                prev.text = (prev.text.rstrip() + " " + seg.text.lstrip()).strip()
                prev.words = prev.words + list(seg.words)
                continue
        merged.append(Segment(seg.start, seg.end, seg.text.strip(),
                              seg.speaker, list(seg.words)))
    return merged


def assign_speakers(words: list[Word], turns: list[tuple[float, float, str]]) -> None:
    for w in words:
        best_label, best_ov = None, 0.0
        for t_start, t_end, label in turns:
            ov = _overlap(w.start, w.end, t_start, t_end)
            if ov > best_ov:
                best_ov, best_label = ov, label
        if best_label is not None:
            w.speaker = best_label


def segment_speaker(words: list[Word]) -> str | None:
    totals: dict[str, float] = defaultdict(float)
    for w in words:
        if w.speaker:
            totals[w.speaker] += max(0.0, w.end - w.start)
    if not totals:
        return None
    return max(totals, key=totals.get)


# Метки pyannote имеют вид SPEAKER_00 — только их и «очеловечиваем». Всё
# остальное (уже готовые «Спикер N», имена из speaker_aliases, в т.ч. с цифрами
# вроде «CFO2») возвращаем как есть: humanize обязан быть идемпотентным,
# потому что по конвейеру метка может проходить через него дважды.
_RAW_LABEL_RE = re.compile(r"^[A-Z]+_?\d+$")


def humanize_speaker(label: str | None) -> str:
    if not label:
        return "Спикер ?"
    if not _RAW_LABEL_RE.match(label):
        return label
    digits = "".join(ch for ch in label if ch.isdigit())
    return f"Спикер {int(digits) + 1}"
