from __future__ import annotations

import sqlite3

# Дефолтные шаблоны сидятся ОДИН раз в пустую таблицу и больше кодом не трогаются.
# Правки пользователя неприкосновенны; новые дефолты из будущих версий заводятся вручную.
DEFAULT_TEMPLATES: list[dict] = [
    {
        "label": "protocol",
        "display_name": "Протокол встречи",
        "description": "Формальный протокол: участники, обсуждение, решения, задачи.",
        "prompt_body": (
            "Составь протокол встречи по расшифровке ниже.\n\n"
            "Структура (заголовки — ровно такие):\n"
            "## Участники\n"
            "## Повестка\n"
            "## Обсуждение\n"
            "По темам, в каждой — суть и таймкод ключевого момента.\n"
            "## Решения\n"
            "## Задачи\n"
            "Таблицей: | Задача | Ответственный | Срок |\n"
            "## Открытые вопросы\n\n"
            "Если раздел нечем заполнить — напиши «не обсуждалось». "
            "Ответственного и срок указывай только если они прозвучали.\n\n"
            "Расшифровка:"
        ),
    },
    {
        "label": "requirements",
        "display_name": "Требования",
        "description": "Требования к продукту или доработке, проговорённые на встрече.",
        "prompt_body": (
            "Собери из расшифровки ниже требования к продукту или доработке.\n\n"
            "Структура (заголовки — ровно такие):\n"
            "## Контекст\n"
            "## Функциональные требования\n"
            "Нумерованный список; каждое требование — одним проверяемым предложением.\n"
            "## Ограничения и нефункциональные требования\n"
            "## Сроки и приоритеты\n"
            "## Что осталось невыясненным\n\n"
            "Не придумывай требований, которых в разговоре не было. Если формулировка "
            "прозвучала расплывчато — вынеси её в «Что осталось невыясненным», "
            "а не додумывай.\n\n"
            "Расшифровка:"
        ),
    },
    {
        "label": "summary",
        "display_name": "Краткое резюме",
        "description": "Короткая выжимка для тех, кто не был на встрече.",
        "prompt_body": (
            "Сделай краткое резюме встречи по расшифровке ниже — для человека, "
            "которого на ней не было.\n\n"
            "Структура (заголовки — ровно такие):\n"
            "## О чём была встреча\n"
            "Два-три предложения.\n"
            "## Главное\n"
            "Пять-семь пунктов.\n"
            "## Решения\n"
            "## Что делать дальше\n\n"
            "Пиши по-деловому и коротко, без воды и без пересказа реплик подряд.\n\n"
            "Расшифровка:"
        ),
    },
]


def seed_defaults(conn: sqlite3.Connection) -> int:
    """Вставляет дефолты, ЕСЛИ таблица пуста. Иначе не делает ничего — никогда."""
    count = conn.execute("SELECT COUNT(*) AS c FROM templates").fetchone()["c"]
    if count:
        return 0
    for t in DEFAULT_TEMPLATES:
        conn.execute(
            "INSERT INTO templates (label, display_name, description, prompt_body) "
            "VALUES (?, ?, ?, ?)",
            (t["label"], t["display_name"], t["description"], t["prompt_body"]),
        )
    conn.commit()
    return len(DEFAULT_TEMPLATES)


def list_templates(conn: sqlite3.Connection, enabled_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM templates"
    if enabled_only:
        sql += " WHERE enabled=1"
    return conn.execute(sql + " ORDER BY id").fetchall()


def get(conn: sqlite3.Connection, template_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()


def get_by_label(conn: sqlite3.Connection, label: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM templates WHERE label=?", (label,)).fetchone()


def create(conn: sqlite3.Connection, label: str, display_name: str, description: str,
           prompt_body: str, enabled: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO templates (label, display_name, description, prompt_body, enabled) "
        "VALUES (?, ?, ?, ?, ?)",
        (label, display_name, description, prompt_body, 1 if enabled else 0),
    )
    conn.commit()
    return int(cur.lastrowid)


def update(conn: sqlite3.Connection, template_id: int, *, label=None, display_name=None,
           description=None, prompt_body=None, enabled=None) -> None:
    fields, values = [], []
    for name, val in (("label", label), ("display_name", display_name),
                      ("description", description), ("prompt_body", prompt_body)):
        if val is not None:
            fields.append(f"{name}=?")
            values.append(val)
    if enabled is not None:
        fields.append("enabled=?")
        values.append(1 if enabled else 0)
    if not fields:
        return
    values.append(template_id)
    conn.execute(f"UPDATE templates SET {', '.join(fields)} WHERE id=?", values)
    conn.commit()


def delete(conn: sqlite3.Connection, template_id: int) -> None:
    conn.execute("DELETE FROM templates WHERE id=?", (template_id,))
    conn.commit()
