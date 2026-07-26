import pytest

from app import analyze
from app.models import Segment, TranscriptResult, Word
from fakes import FakeProvider


def result_from(pairs, diarized=True):
    """pairs: [(start, speaker, text), ...] → TranscriptResult.

    Спикеров в тестах чередуем: подряд идущие реплики ОДНОГО спикера склеиваются
    merge_speaker_runs в одну — тогда чанков не получится, сколько текста ни дай."""
    segs = [Segment(start=s, end=s + 1.0, text=t, speaker=sp,
                    words=[Word(s, s + 1.0, t, speaker=sp)])
            for s, sp, t in pairs]
    return TranscriptResult("ru", segs[-1].end if segs else 0.0, "large-v3", diarized, segs)


def test_estimate_tokens_uses_russian_ratio():
    """2,4 символа на токен — эмпирика замера на реальных транскриптах.

    Точное равенство не проверяем: 240/2.4 в двоичной плавающей точке даёт
    100.00000000000001, и тест на «== 101» держится на разряде, который к делу
    отношения не имеет. Важно другое — оценка не должна ЗАНИЖАТЬ размер."""
    assert analyze.estimate_tokens("") == 1
    assert 99 <= analyze.estimate_tokens("a" * 240) <= 102
    # Замер 2026-06-24: 32 471 символ дали 13 283 токена.
    assert analyze.estimate_tokens("a" * 32471) >= 13283


def test_fold_prompt_guarantee_covers_names():
    """Гарантия сохранения в FOLD_PROMPT перечисляет факты/решения/задачи/вопросы,
    но не имена участников — ровно то, ради чего заведён глоссарий, и терять их
    на свёртке обиднее всего. '"имена" in FOLD_PROMPT' был бы ложно-зелёным: слово
    уже встречается в списке пунктов выше по тексту — проверяем именно гарантийное
    предложение, начинающееся с «Сохрани»."""
    start = analyze.FOLD_PROMPT.index("Сохрани")
    end = analyze.FOLD_PROMPT.index(".", start)
    guarantee = analyze.FOLD_PROMPT[start:end]
    assert "имена" in guarantee


def test_transcript_replicas_format_and_merge():
    """Формат '[MM:SS] Спикер N: текст'; подряд идущие реплики одного спикера склеены
    той же функцией, что и в TXT, — иначе анализ и протокол разъедутся."""
    r = result_from([(0.0, "SPEAKER_00", "раз"),
                     (1.0, "SPEAKER_00", "два"),
                     (65.0, "SPEAKER_01", "три")])
    lines = analyze.transcript_replicas(r)
    assert lines[0] == "[00:00] Спикер 1: раз два"
    assert lines[1] == "[01:05] Спикер 2: три"


def test_transcript_replicas_without_diarization_omit_speaker():
    r = result_from([(0.0, None, "привет")], diarized=False)
    assert analyze.transcript_replicas(r) == ["[00:00] привет"]


def test_chunking_keeps_replica_boundaries():
    """Реплика не режется пополам: граница чанка проходит между репликами."""
    reps = [f"[00:0{i}] Спикер 1: {'я' * 100}" for i in range(5)]
    chunks = analyze.chunk_replicas(reps, max_tokens=100, overlap=0)
    assert len(chunks) > 1
    for ch in chunks:
        for line in ch:
            assert line in reps


def test_chunks_overlap_by_two_replicas():
    """Факт на стыке («— Сделаешь до пятницы? / — Да») теряется в обоих кусках,
    если их не перекрыть."""
    # Реплика ≈ 50 токенов, в чанк влезает 5 — хвосту из двух есть где поместиться.
    reps = [f"[00:0{i}] Спикер 1: {'я' * 100}" for i in range(6)]
    chunks = analyze.chunk_replicas(reps, max_tokens=260, overlap=2)
    assert len(chunks) == 2
    assert chunks[1][:2] == chunks[0][-2:]


def test_chunking_never_exceeds_budget():
    reps = [f"[00:0{i}] Спикер 1: {'я' * 60}" for i in range(10)]
    for ch in analyze.chunk_replicas(reps, max_tokens=120, overlap=2):
        assert analyze.estimate_tokens("\n".join(ch)) <= 120 * 1.2


