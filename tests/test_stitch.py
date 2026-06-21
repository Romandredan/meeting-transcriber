from app.models import Segment, Word
from app import stitch


def test_assign_speakers_by_overlap():
    words = [Word(0.0, 1.0, "a"), Word(1.2, 2.0, "b")]
    turns = [(0.0, 1.0, "SPEAKER_00"), (1.1, 3.0, "SPEAKER_01")]
    stitch.assign_speakers(words, turns)
    assert words[0].speaker == "SPEAKER_00"
    assert words[1].speaker == "SPEAKER_01"


def test_assign_speakers_no_overlap_leaves_none():
    words = [Word(5.0, 6.0, "x")]
    turns = [(0.0, 1.0, "SPEAKER_00")]
    stitch.assign_speakers(words, turns)
    assert words[0].speaker is None


def test_segment_speaker_dominant():
    words = [Word(0, 1, "a", speaker="SPEAKER_00"),
             Word(1, 3, "b", speaker="SPEAKER_01"),
             Word(3, 3.5, "c", speaker="SPEAKER_01")]
    assert stitch.segment_speaker(words) == "SPEAKER_01"


def test_humanize_speaker():
    assert stitch.humanize_speaker("SPEAKER_00") == "Спикер 1"
    assert stitch.humanize_speaker("SPEAKER_05") == "Спикер 6"
    assert stitch.humanize_speaker(None) == "Спикер ?"


def test_merge_speaker_runs_collapses_consecutive():
    segs = [
        Segment(0.0, 1.0, "привет", speaker="SPEAKER_00",
                words=[Word(0, 1, "привет", speaker="SPEAKER_00")]),
        Segment(1.0, 2.0, "как дела", speaker="SPEAKER_00",
                words=[Word(1, 2, "как", speaker="SPEAKER_00")]),
        Segment(2.0, 3.0, "нормально", speaker="SPEAKER_01",
                words=[Word(2, 3, "нормально", speaker="SPEAKER_01")]),
    ]
    merged = stitch.merge_speaker_runs(segs)
    assert len(merged) == 2
    assert merged[0].speaker == "SPEAKER_00"
    assert merged[0].start == 0.0 and merged[0].end == 2.0
    assert merged[0].text == "привет как дела"
    assert len(merged[0].words) == 2          # слова сохранены
    assert merged[1].speaker == "SPEAKER_01"
    # вход не мутирован
    assert len(segs) == 3 and segs[0].end == 1.0
