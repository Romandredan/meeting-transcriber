from app import watcher


def test_is_media():
    assert watcher.is_media("a.mp4")
    assert watcher.is_media("b.MKV")
    assert watcher.is_media("c.wav")
    assert not watcher.is_media("d.txt")


def test_wait_until_stable_true_when_size_constant():
    sizes = iter([100, 100, 100, 100])
    ok = watcher.wait_until_stable("f", size_fn=lambda p: next(sizes),
                                   sleep_fn=lambda s: None, checks=3, interval=0)
    assert ok is True


def test_wait_until_stable_false_when_growing():
    sizes = iter([100, 200, 300, 400, 500, 600])
    ok = watcher.wait_until_stable("f", size_fn=lambda p: next(sizes),
                                   sleep_fn=lambda s: None, checks=3, interval=0)
    assert ok is False
