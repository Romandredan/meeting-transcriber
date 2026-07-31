from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# Базовая схема — снимок на момент введения миграций (версия 0).
# ВНИМАНИЕ: этот блок больше не редактируем — любые изменения схемы
# делаются только новыми миграциями в MIGRATIONS ниже, чтобы существующие
# data.db пользователей доезжали до актуальной схемы автоматически.
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    filename    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    progress    REAL NOT NULL DEFAULT 0,
    stage       TEXT NOT NULL DEFAULT '',
    error       TEXT,
    settings_json TEXT NOT NULL DEFAULT '{}',
    output_dir  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    data    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS templates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    prompt_body  TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS analyses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL,
    label           TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    prompt_snapshot TEXT NOT NULL,
    model           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'queued',
    stage           TEXT NOT NULL DEFAULT '',
    progress        REAL NOT NULL DEFAULT 0,
    chunks          INTEGER NOT NULL DEFAULT 1,
    result_md       TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_analyses_job ON analyses(job_id, id DESC);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Создаёт базовую схему (для новых БД) и догоняет миграции до актуальной версии."""
    conn.executescript(SCHEMA)
    conn.commit()
    migrate(conn)


# Список миграций: (версия, описание, шаг). Правила:
# - миграции только добавляются в конец; выпущенную миграцию не редактировать
#   (у пользователей она уже применена — изменение не подхватится и создаст рассинхрон);
# - шаг по возможности идемпотентен (CREATE ... IF NOT EXISTS); для ADD COLUMN —
#   сначала проверка через PRAGMA table_info;
# - версия = PRAGMA user_version после применения; версии строго монотонны.
Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _m001_speaker_aliases(conn: sqlite3.Connection) -> None:
    # Алиасы и слияния спикеров: speaker — человеческая метка («Спикер 1»),
    # name — отображаемое имя, merged_into — метка спикера, с которым объединён.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS speaker_aliases (
            job_id      INTEGER NOT NULL,
            speaker     TEXT NOT NULL,
            name        TEXT NOT NULL DEFAULT '',
            merged_into TEXT,
            PRIMARY KEY (job_id, speaker)
        )""")


def _m002_transcripts_fts(conn: sqlite3.Connection) -> None:
    # Транскрипты в БД (источник истины; output/*.json — best-effort копия) и
    # полнотекстовый индекс по репликам и анализам. Сегменты храним без words:
    # детализация по словам нужна только файлу на диске, а в БД раздувала бы
    # каждую встречу в разы.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transcripts (
            job_id        INTEGER PRIMARY KEY,
            language      TEXT NOT NULL DEFAULT '',
            duration      REAL NOT NULL DEFAULT 0,
            model         TEXT NOT NULL DEFAULT '',
            diarized      INTEGER NOT NULL DEFAULT 0,
            segments_json TEXT NOT NULL
        )""")
    try:
        # kind: 'replica' (ref_id — номер реплики, start — таймкод) или
        # 'analysis' (ref_id — id анализа). speaker/text — индексируемые поля.
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                kind UNINDEXED, job_id UNINDEXED, ref_id UNINDEXED,
                start UNINDEXED, speaker, text,
                tokenize='unicode61'
            )""")
    except sqlite3.OperationalError:
        # FTS5 не собран в этом SQLite (экзотическая сборка Python) — поиск
        # деградирует на LIKE-перебор, остальное работает как обычно.
        log.warning("FTS5 недоступен в этой сборке SQLite — "
                    "поиск будет работать в режиме LIKE")


def _m003_jobs_processed_path(conn: sqlite3.Connection) -> None:
    # Фактическое расположение исходника после move_to_processed: source_path
    # в inbox после успешной расшифровки мёртв, а обратиться к файлу нужно
    # (тултип в UI, эндпоинт /media для плеера).
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
    if "processed_path" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN processed_path TEXT")


def _m004_analyses_edited(conn: sqlite3.Connection) -> None:
    # Пометка «версия анализа изменена вручную» (план 6a): снимок защищает от
    # случайных изменений, а осознанная правка пользователя — легальна, но
    # должна быть видна (и перегенерация её должна предупреждать).
    cols = [r[1] for r in conn.execute("PRAGMA table_info(analyses)")]
    if "edited" not in cols:
        conn.execute("ALTER TABLE analyses ADD COLUMN edited INTEGER NOT NULL DEFAULT 0")


MIGRATIONS: list[Migration] = [
    (1, "таблица speaker_aliases — имена и объединения спикеров", _m001_speaker_aliases),
    (2, "transcripts в БД + FTS5-индекс поиска", _m002_transcripts_fts),
    (3, "jobs.processed_path — фактическое расположение исходника", _m003_jobs_processed_path),
    (4, "analyses.edited — пометка ручной правки версии", _m004_analyses_edited),
]

SCHEMA_VERSION = MIGRATIONS[-1][0] if MIGRATIONS else 0


def migrate(conn: sqlite3.Connection) -> None:
    """Применяет неприменённые миграции. Каждая — в своей транзакции:
    сбой шага откатывает его, версия не повышается, уже применённые не теряются."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, description, step in MIGRATIONS:
        if version <= current:
            continue
        try:
            # Явный BEGIN: в legacy-режиме sqlite3 DDL-операторы (CREATE TABLE)
            # сами по себе транзакцию не открывают, и без BEGIN откатить
            # сорвавшуюся миграцию было бы нечего.
            conn.execute("BEGIN")
            step(conn)
            # user_version не параметризуется — только f-string с int
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        log.info("Миграция БД %d применена: %s", version, description)
        current = version
