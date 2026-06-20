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
