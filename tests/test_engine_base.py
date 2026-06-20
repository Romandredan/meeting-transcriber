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
