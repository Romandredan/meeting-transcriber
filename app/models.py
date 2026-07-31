from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"


@dataclass
class Word:
    start: float
    end: float
    text: str
    speaker: str | None = None
    score: float | None = None


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[Word] = field(default_factory=list)


@dataclass
class TranscriptResult:
    language: str
    duration: float
    model: str
    diarized: bool
    segments: list[Segment]

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "duration": self.duration,
            "model": self.model,
            "diarized": self.diarized,
            "segments": [asdict(s) for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TranscriptResult":
        """Поднимает результат из JSON-файла (обратная операция к to_dict).

        Собирает Segment и Word объектами: asdict() в to_dict разворачивает их в
        словари, и без явной сборки дальше по конвейеру поедят dict'ы."""
        segments: list[Segment] = []
        for s in data.get("segments", []):
            words = [Word(start=float(w.get("start", 0.0)),
                          end=float(w.get("end", 0.0)),
                          text=str(w.get("text", "")),
                          speaker=w.get("speaker"),
                          score=w.get("score"))
                     for w in (s.get("words") or [])]
            segments.append(Segment(start=float(s.get("start", 0.0)),
                                    end=float(s.get("end", 0.0)),
                                    text=str(s.get("text", "")),
                                    speaker=s.get("speaker"),
                                    words=words))
        return cls(
            language=str(data.get("language", "")),
            duration=float(data.get("duration", 0.0)),
            model=str(data.get("model", "")),
            diarized=bool(data.get("diarized", False)),
            segments=segments,
        )


@dataclass
class Settings:
    model: str = "large-v3-turbo"
    language: str | None = None
    diarize: bool = True
    num_speakers: int | None = None
    vocabulary: str = ""
    # Авто-анализ после расшифровки: "" — выкл, "auto" — классификация типа
    # встречи LLM, иначе — метка шаблона, которым анализ ставится автоматически.
    auto_analyze: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        return cls(
            model=data.get("model", "large-v3-turbo"),
            language=data.get("language"),
            diarize=data.get("diarize", True),
            num_speakers=data.get("num_speakers"),
            vocabulary=data.get("vocabulary", ""),
            auto_analyze=data.get("auto_analyze", "") or "",
        )
