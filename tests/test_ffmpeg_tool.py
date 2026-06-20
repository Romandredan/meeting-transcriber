import pytest
from app import ffmpeg_tool


def test_build_extract_cmd_has_required_audio_params():
    cmd = ffmpeg_tool.build_extract_cmd("in.mp4", "out.wav", ffmpeg="ffmpeg")
    assert "-ar" in cmd and "16000" in cmd
    assert "-ac" in cmd and "1" in cmd
    assert "pcm_s16le" in cmd
    assert cmd[-1] == "out.wav"
    assert "in.mp4" in cmd


def test_extract_audio_raises_when_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(ffmpeg_tool, "find_ffmpeg", lambda: None)
    with pytest.raises(ffmpeg_tool.FFmpegError) as e:
        ffmpeg_tool.extract_audio("in.mp4", "out.wav")
    assert "ffmpeg" in str(e.value).lower()