def test_split_long_replica_on_sentence_boundaries():
    """MERGE_MAX_SECONDS=0 отключает нарезку монолога — тогда блок режется
    по границам предложений, а не посреди слова."""
    body = "Первое предложение. Второе предложение! Третье предложение? Четвёртое."
    replica = "[00:00] Спикер 1: " + body
    pieces = analyze.split_long_replica(replica, max_tokens=20)
    assert len(pieces) > 1
    for p in pieces:
        assert p.startswith("[00:00] Спикер 1: ")
    joined = " ".join(p.replace("[00:00] Спикер 1: ", "") for p in pieces)
    assert "Первое предложение." in joined
    assert "Четвёртое." in joined


def test_chunk_replicas_pre_splits_oversized_replica():
    long_one = "[00:00] Спикер 1: " + "Фраза раз. " * 200
    chunks = analyze.chunk_replicas([long_one], max_tokens=100, overlap=0)
    assert len(chunks) > 1


def test_split_long_replica_non_diarized_does_not_swallow_colon_into_prefix():
    """Без диаризации префикс — только таймкод. Оборот вида «Итого: ...» в начале
    тела реплики не должен приниматься за имя спикера и дублироваться на каждом
    куске монолога (ровно так отдаёт текст TransformersWhisperEngine при
    diarize=false — одним недиаризованным сегментом)."""
    body = ("Итого: было решено сделать раз. Второе решение принято тоже! "
            "Третье решение таково? Четвёртое решение окончательное.")
    replica = "[00:00] " + body
    pieces = analyze.split_long_replica(replica, max_tokens=20, diarized=False)
    assert len(pieces) > 1
    for p in pieces:
        assert p.startswith("[00:00] ")
    assert pieces[0].startswith("[00:00] Итого:")
    for p in pieces[1:]:
        assert "Итого:" not in p


def test_chunk_replicas_diarized_false_does_not_duplicate_false_prefix():
    """То же самое, но через chunk_replicas: параметр diarized должен дойти до
    split_long_replica при предварительной нарезке сверхдлинной реплики."""
    long_one = "[00:00] Итого: " + "Решение принято. " * 200
    chunks = analyze.chunk_replicas([long_one], max_tokens=100, overlap=0, diarized=False)
    assert len(chunks) > 1
    total_occurrences = sum(line.count("Итого:") for ch in chunks for line in ch)
    assert total_occurrences == 1


def test_chunk_replicas_empty_input_returns_empty_list():
    assert analyze.chunk_replicas([], max_tokens=100) == []


def test_transcript_replicas_empty_segments_returns_empty_list():
    assert analyze.transcript_replicas(result_from([])) == []


def long_result(n_replicas=8, chars=400):
    """Транскрипт, который заведомо не влезает в маленькое окно.

    Спикеры чередуются: иначе merge_speaker_runs склеит всё в одну реплику."""
    return result_from([(float(i), f"SPEAKER_{i % 2:02d}", "я" * chars)
                        for i in range(n_replicas)])


def test_single_call_when_transcript_fits():
    p = FakeProvider(["# Протокол\nвсё хорошо"])
    out = analyze.run_analysis(p, long_result(3, 200), "тело шаблона", num_ctx=32768)
    assert out == "# Протокол\nвсё хорошо"
    assert len(p.calls) == 1
    assert "тело шаблона" in p.calls[0][1]


def test_map_reduce_when_transcript_does_not_fit():
    """Не влезло — чанки в MAP нейтральным промптом, затем один REDUCE шаблоном."""
    p = FakeProvider(default="заметка")
    out = analyze.run_analysis(p, long_result(8, 400), "тело шаблона", num_ctx=2000)
    assert out == "заметка"
    assert len(p.calls) > 2
    assert all(analyze.MAP_PROMPT[:40] in user for _, user in p.calls[:-1])
    assert "тело шаблона" in p.calls[-1][1]
    assert analyze.MAP_PROMPT[:40] not in p.calls[-1][1]


def test_glossary_from_vocabulary_reaches_system_prompt():
    """settings.vocabulary лечит «Битрикс24 → Bittrex 24» без новой сущности."""
    p = FakeProvider(["итог"])
    analyze.run_analysis(p, long_result(3, 200), "тело", num_ctx=32768,
                         vocabulary="АккордПост, ОФД")
    assert "АккордПост" in p.calls[0][0]


def test_empty_vocabulary_adds_no_glossary_section():
    p = FakeProvider(["итог"])
    analyze.run_analysis(p, long_result(3, 200), "тело", num_ctx=32768)
    assert "Правильные написания" not in p.calls[0][0]


def test_progress_reports_map_and_reduce_stages():
    seen = []
    analyze.run_analysis(FakeProvider(default="з"), long_result(8, 400), "тело",
                         num_ctx=2000, report=lambda s, p: seen.append(s))
    assert any(s.startswith("map 1/") for s in seen)
    assert "reduce" in seen


