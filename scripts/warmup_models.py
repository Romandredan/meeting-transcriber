"""Предзагрузка моделей в кэш — вызывается установщиком (install.ps1).

Зачем: первая расшифровка иначе молча качает ~3 ГБ (Whisper) и ещё веса
pyannote, а пользователь в это время смотрит на «зависший» прогресс 10%.
Установщик прогоняет этот скрипт заранее, с понятным выводом и ошибками.

Имена моделей переопределяются в .env: WHISPER_MODEL (по умолчанию
large-v3-turbo) и PYANNOTE_PIPELINE (по умолчанию
pyannote/speaker-diarization-community-1, см. app/diarize.py).

Использование:
    python scripts/warmup_models.py            # всё
    python scripts/warmup_models.py whisper    # только Whisper
    python scripts/warmup_models.py pyannote   # только веса диаризации

Коды возврата: 0 — успех (или осознанный пропуск); 1 — ошибка скачивания
(читаемый текст на русском уходит в stderr, установщик покажет его).
"""

from __future__ import annotations

import os
import sys

# Скрипт запускают из корня проекта (install.ps1 так и делает); добавляем корень
# в sys.path на случай запуска из другой директории.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402  (после правки sys.path)


def warmup_whisper(model_name: str | None = None) -> str:
    """Скачивает Whisper-модель в MODELS_DIR. Возвращает имя модели.

    device="cpu": веса поднимаются в ОЗУ только ради скачивания, видеокарта не
    трогается — установщик может идти и на машине без GPU вовсе."""
    from faster_whisper import WhisperModel

    name = model_name or config.WHISPER_MODEL
    WhisperModel(name, device="cpu", compute_type="int8",
                 download_root=str(config.MODELS_DIR))
    return name


def warmup_pyannote(hf_token: str | None = None) -> str | None:
    """Скачивает веса pyannote при наличии токена. Без токена — пропуск (None).

    from_pretrained подтягивает и сам пайплайн, и зависимые веса (сегментация,
    эмбеддинги) в тот же кэш, которым пользуется рантайм, — snapshot_download
    одного репозитория пайплайна для этого недостаточно."""
    token = hf_token or config.HF_TOKEN
    if not token:
        return None
    from pyannote.audio import Pipeline

    model_id = os.environ.get("PYANNOTE_PIPELINE", "pyannote/speaker-diarization-community-1")
    try:
        Pipeline.from_pretrained(model_id, token=token)  # pyannote >= 4.x
    except TypeError:
        Pipeline.from_pretrained(model_id, use_auth_token=token)  # < 4.x
    return model_id


def main(argv: list[str]) -> int:
    which = argv[1] if len(argv) > 1 else "all"
    if which not in ("all", "whisper", "pyannote"):
        print(f"Неизвестный аргумент «{which}» — ожидается whisper, pyannote или все сразу (без аргумента)",
              file=sys.stderr)
        return 1
    try:
        if which in ("all", "whisper"):
            name = warmup_whisper()
            print(f"Модель распознавания «{name}» скачана (кэш: {config.MODELS_DIR})")
        if which in ("all", "pyannote"):
            model_id = warmup_pyannote()
            if model_id:
                print(f"Модель диаризации «{model_id}» скачана")
            else:
                print("HF_TOKEN не задан — веса диаризации пропущены: "
                      "расшифровка будет без разделения на спикеров")
    except Exception as e:
        # Текст читает и пользователь (в окне установщика), и поддержка (в логе).
        print(f"Не удалось скачать модели: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
