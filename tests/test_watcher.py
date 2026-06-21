import itertools

from app import watcher


def test_is_media():
    assert watcher.is_media("a.mp4")
    assert watcher.is_media("b.MKV")
    assert watcher.is_media("c.wav")
    assert not watcher.is_media("d.txt")


def test_wait_until_stable_true_when_constant():
    ok = watcher.wait_until_stable("f", sig_fn=lambda p: (100, 1.0),
                                   sleep_fn=lambda s: None,
                                   quiet_seconds=4, poll_seconds=2)
    assert ok is True


def test_wait_until_stable_false_when_growing():
    sizes = iter(range(10_000))
    ok = watcher.wait_until_stable("f", sig_fn=lambda p: (next(sizes), 1.0),
                                   sleep_fn=lambda s: None,
                                   quiet_seconds=4, poll_seconds=2, max_wait=20)
    assert ok is False


def test_wait_until_stable_waits_for_streaming_to_finish():
    # Размер растёт 3 опроса (идёт запись), затем фиксируется — как стрим, который дописался.
    it = itertools.chain([(10, 1.0), (20, 2.0), (30, 3.0)], itertools.repeat((30, 5.0)))
    polls = {"n": 0}

    def sig_fn(_):
        polls["n"] += 1
        return next(it)

    ok = watcher.wait_until_stable("f", sig_fn=sig_fn, sleep_fn=lambda s: None,
                                   quiet_seconds=6, poll_seconds=2, max_wait=100)
    assert ok is True
    # Не схватили во время роста: дождались остановки + тихого периода.
    assert polls["n"] >= 6


def test_wait_until_stable_false_when_file_gone():
    def boom(_):
        raise OSError("нет файла")

    assert watcher.wait_until_stable("f", sig_fn=boom, sleep_fn=lambda s: None) is False
