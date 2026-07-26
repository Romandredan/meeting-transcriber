from types import SimpleNamespace
from app.engine import base
from app.models import Segment


def test_truncate_prompt_limits_words():
    vocab = " ".join(f"термин{i}" for i in range(300))
    out = base.truncate_prompt(vocab, max_words=200)
    assert len(out.split()) == 200


def test_truncate_prompt_short_passthrough():
    assert base.truncate_prompt("АккордПост ОФД", max_words=200) == "АккордПост ОФД"


def test_build_segments_normalizes():
    raw_word = SimpleNamespace(start=0.0, end=0.5, word="привет", probability=0.9)
    raw_seg = SimpleNamespace(start=0.0, end=0.5, text=" привет", words=[raw_word])
    segs = base.build_segments([raw_seg])
    assert isinstance(segs[0], Segment)
    assert segs[0].text == " привет"
    assert segs[0].words[0].text == "привет"
    assert segs[0].words[0].score == 0.9


def test_faster_whisper_unload_clears_cached_models():
    from app.engine.faster_whisper_engine import FasterWhisperEngine
    eng = FasterWhisperEngine()
    eng._models["large-v3"] = object()   # как будто модель уже загружена
    eng.unload()
    assert eng._models == {}
    eng.unload()                          # повторный вызов безопасен


def test_transformers_unload_clears_cached_pipes():
    from app.engine.transformers_engine import TransformersWhisperEngine
    eng = TransformersWhisperEngine()
    eng._pipes["large-v3"] = object()
    eng.unload()
    assert eng._pipes == {}


def test_faster_whisper_unload_clears_diarization_pipeline():
    """Диаризация делит VRAM с Whisper и LLM — выгрузка движка должна освобождать
    и её глобальный пайплайн, а не только кэш моделей Whisper."""
    import app.diarize as diarize_module
    from app.engine.faster_whisper_engine import FasterWhisperEngine
    diarize_module._PIPELINE = object()   # как будто пайплайн уже загружен на GPU
    eng = FasterWhisperEngine()
    eng.unload()
    assert diarize_module._PIPELINE is None


def test_transformers_unload_clears_diarization_pipeline():
    import app.diarize as diarize_module
    from app.engine.transformers_engine import TransformersWhisperEngine
    diarize_module._PIPELINE = object()
    eng = TransformersWhisperEngine()
    eng.unload()
    assert diarize_module._PIPELINE is None


def test_diarize_unload_pipeline_resets_global_without_loading_models():
    import app.diarize as diarize_module
    diarize_module._PIPELINE = object()
    diarize_module.unload_pipeline()
    assert diarize_module._PIPELINE is None
    diarize_module.unload_pipeline()   # повторный вызов безопасен (уже None)
