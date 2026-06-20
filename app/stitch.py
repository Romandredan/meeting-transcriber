from __future__ import annotations

from collections import defaultdict

from app.models import Word


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


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


def humanize_speaker(label: str | None) -> str:
    if not label:
        return "Спикер ?"
    digits = "".join(ch for ch in label if ch.isdigit())
    if digits == "":
        return label
    return f"Спикер {int(digits) + 1}"
