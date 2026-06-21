from app import config


def test_paths_under_base():
    assert config.INBOX_DIR.name == "inbox"
    assert config.OUTPUT_DIR.name == "output"
    assert config.DB_PATH.name == "data.db"


def test_default_port_not_8000():
    assert config.APP_PORT == 8473
    assert config.APP_HOST == "127.0.0.1"


def test_ensure_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(config, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")
    config.ensure_dirs()
    assert (tmp_path / "inbox").is_dir()
    assert (tmp_path / "output").is_dir()
    assert (tmp_path / "processed").is_dir()


def test_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("X_DIR", str(tmp_path / "z"))
    assert config._dir("X_DIR", tmp_path / "def") == tmp_path / "z"
    monkeypatch.delenv("X_DIR")
    assert config._dir("X_DIR", tmp_path / "def") == tmp_path / "def"


def test_flag_parsing(monkeypatch):
    monkeypatch.setenv("F", "false")
    assert config._flag("F", True) is False
    monkeypatch.setenv("F", "true")
    assert config._flag("F", False) is True
    monkeypatch.delenv("F")
    assert config._flag("F", True) is True
