"""TDD-тесты для app.diarize.apply_diarization (FIX A)."""
from __future__ import annotations

import app.diarize as diarize_module
from app.diarize import DiarizationError
from app.models import Segment, Word


def _make_segment():
    w = [Word(0.0, 1.0, "слово")]
    return Segment(0.0, 1.0, "слово", words=w)


def test_apply_diarization_degrades_on_error(monkeypatch):
    """Когда diarize_audio бросает DiarizationError, apply_diarization возвращает False
    и сегмент остаётся без спикера."""
    def _raise(*args, **kwargs):
        raise DiarizationError("нет токена")

    monkeypatch.setattr(diarize_module, "diarize_audio", _raise)

    from app.diarize import apply_diarization
    seg = _make_segment()
    result = apply_diarization([seg], "audio.wav", None, None)

    assert result is False
    assert seg.speaker is None


def test_apply_diarization_assigns_speakers_on_success(monkeypatch):
    """Когда diarize_audio возвращает turns, apply_diarization возвращает True
    и назначает спикера сегменту."""
    turns = [(0.0, 1.0, "SPEAKER_00")]
    monkeypatch.setattr(diarize_module, "diarize_audio", lambda *a, **kw: turns)

    from app.diarize import apply_diarization
    seg = _make_segment()
    result = apply_diarization([seg], "audio.wav", None, None)

    assert result is True
    assert seg.speaker == "SPEAKER_00"
    assert seg.words[0].speaker == "SPEAKER_00"
