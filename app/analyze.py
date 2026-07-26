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

# Два раздельных шаблона вместо одного «умного»: один общий регэксп с
# опциональной меткой спикера не может отличить «Спикер N: » от произвольного
# текста реплики с двоеточием (например, «Итого: ...») — при недиаризованном
# тексте это искажало бы монолог. Какой шаблон применить, решает вызывающий
# код (параметр diarized), а не угадывание по содержимому строки.
_TIMESTAMP_PREFIX_RE = re.compile(r"^(\[\d{2,}:\d{2}\] )")
_SPEAKER_PREFIX_RE = re.compile(r"^(\[\d{2,}:\d{2}\] [^:]+: )")
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


def split_long_replica(replica: str, max_tokens: int, diarized: bool = True) -> list[str]:
    """Режет сверхдлинную реплику по границам предложений, сохраняя на каждом куске
    таймкод и (если есть) имя спикера (иначе продолжение монолога теряет автора).

    Нужно только при MERGE_MAX_SECONDS=0: с дефолтом 90 с блок ≈ 600 токенов и в
    чанк влезает всегда. Одиночное предложение длиннее чанка остаётся как есть —
    такого в человеческой речи не бывает.

    `diarized` определяет, что считать префиксом: с диаризацией — таймкод плюс
    имя спикера, без неё — только таймкод. Так `TransformersWhisperEngine` при
    diarize=false отдаёт всю встречу одним сегментом без диаризации; текст этого
    монолога может начинаться с оборота вида «Итого: ...» — без явного diarized
    регэксп принял бы это «Итого:» за имя спикера и продублировал бы его на
    каждом куске монолога."""
    prefix_re = _SPEAKER_PREFIX_RE if diarized else _TIMESTAMP_PREFIX_RE
    m = prefix_re.match(replica)
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
                   overlap: int = OVERLAP_REPLICAS, diarized: bool = True) -> list[list[str]]:
    """Нарезает список реплик на чанки по границам реплик, с перекрытием.

    Перекрытие в две реплики нужно для фактов, размазанных по стыку: «— Сделаешь
    до пятницы? / — Да, сделаю» без него теряется в обоих фрагментах — в первом
    нет ответа, во втором нет вопроса. Дублирование тезисов безвредно: REDUCE их
    схлопнет.

    `diarized` (по умолчанию True — контракт вызовов не ломается) пробрасывается
    в split_long_replica при предварительной нарезке сверхдлинных реплик: без
    диаризации в префиксе не должно быть ничего, кроме таймкода."""
    items: list[str] = []
    for r in replicas:
        items.extend(split_long_replica(r, max_tokens, diarized=diarized)
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


MAX_ATTEMPTS = 3        # первая попытка + две повторные

SYSTEM_PROMPT = (
    "Ты превращаешь расшифровку рабочей встречи в структурированный документ.\n"
    "Опирайся только на то, что есть в расшифровке: ничего не додумывай и не "
    "дописывай выводов, которых не прозвучало. Чего в записи нет — так и пиши, "
    "а не выдумывай.\n"
    "Расшифровка автоматическая: имена, термины и цифры могут быть распознаны с "
    "ошибками, реплики иногда обрываются. Время в квадратных скобках — от начала "
    "записи.\n"
    "Отвечай по-русски, в Markdown, без вступлений вроде «Конечно» и без "
    "рассуждений о том, как ты работаешь."
)

MAP_PROMPT = (
    "Ниже — фрагмент расшифровки встречи. Выпиши из него тезисно, без вводных слов:\n"
    "- факты и цифры;\n"
    "- принятые решения;\n"
    "- задачи с ответственными и сроками;\n"
    "- открытые вопросы и разногласия;\n"
    "- имена участников и упомянутые названия.\n"
    "Только то, что есть во фрагменте. Не обобщай и не сокращай смысл — эти заметки "
    "пойдут на вход следующему шагу, а не человеку.\n\n"
    "Фрагмент:"
)

FOLD_PROMPT = (
    "Ниже — заметки, извлечённые на предыдущем шаге из нескольких фрагментов "
    "расшифровки встречи. Объедини их в один связный список того же вида:\n"
    "- факты и цифры;\n"
    "- принятые решения;\n"
    "- задачи с ответственными и сроками;\n"
    "- открытые вопросы и разногласия;\n"
    "- имена участников и упомянутые названия.\n"
    "Сохрани все факты, решения, задачи, вопросы и имена из заметок — ничего не "
    "выбрасывай. Но схлопни повторы: один и тот же пункт, попавший в несколько "
    "заметок из-за перекрытия фрагментов, оставь один раз. Убери воду, "
    "повторяющиеся формулировки и служебные вводные слова, изложи плотнее — "
    "результат должен получиться короче входа, потому что дальше он может "
    "пойти на ещё один такой же шаг свёртки.\n\n"
    "Заметки:"
)

GLOSSARY_HEADER = (
    "\n\nПравильные написания терминов и имён, звучавших на встрече. Если в "
    "расшифровке они искажены — используй эти написания:\n"
)


def build_system_prompt(vocabulary: str) -> str:
    """Системная часть + глоссарий из settings.vocabulary.

    Тот же словарь уже кормит initial_prompt Whisper; здесь он лечит остаточные
    искажения («Битрикс24 → Bittrex 24») без новой сущности в настройках."""
    vocabulary = (vocabulary or "").strip()
    if not vocabulary:
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + GLOSSARY_HEADER + vocabulary


def _call(provider, system: str, user: str) -> str:
    """Вызов модели с повторами. Пустой ответ считаем сбоем: единичный вырожденный
    ответ закрывается повтором, а systemic-отказ (демон упал) всё равно исчерпает
    попытки и уронит весь анализ — что и требуется."""
    last: Exception | None = None
    for _ in range(MAX_ATTEMPTS):
        try:
            out = provider.generate(system, user)
            if out and out.strip():
                return out.strip()
            last = RuntimeError("модель вернула пустой ответ")
        except AnalyzeError:
            raise
        except Exception as e:
            last = e
    raise AnalyzeError(f"вызов модели не удался после {MAX_ATTEMPTS} попыток: {last}")


def _total_tokens(notes: list[str]) -> int:
    return estimate_tokens("\n\n".join(notes))


def run_analysis(provider, result: TranscriptResult, prompt_body: str, *,
                 num_ctx: int, vocabulary: str = "", report=None) -> str:
    """Полный конвейер анализа. Возвращает markdown или бросает AnalyzeError.

    Частичных результатов не бывает: если хоть один вызов не прошёл после повторов,
    падает весь анализ. Документ, собранный без части встречи, выглядит как
    полноценный протокол, но молча теряет решения из пропущенного куска — пометка
    в шапке этого не лечит, потому что выводы уже искажены."""
    def say(stage: str, progress: float) -> None:
        if report is not None:
            report(stage, progress)

    replicas = transcript_replicas(result)
    text = "\n".join(replicas)
    if len(text) < MIN_TRANSCRIPT_CHARS:
        raise AnalyzeError("слишком короткий транскрипт для анализа")

    system = build_system_prompt(vocabulary)
    budget = (num_ctx - estimate_tokens(system) - estimate_tokens(prompt_body)
              - OUTPUT_RESERVE_TOKENS)
    if budget <= 0:
        raise AnalyzeError(
            f"окно контекста {num_ctx} токенов слишком мало для этого шаблона — "
            f"увеличьте LLM_NUM_CTX или укоротите промпт")

    # Влезает целиком — один вызов, самый точный вариант.
    if estimate_tokens(text) <= budget:
        say("analyze", 0.1)
        return _call(provider, system, f"{prompt_body}\n\n{text}")

    # MAP: чанки размером в половину окна — вход и заметки должны ужиться вместе.
    chunks = chunk_replicas(replicas, max_tokens=max(1, num_ctx // 2),
                            diarized=result.diarized)
    notes: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        say(f"map {i}/{len(chunks)}", 0.05 + 0.75 * i / len(chunks))
        notes.append(_call(provider, system, f"{MAP_PROMPT}\n\n" + "\n".join(chunk)))

    # Заметки не влезают в REDUCE — сворачиваем их рекурсивно до сходимости.
    # Жёсткого лимита глубины нет: любой транскрипт рано или поздно сворачивается.
    # Единственная защита — прогресс: проход, не уменьшивший объём, значит поломку.
    # Заметки — не реплики: у них нет ни таймкода, ни спикера, поэтому чанкинг
    # здесь идёт с diarized=False (не искать несуществующий префикс спикера) и
    # overlap=0 (перекрытие уже сделано на MAP, дублировать его незачем).
    level = 1
    while _total_tokens(notes) > budget:
        before = _total_tokens(notes)
        groups = chunk_replicas(notes, max_tokens=max(1, num_ctx // 2), overlap=0,
                                diarized=False)
        folded: list[str] = []
        for i, group in enumerate(groups, 1):
            # Коридор строго между концом MAP (0.8) и REDUCE (0.9) — растёт по
            # группам внутри уровня, а не стоит на месте, но никогда не достаёт
            # до 0.9, чтобы не выглядеть как reduce, пока свёртка ещё не готова.
            say(f"fold {level} · {i}/{len(groups)}", 0.8 + 0.09 * i / len(groups))
            folded.append(_call(provider, system, f"{FOLD_PROMPT}\n\n" + "\n\n".join(group)))
        if _total_tokens(folded) >= before:
            raise AnalyzeError(
                "свёртка не сходится: очередной проход не уменьшил объём заметок")
        notes = folded
        level += 1

    say("reduce", 0.9)
    return _call(provider, system, f"{prompt_body}\n\n" + "\n\n".join(notes))
