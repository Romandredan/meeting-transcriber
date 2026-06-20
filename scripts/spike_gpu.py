"""De-risking spike: проверяет, что faster-whisper и pyannote реально работают на GPU (Blackwell sm_120).

Запуск:  .venv/Scripts/python scripts/spike_gpu.py [путь_к_короткому_аудио_или_видео]
Если путь не задан — генерирует 10 сек тонового WAV через ffmpeg (проверяет только GPU-путь, не качество).
"""
import subprocess
import sys
import tempfile
from pathlib import Path


def make_tone(path: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", path],
        check=True, capture_output=True,
    )


def main() -> int:
    import torch
    print(f"torch={torch.__version__} cuda_available={torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"device={torch.cuda.get_device_name(0)} capability={torch.cuda.get_device_capability(0)}")
    if not torch.cuda.is_available():
        print("FAIL: CUDA недоступна для PyTorch")
        return 1

    tmp = tempfile.mkdtemp()
    audio = sys.argv[1] if len(sys.argv) > 1 else str(Path(tmp) / "tone.wav")
    if len(sys.argv) <= 1:
        make_tone(audio)

    # --- faster-whisper на GPU ---
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("large-v3", device="cuda", compute_type="float16")
        segments, info = model.transcribe(audio, language="ru", word_timestamps=True)
        segs = list(segments)
        print(f"OK faster-whisper: lang={info.language} segments={len(segs)}")
        ct2_ok = True
    except Exception as e:
        print(f"FAIL faster-whisper на cuda: {type(e).__name__}: {e}")
        ct2_ok = False

    # --- pyannote на GPU ---
    try:
        import os
        from pyannote.audio import Pipeline
        token = os.environ.get("HF_TOKEN")
        if not token:
            print("SKIP pyannote: нет HF_TOKEN")
            pyannote_ok = None
        else:
            pipe = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=token)
            pipe.to(torch.device("cuda"))
            diar = pipe(audio)
            print(f"OK pyannote: turns={len(list(diar.itertracks()))}")
            pyannote_ok = True
    except Exception as e:
        print(f"FAIL pyannote на cuda: {type(e).__name__}: {e}")
        pyannote_ok = False

    print("\n=== ИТОГ ===")
    print(f"faster-whisper(CT2) GPU: {'OK' if ct2_ok else 'FAIL → использовать transformers_engine'}")
    print(f"pyannote GPU: {pyannote_ok}")
    return 0 if ct2_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
