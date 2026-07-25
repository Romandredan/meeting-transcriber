from __future__ import annotations

import re

from app import config
from app.models import TranscriptResult
from app.stitch import humanize_speaker, merge_speaker_runs

# Эмпирика замера 2026-06-24: 32 471 символ → 13 283 токена (2,44 симв./токен для
# русского). Берём 2,4 с запасом — недооценка окна безопаснее переоценки.
CHARS_PER_TOKEN = 2.4
OUTPUT_RESERVE_TOKENS = 1000        # место под ответ модели
MIN_TRANSCRIPT_CHARS = 200          # короче — анализировать нечего
OVERLAP_REPLICAS = 2                # перекрытие соседних чанков

_PREFIX_RE = re.compile(r"^(\[\d{2,}:\d{2}\] (?:[^:]+: )?)")
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")


class AnalyzeError(RuntimeError):
    """Отказ конвейера с текстом, который можно показать пользователю."""


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def _mmss(seconds: float) -> str:
    """MM:SS от начала записи; минуты не переполняются в часы — для трёхчасовой
    встречи получится [178:24], и это читается лучше, чем усечённое время."""
    total = int(max(0.0, seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def transcript_replicas(result: TranscriptResult) -> list[str]:
    """Транскрипт в виде строк '[MM:SS] Спикер 1: текст'.

    Склейка — та же самая (stitch.merge_speaker_runs с config.MERGE_MAX_SECONDS),
    что идёт в TXT. Второго формата текста не заводим: иначе анализ и протокол
    со временем разъедутся. Таймкоды оставляем — они дают модели возможность
    сослаться на момент, а стоят немного."""
    segments = (merge_speaker_runs(result.segments, max_seconds=config.MERGE_MAX_SECONDS)
                if result.diarized else result.segments)
    lines = []
    for seg in segments:
        prefix = f"{humanize_speaker(seg.speaker)}: " if result.diarized else ""
        lines.append(f"[{_mmss(seg.start)}] {prefix}{seg.text.strip()}")
    return lines


def split_long_replica(replica: str, max_tokens: int) -> list[str]:
    """Режет сверхдлинную реплику по границам предложений, сохраняя на каждом куске
    таймкод и имя спикера (иначе продолжение монолога теряет автора).

    Нужно только при MERGE_MAX_SECONDS=0: с дефолтом 90 с блок ≈ 600 токенов и в
    чанк влезает всегда. Одиночное предложение длиннее чанка остаётся как есть —
    такого в человеческой речи не бывает."""
    m = _PREFIX_RE.match(replica)
    prefix = m.group(1) if m else ""
    body = replica[len(prefix):]
    pieces: list[str] = []
    cur = ""
    for sentence in _SENTENCE_RE.split(body):
        candidate = f"{cur} {sentence}".strip() if cur else sentence
        if cur and estimate_tokens(prefix + candidate) > max_tokens:
            pieces.append(prefix + cur)
            cur = sentence
        else:
            cur = candidate
    if cur:
        pieces.append(prefix + cur)
    return pieces or [replica]


def chunk_replicas(replicas: list[str], max_tokens: int,
                   overlap: int = OVERLAP_REPLICAS) -> list[list[str]]:
    """Нарезает список реплик на чанки по границам реплик, с перекрытием.

    Перекрытие в две реплики нужно для фактов, размазанных по стыку: «— Сделаешь
    до пятницы? / — Да, сделаю» без него теряется в обоих фрагментах — в первом
    нет ответа, во втором нет вопроса. Дублирование тезисов безвредно: REDUCE их
    схлопнет."""
    items: list[str] = []
    for r in replicas:
        items.extend(split_long_replica(r, max_tokens)
                     if estimate_tokens(r) > max_tokens else [r])

    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_tokens = 0
    for item in items:
        t = estimate_tokens(item)
        if cur and cur_tokens + t > max_tokens:
            chunks.append(cur)
            cur = list(cur[-overlap:]) if overlap else []
            cur_tokens = sum(estimate_tokens(x) for x in cur)
            # Хвост-перекрытие не должен съесть весь бюджет нового чанка.
            while cur and cur_tokens + t > max_tokens:
                cur.pop(0)
                cur_tokens = sum(estimate_tokens(x) for x in cur)
        cur.append(item)
        cur_tokens += t
    if cur:
        chunks.append(cur)
    return chunks
