from app.engine import factory


def test_factory_forces_transformers():
    eng = factory.make_engine("transformers")
    assert eng.__class__.__name__ == "TransformersWhisperEngine"


def test_factory_forces_faster_whisper():
    eng = factory.make_engine("faster_whisper")
    assert eng.__class__.__name__ == "FasterWhisperEngine"
