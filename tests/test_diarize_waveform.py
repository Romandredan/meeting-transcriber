import wave

import numpy as np

from app.diarize import load_waveform


def test_load_waveform_reads_16k_mono(tmp_path):
    p = tmp_path / "a.wav"
    sr = 16000
    samples = (np.sin(np.linspace(0, 12.56, sr)) * 10000).astype("<i2")
    with wave.open(str(p), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())

    waveform, got_sr = load_waveform(str(p))
    assert got_sr == 16000
    assert waveform.shape[0] == 1        # один канал (channel, time)
    assert waveform.shape[1] == sr       # время = число сэмплов
    assert -1.0 <= float(waveform.min()) and float(waveform.max()) <= 1.0  # нормализовано