def test_notes_are_folded_recursively_until_they_fit():
    """Заметки MAP не влезли в REDUCE — сворачиваются ещё раз."""
    big = "з" * 2000     # ~834 токена: две таких заметки в бюджет не влезают
    p = FakeProvider([big, big, "коротко", "коротко", "# Итог"])
    out = analyze.run_analysis(p, long_result(8, 400), "тело", num_ctx=2000,
                               report=lambda s, pr: None)
    assert out == "# Итог"
    assert len(p.calls) == 5      # 2 map + 2 fold + 1 reduce


def test_fold_uses_fold_prompt_not_map_prompt():
    """Свёртка консолидирует уже извлечённые заметки — ей нужен свой промпт,
    который просит сжимать, а не MAP_PROMPT, который прямо запрещает сокращать."""
    big = "з" * 2000
    p = FakeProvider([big, big, "коротко", "коротко", "# Итог"])
    analyze.run_analysis(p, long_result(8, 400), "тело", num_ctx=2000)
    map_calls, fold_calls = p.calls[:2], p.calls[2:4]
    assert all(analyze.MAP_PROMPT[:40] in user for _, user in map_calls)
    assert all(analyze.FOLD_PROMPT[:40] in user for _, user in fold_calls)
    assert all(analyze.MAP_PROMPT[:40] not in user for _, user in fold_calls)


def test_fold_recurses_through_two_levels():
    """Первый проход свёртки уменьшает объём, но всё ещё не влезает в бюджет —
    должен пройти второй проход, а не свалиться в «не сходится»."""
    big = "з" * 2000
    mid = "с" * 1000     # короче исходных заметок, но вдвоём ещё не влезают
    p = FakeProvider([big, big, mid, mid, "коротко", "# Итог"])
    seen = []
    out = analyze.run_analysis(p, long_result(8, 400), "тело", num_ctx=2000,
                               report=lambda s, pr: seen.append(s))
    assert out == "# Итог"
    assert len(p.calls) == 6    # 2 map + 2 fold(уровень 1) + 1 fold(уровень 2) + 1 reduce
    assert any(s.startswith("fold 1") for s in seen)
    assert any(s.startswith("fold 2") for s in seen)


def test_fold_reports_its_own_stage():
    big = "з" * 2000
    seen = []
    p = FakeProvider([big, big, "коротко", "коротко", "# Итог"])
    analyze.run_analysis(p, long_result(8, 400), "тело", num_ctx=2000,
                         report=lambda s, pr: seen.append(s))
    assert any(s.startswith("fold 1") for s in seen)


def test_error_when_fold_does_not_converge():
    """Свёртка не уменьшает объём — останавливаемся, а не крутимся вечно."""
    p = FakeProvider(default="з" * 2000)
    with pytest.raises(analyze.AnalyzeError) as e:
        analyze.run_analysis(p, long_result(8, 400), "тело", num_ctx=2000)
    assert "не сходится" in str(e.value)


def test_failed_call_is_retried():
    p = FakeProvider([RuntimeError("оборвалось"), "# Протокол"])
    out = analyze.run_analysis(p, long_result(3, 200), "тело", num_ctx=32768)
    assert out == "# Протокол"
    assert len(p.calls) == 2


def test_blank_answer_counts_as_failure_and_is_retried():
    p = FakeProvider(["   ", "# Протокол"])
    assert analyze.run_analysis(p, long_result(3, 200), "тело", num_ctx=32768) == "# Протокол"


def test_whole_analysis_fails_after_retries_exhausted():
    """Частичный документ не собираем НИКОГДА: он выглядит как полноценный протокол,
    но молча теряет решения из пропущенного куска."""
    p = FakeProvider([RuntimeError("раз"), RuntimeError("два"), RuntimeError("три")])
    with pytest.raises(analyze.AnalyzeError) as e:
        analyze.run_analysis(p, long_result(3, 200), "тело", num_ctx=32768)
    assert "три" in str(e.value)
    assert len(p.calls) == analyze.MAX_ATTEMPTS


def test_error_on_too_short_transcript():
    p = FakeProvider()
    with pytest.raises(analyze.AnalyzeError) as e:
        analyze.run_analysis(p, result_from([(0.0, "SPEAKER_00", "ага")]), "тело",
                             num_ctx=32768)
    assert "слишком короткий" in str(e.value)
    assert p.calls == []
