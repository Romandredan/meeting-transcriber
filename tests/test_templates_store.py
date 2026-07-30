import sqlite3

import pytest

from app import db, templates_store


def make_conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    return conn


def test_seed_inserts_defaults_once(tmp_path):
    conn = make_conn(tmp_path)
    assert templates_store.seed_defaults(conn) == len(templates_store.DEFAULT_TEMPLATES)
    labels = {r["label"] for r in templates_store.list_templates(conn)}
    assert {"protocol", "requirements", "summary", "daily", "process"} <= labels
    # Второй вызов не трогает таблицу вообще.
    assert templates_store.seed_defaults(conn) == 0


def test_seed_never_overwrites_user_edits(tmp_path):
    """Правки пользователя неприкосновенны: обновление кода их не затирает."""
    conn = make_conn(tmp_path)
    templates_store.seed_defaults(conn)
    row = templates_store.get_by_label(conn, "protocol")
    templates_store.update(conn, row["id"], prompt_body="мой промпт")
    templates_store.seed_defaults(conn)
    assert templates_store.get_by_label(conn, "protocol")["prompt_body"] == "мой промпт"


def test_seed_skips_when_table_not_empty(tmp_path):
    """Непустая таблица — не делаем ничего, даже если дефолтной метки в ней нет."""
    conn = make_conn(tmp_path)
    templates_store.create(conn, "custom", "Своё", "", "тело")
    assert templates_store.seed_defaults(conn) == 0
    assert templates_store.get_by_label(conn, "protocol") is None


def test_label_is_unique(tmp_path):
    conn = make_conn(tmp_path)
    templates_store.create(conn, "protocol", "Протокол", "", "тело")
    with pytest.raises(sqlite3.IntegrityError):
        templates_store.create(conn, "protocol", "Другой", "", "тело")


def test_list_enabled_only_filters(tmp_path):
    conn = make_conn(tmp_path)
    on = templates_store.create(conn, "a", "A", "", "тело")
    off = templates_store.create(conn, "b", "B", "", "тело", enabled=False)
    ids = [r["id"] for r in templates_store.list_templates(conn, enabled_only=True)]
    assert ids == [on]
    assert off in [r["id"] for r in templates_store.list_templates(conn)]


def test_update_toggles_enabled_and_delete_removes(tmp_path):
    conn = make_conn(tmp_path)
    tid = templates_store.create(conn, "a", "A", "", "тело")
    templates_store.update(conn, tid, enabled=False, display_name="A2")
    row = templates_store.get(conn, tid)
    assert row["enabled"] == 0
    assert row["display_name"] == "A2"
    templates_store.delete(conn, tid)
    assert templates_store.get(conn, tid) is None


def test_default_prompts_are_non_empty():
    for t in templates_store.DEFAULT_TEMPLATES:
        assert t["label"] and t["display_name"] and len(t["prompt_body"]) > 100


def test_default_prompts_document_real_replica_format():
    """Дефолтные промпты обязаны описывать вход честно: реплики вида
    [MM:SS] Спикер N: текст, как их реально отдаёт analyze.transcript_replicas.
    Выдуманный формат (например, жирный **[HH:MM:SS]**) даёт модели противоречивое
    описание входа — уже наступали."""
    for t in templates_store.DEFAULT_TEMPLATES:
        assert "[MM:SS]" in t["prompt_body"], t["label"]
        assert "[HH:MM:SS]" not in t["prompt_body"], t["label"]
        assert t["prompt_body"].rstrip().endswith("Расшифровка:"), t["label"]
