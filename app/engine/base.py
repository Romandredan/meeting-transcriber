from __future__ import annotations

from typing import Callable, Protocol

from app.models import Segment, Settings, TranscriptResult, Word


class TranscriptEngine(Protocol):
    def transcribe(self, audio_path: str, settings: Settings,
                   progress: Callable[[str, float], None]) -> TranscriptResult: ...

    def unload(self) -> None:
        """Освободить видеопамять: перед вызовом локальной LLM модель Whisper должна
        уйти из VRAM, иначе 14B рядом с ней не помещается. Перезагрузка на следующей
        транскрибации стоит единицы секунд — обработка фоновая, это приемлемо."""
        ...


def truncate_prompt(vocabulary: str, max_words: int = 200) -> str:
    words = vocabulary.split()
    if len(words) <= max_words:
        return vocabulary.strip()
    return " ".join(words[:max_words])


def build_segments(raw_segments) -> list[Segment]:
    segments: list[Segment] = []
    for rs in raw_segments:
        words: list[Word] = []
        for rw in (getattr(rs, "words", None) or []):
            words.append(Word(
                start=float(rw.start),
                end=float(rw.end),
                text=str(getattr(rw, "word", "")).strip(),
                score=getattr(rw, "probability", None),
            ))
        segments.append(Segment(
            start=float(rs.start),
            end=float(rs.end),
            text=str(rs.text),
            words=words,
        ))
    return segments
