import json
from app.models import Word, Segment, TranscriptResult
from app import writers


def sample():
    w = [Word(0.0, 1.0, "привет", speaker="SPEAKER_00"),
         Word(1.0, 2.0, "мир", speaker="SPEAKER_00")]
    s = Segment(0.0, 2.0, "привет мир", speaker="SPEAKER_00", words=w)
    return TranscriptResult("ru", 2.0, "large-v3-turbo", True, [s])


def test_format_timestamp_srt():
    assert writers.format_timestamp(62.5, ",") == "00:01:02,500"


def test_format_timestamp_vtt():
    assert writers.format_timestamp(62.5, ".") == "00:01:02.500"


def test_to_txt_has_speaker_and_time():
    txt = writers.to_txt(sample())
    assert "[00:00:00]" in txt
    assert "Спикер 1:" in txt
    assert "привет мир" in txt


def test_to_srt_structure():
    srt = writers.to_srt(sample())
    assert "1\n" in srt
    assert "00:00:00,000 --> 00:00:02,000" in srt


def test_to_json_valid():
    data = json.loads(writers.to_json(sample()))
    assert data["language"] == "ru"
    assert data["segments"][0]["text"] == "привет мир"


def test_write_all_creates_files(tmp_path):
    paths = writers.write_all(sample(), str(tmp_path), ["txt", "srt", "json"], "meeting")
    names = sorted(p.split("/")[-1].split("\\")[-1] for p in paths)
    assert names == ["meeting.json", "meeting.srt", "meeting.txt"]
    for p in paths:
        assert open(p, encoding="utf-8").read()
