from __future__ import annotations

from app.engine.base import TranscriptEngine


def make_engine(backend: str = "auto") -> TranscriptEngine:
    if backend == "transformers":
        from app.engine.transformers_engine import TransformersWhisperEngine
        return TransformersWhisperEngine()
    if backend == "faster_whisper":
        from app.engine.faster_whisper_engine import FasterWhisperEngine
        return FasterWhisperEngine()
    # auto
    try:
        import ctranslate2  # noqa: F401
        from app.engine.faster_whisper_engine import FasterWhisperEngine
        return FasterWhisperEngine()
    except Exception:
        from app.engine.transformers_engine import TransformersWhisperEngine
        return TransformersWhisperEngine()
