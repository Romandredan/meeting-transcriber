from app import device


def test_pick_force_cpu():
    assert device.pick(force="cpu") == ("cpu", "int8")


def test_pick_force_cuda():
    assert device.pick(force="cuda") == ("cuda", "float16")


def test_pick_auto_returns_known_pair():
    dev, ct = device.pick()
    assert (dev, ct) in {("cuda", "float16"), ("cpu", "int8")}
