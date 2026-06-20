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


@dataclass
class Settings:
    model: str = "large-v3-turbo"
    language: str | None = None
    diarize: bool = True
    num_speakers: int | None = None
    vocabulary: str = ""

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
        )
