"""Тесты механизма миграций схемы БД (PRAGMA user_version + MIGRATIONS).

Механику проверяем на фейковых миграциях через monkeypatch — реальные
миграции (speaker_aliases, transcripts, ...) тестируются в своих модулях.
"""
import sqlite3

import pytest

from app import db


def user_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def test_fresh_db_gets_latest_version(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    assert user_version(conn) == db.SCHEMA_VERSION
    # базовая схема на месте
    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "settings", "templates", "analyses"} <= names


def test_migrations_apply_in_order_once(tmp_path, monkeypatch):
    calls = []

    def step1(c):
        calls.append(1)
        c.execute("CREATE TABLE m1 (id INTEGER)")

    def step2(c):
        calls.append(2)
        c.execute("CREATE TABLE m2 (id INTEGER)")

    monkeypatch.setattr(db, "MIGRATIONS", [(1, "первая", step1), (2, "вторая", step2)])
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)

    assert calls == [1, 2]  # обе применены, по порядку
    assert user_version(conn) == 2

    db.migrate(conn)  # повторный прогон — no-op
    assert calls == [1, 2]


def test_old_db_catches_up_only_missing(tmp_path, monkeypatch):
    """БД на версии 1 получает только миграции новее неё."""
    conn = db.connect(tmp_path / "t.db")
    db.init_schema(conn)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()

    calls = []

    def step1(c):
        calls.append(1)

    def step2(c):
        calls.append(2)

    monkeypatch.setattr(db, "MIGRATIONS", [(1, "уже есть", step1), (2, "новая", step2)])
    db.migrate(conn)

    assert calls == [2]
    assert user_version(conn) == 2


def test_failed_migration_rolls_back_and_keeps_version(tmp_path, monkeypatch):
    def bad_step(c):
        c.execute("CREATE TABLE half (id INTEGER)")
        raise RuntimeError("бум")

    monkeypatch.setattr(db, "MIGRATIONS", [(1, "сломанная", bad_step)])
    conn = db.connect(tmp_path / "t.db")
    conn.executescript(db.SCHEMA)  # только базовая схема, без миграций

    with pytest.raises(RuntimeError):
        db.migrate(conn)

    assert user_version(conn) == 0  # версия не повысилась
    # следов транзакции не осталось — следующая попытка начнётся с чистого листа
    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "half" not in names


def test_init_schema_on_existing_user_db_is_safe(tmp_path):
    """Симуляция БД реального пользователя: таблицы и данные уже есть,
    user_version = 0. init_schema не должен ничего потерять."""
    path = tmp_path / "t.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO templates (label, display_name, prompt_body) "
                 "VALUES ('protocol', 'Протокол', 'текст')")
    conn.commit()
    conn.close()

    conn = db.connect(path)
    db.init_schema(conn)
    assert conn.execute("SELECT COUNT(*) c FROM templates").fetchone()["c"] == 1
    assert user_version(conn) == db.SCHEMA_VERSION
