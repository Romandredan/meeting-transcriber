from __future__ import annotations
import logging

_log = logging.getLogger(__name__)


class DiarizationError(Exception):
    pass


_PIPELINE = None


def _get_pipeline(hf_token: str | None):
    global _PIPELINE
    if hf_token is None:
        raise DiarizationError("Диаризация требует HF_TOKEN (pyannote). Укажите токен в .env.")
    if _PIPELINE is None:
        from pyannote.audio import Pipeline
        from app.device import torch_device
        _PIPELINE = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1", use_auth_token=hf_token
        )
        _PIPELINE.to(torch_device())
    return _PIPELINE


def diarize_audio(audio_path: str, num_speakers: int | None,
                  hf_token: str | None) -> list[tuple[float, float, str]]:
    pipeline = _get_pipeline(hf_token)
    kwargs = {}
    if num_speakers:
        kwargs["num_speakers"] = num_speakers
    annotation = pipeline(audio_path, **kwargs)
    turns: list[tuple[float, float, str]] = []
    for segment, _, label in annotation.itertracks(yield_label=True):
        turns.append((float(segment.start), float(segment.end), str(label)))
    return turns


def apply_diarization(segments, audio_path, num_speakers, hf_token) -> bool:
    """Применяет диаризацию к сегментам. При недоступности (нет токена / ошибка GPU)
    деградирует: логирует предупреждение, возвращает False, сегменты остаются без спикеров."""
    try:
        turns = diarize_audio(audio_path, num_speakers, hf_token)
    except Exception as e:  # DiarizationError, ошибки GPU/модели и т.п.
        _log.warning("Диаризация пропущена: %s", e)
        return False
    from app.stitch import assign_speakers, segment_speaker
    for seg in segments:
        assign_speakers(seg.words, turns)
        seg.speaker = segment_speaker(seg.words)
    return True
