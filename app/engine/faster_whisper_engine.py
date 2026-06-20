from __future__ import annotations

from typing import Callable

from app import config
from app.engine.base import build_segments, truncate_prompt
from app.models import Settings, TranscriptResult


class FasterWhisperEngine:
    name = "faster-whisper"

    def __init__(self) -> None:
        self._models: dict[str, object] = {}

    def _get_model(self, model_name: str):
        if model_name not in self._models:
            from faster_whisper import WhisperModel
            from app.device import pick
            dev, compute_type = pick()
            self._models[model_name] = WhisperModel(
                model_name, device=dev, compute_type=compute_type,
                download_root=str(config.MODELS_DIR),
            )
        return self._models[model_name]

    def transcribe(self, audio_path: str, settings: Settings,
                   progress: Callable[[str, float], None]) -> TranscriptResult:
        progress("transcribe", 0.1)
        model = self._get_model(settings.model)
        prompt = truncate_prompt(settings.vocabulary) or None
        raw_segments, info = model.transcribe(
            audio_path,
            language=settings.language,
            initial_prompt=prompt,
            word_timestamps=True,
            vad_filter=True,
        )
        segments = build_segments(raw_segments)
        progress("transcribe", 0.6)

        diarized = False
        if settings.diarize and segments:
            progress("diarize", 0.65)
            from app.diarize import apply_diarization
            diarized = apply_diarization(segments, audio_path, settings.num_speakers, config.HF_TOKEN)
        progress("diarize", 0.9)

        duration = segments[-1].end if segments else 0.0
        return TranscriptResult(
            language=info.language, duration=duration,
            model=settings.model, diarized=diarized, segments=segments,
        )
