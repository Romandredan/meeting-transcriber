from app import analyze
from app.models import Segment, TranscriptResult, Word


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
