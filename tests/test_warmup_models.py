import scripts.warmup_models as wm


def test_main_unknown_argument_is_error(capsys):
    assert wm.main(["warmup_models.py", "bogus"]) == 1
    assert "Неизвестный аргумент" in capsys.readouterr().err


def test_main_all_runs_both_warmups(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(wm, "warmup_whisper", lambda: calls.append("whisper") or "large-v3-turbo")
    monkeypatch.setattr(wm, "warmup_pyannote", lambda: calls.append("pyannote") or "pyannote/x")
    assert wm.main(["warmup_models.py"]) == 0
    assert calls == ["whisper", "pyannote"]
    out = capsys.readouterr().out
    assert "large-v3-turbo" in out and "pyannote/x" in out


def test_main_selective_warmup(monkeypatch):
    calls = []
    monkeypatch.setattr(wm, "warmup_whisper", lambda: calls.append("whisper") or "m")
    monkeypatch.setattr(wm, "warmup_pyannote", lambda: calls.append("pyannote") or "p")
    assert wm.main(["warmup_models.py", "whisper"]) == 0
    assert calls == ["whisper"]


def test_main_pyannote_without_token_is_skip_not_error(monkeypatch, capsys):
    monkeypatch.setattr(wm, "warmup_whisper", lambda: "m")
    monkeypatch.setattr(wm, "warmup_pyannote", lambda: None)
    assert wm.main(["warmup_models.py"]) == 0
    assert "пропущены" in capsys.readouterr().out


def test_main_failure_returns_1_with_russian_message(monkeypatch, capsys):
    def boom():
        raise ConnectionError("no route")

    monkeypatch.setattr(wm, "warmup_whisper", boom)
    monkeypatch.setattr(wm, "warmup_pyannote", lambda: None)
    assert wm.main(["warmup_models.py"]) == 1
    err = capsys.readouterr().err
    assert "Не удалось скачать модели" in err and "no route" in err


def test_warmup_pyannote_without_token_does_not_import_pyannote(monkeypatch):
    monkeypatch.setattr(wm.config, "HF_TOKEN", None)
    assert wm.warmup_pyannote() is None
