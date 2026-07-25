from app.models import Word, Segment, TranscriptResult, JobStatus, Settings


def test_transcript_result_to_dict_roundtrip():
    w = Word(start=0.0, end=0.5, text="привет", speaker="SPEAKER_00", score=0.9)
    s = Segment(start=0.0, end=0.5, text="привет", speaker="SPEAKER_00", words=[w])
    r = TranscriptResult(language="ru", duration=0.5, model="large-v3-turbo", diarized=True, segments=[s])
    d = r.to_dict()
    assert d["language"] == "ru"
    assert d["segments"][0]["words"][0]["text"] == "привет"
    assert d["segments"][0]["speaker"] == "SPEAKER_00"


def test_settings_from_dict_defaults():
    st = Settings.from_dict({"model": "large-v3", "diarize": False})
    assert st.model == "large-v3"
    assert st.diarize is False
    assert st.language is None
    assert st.vocabulary == ""


def test_job_status_values():
    assert JobStatus.QUEUED.value == "queued"
    assert JobStatus.DONE == "done"


def test_transcript_result_from_dict_restores_nested_objects():
    """to_dict → from_dict возвращает Segment/Word объектами, а не словарями.

    Наивная реализация (Segment(**s)) оставляет words списком dict'ов: код склейки
    реплик молча пройдёт мимо (он трогает .text/.speaker сегмента), а анализ получит
    правдоподобный, но битый результат."""
    w = Word(start=0.0, end=0.5, text="привет", speaker="SPEAKER_00", score=0.9)
    s = Segment(start=0.0, end=0.5, text="привет", speaker="SPEAKER_00", words=[w])
    src = TranscriptResult(language="ru", duration=0.5, model="large-v3-turbo",
                           diarized=True, segments=[s])
    back = TranscriptResult.from_dict(src.to_dict())
    assert back.language == "ru"
    assert back.duration == 0.5
    assert back.diarized is True
    assert isinstance(back.segments[0], Segment)
    assert isinstance(back.segments[0].words[0], Word)
    assert back.segments[0].words[0].text == "привет"
    assert back.segments[0].speaker == "SPEAKER_00"


def test_transcript_result_from_dict_tolerates_missing_words():
    """JSON без слов (например, из старого запуска) не должен ронять разбор."""
    r = TranscriptResult.from_dict({
        "language": "ru", "duration": 1.0, "model": "m", "diarized": False,
        "segments": [{"start": 0.0, "end": 1.0, "text": "тест"}],
    })
    assert r.segments[0].words == []
    assert r.segments[0].speaker is None
