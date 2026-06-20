from __future__ import annotations

from typing import Callable

from app import config
from app.engine.base import truncate_prompt
from app.models import Segment, Settings, TranscriptResult, Word
from app.stitch import assign_speakers, segment_speaker

_MODEL_MAP = {
    "large-v3-turbo": "openai/whisper-large-v3-turbo",
    "large-v3": "openai/whisper-large-v3",
}


class TransformersWhisperEngine:
    name = "transformers"

    def __init__(self) -> None:
        self._pipes: dict[str, object] = {}

    def _get_pipe(self, model_name: str):
        if model_name not in self._pipes:
            import torch
            from transformers import pipeline
            from app.device import pick
            hf_id = _MODEL_MAP.get(model_name, model_name)
            dev, _ = pick()
            dtype = torch.float16 if dev == "cuda" else torch.float32
            self._pipes[model_name] = pipeline(
                "automatic-speech-recognition", model=hf_id,
                torch_dtype=dtype, device=dev,
            )
        return self._pipes[model_name]

    def transcribe(self, audio_path: str, settings: Settings,
                   progress: Callable[[str, float], None]) -> TranscriptResult:
        progress("transcribe", 0.1)
        pipe = self._get_pipe(settings.model)
        prompt = truncate_prompt(settings.vocabulary) or None
        generate_kwargs = {"language": settings.language} if settings.language else {}
        if prompt:
            generate_kwargs["prompt"] = prompt
        out = pipe(audio_path, return_timestamps="word", chunk_length_s=30,
                   batch_size=8, generate_kwargs=generate_kwargs)
        words = [Word(start=float(c["timestamp"][0] or 0.0),
                      end=float(c["timestamp"][1] or 0.0),
                      text=c["text"].strip())
                 for c in out.get("chunks", []) if c.get("timestamp")]
        segment = Segment(start=words[0].start if words else 0.0,
                          end=words[-1].end if words else 0.0,
                          text=out.get("text", "").strip(), words=words)
        segments = [segment] if words else []
        progress("transcribe", 0.6)

        diarized = False
        if settings.diarize and segments:
            progress("diarize", 0.65)
            from app.diarize import diarize_audio
            turns = diarize_audio(audio_path, settings.num_speakers, config.HF_TOKEN)
            for seg in segments:
                assign_speakers(seg.words, turns)
                seg.speaker = segment_speaker(seg.words)
            diarized = True
        progress("diarize", 0.9)

        duration = segments[-1].end if segments else 0.0
        return TranscriptResult(language=settings.language or "ru", duration=duration,
                                model=settings.model, diarized=diarized, segments=segments)
