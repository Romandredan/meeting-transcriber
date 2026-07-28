from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time

from app import analyses, analyze, config, ffmpeg_tool, job_queue, writers
from app.models import Settings, TranscriptResult

_log = logging.getLogger(__name__)


def merge_settings(global_s: Settings, override: dict) -> Settings:
    base = global_s.to_dict()
    base.update({k: v for k, v in (override or {}).items() if v is not None})
    return Settings.from_dict(base)


def move_to_processed(source_path: str, inbox_dir: str | None,
                      processed_dir: str | None) -> str | None:
    """Переносит исходник из inbox в processed, сохраняя относительный путь (подпапки).

    Файлы ВНЕ inbox (добавленные по пути из UI — это оригиналы пользователя в других
    папках) не трогаем. Best-effort: при блокировке/ошибке не валим успешный job.
    Возвращает путь назначения или None, если перенос не делался/не удался.
    """
    if not inbox_dir or not processed_dir:
        return None
    try:
        src = os.path.abspath(source_path)
        inbox = os.path.abspath(inbox_dir)
        rel = os.path.relpath(src, inbox)
        if rel.startswith("..") or os.path.isabs(rel):
            return None  # источник не внутри inbox — не наш файл
        dst = os.path.join(processed_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):  # не перезаписываем — добавляем суффикс
            base, ext = os.path.splitext(dst)
            i = 1
            while os.path.exists(f"{base}.{i}{ext}"):
                i += 1
            dst = f"{base}.{i}{ext}"
        shutil.move(src, dst)
        return dst
    except Exception:
        return None


# Типовые сбои железа/сети → понятный совет пользователю. Исходный текст ошибки
# всегда сохраняется в скобках — его пришлют в поддержку, перевод не должен
# съедать диагностику.
_ERROR_HINTS = [
    ("out of memory",
     "Не хватило видеопамяти. Закройте игры, браузер и другие программы, "
     "нагружающие видеокарту, и нажмите «Повторить». Если не помогло — выберите "
     "в настройках модель меньше (например, large-v3-turbo вместо large-v3)."),
    ("cudnn",
     "Сбой библиотеки cuDNN (часть драйвера NVIDIA). Обновите драйвер "
     "видеокарты с сайта nvidia.ru и перезагрузите компьютер."),
    ("cuda driver",
     "Драйвер NVIDIA не отвечает. Обновите драйвер и перезагрузите компьютер."),
    ("connection", "Не удалось скачать модель распознавания. Проверьте "
     "интернет-соединение и нажмите «Повторить» — скачивание продолжится."),
    ("timed out", "Сеть отвечает слишком медленно при скачивании модели. "
     "Проверьте интернет и нажмите «Повторить»."),
]


def describe_error(e: Exception) -> str:
    """Текст ошибки для строки в интерфейсе: совет на русском + оригинал.

    Сырой вид «RuntimeError: CUDA out of memory» пользователю ни о чём не
    скажет, а поддержке нужен именно он — поэтому подсказка ДОБАВЛЯЕТСЯ
    к оригиналу, а не заменяет его."""
    raw = f"{type(e).__name__}: {e}"
    low = raw.lower()
    for needle, hint in _ERROR_HINTS:
        if needle in low:
            return f"{hint} (подробности: {raw})"
    return raw


def process_job(conn, broker, engine, job_row, *, settings_global: Settings,
                formats: list[str], tmp_dir: str, output_dir: str,
                inbox_dir: str | None = None, processed_dir: str | None = None) -> None:
    job_id = job_row["id"]
    override = json.loads(job_row["settings_json"] or "{}")
    settings = merge_settings(settings_global, override)
    os.makedirs(tmp_dir, exist_ok=True)
    job_out = os.path.join(output_dir, str(job_id))

    def report(stage: str, progress: float) -> None:
        # Отмену проверяем здесь: report — единственная точка, которую движок
        # дёргает по ходу расшифровки. Отмена срабатывает на ближайшей стадии
        # (audio → transcribe → diarize → write), то есть не мгновенно: рвать
        # инференс посреди файла нечем, а ждать конца стадии честнее, чем
        # показывать «отменено» и продолжать жечь GPU.
        if job_queue.cancel_requested(job_id):
            raise job_queue.JobCancelled()
        job_queue.update(conn, job_id, stage=stage, progress=progress)
        broker.publish(job_id, stage, progress, "processing")

    try:
        report("audio", 0.02)
        wav = os.path.join(tmp_dir, f"{job_id}.wav")
        ffmpeg_tool.extract_audio(job_row["source_path"], wav)

        result = engine.transcribe(wav, settings, report)

        report("write", 0.95)
        basename = os.path.splitext(job_row["filename"])[0]
        writers.write_all(result, job_out, formats, basename)

        job_queue.update(conn, job_id, status="done", progress=1.0,
                         stage="write", output_dir=job_out)
        broker.publish(job_id, "write", 1.0, "done")
        # Успех: уносим исходник из inbox в processed, чтобы не транскрибировать повторно.
        move_to_processed(job_row["source_path"], inbox_dir, processed_dir)
    except job_queue.JobCancelled:
        job_queue.update(conn, job_id, status="cancelled", stage="", progress=0.0, error="")
        broker.publish(job_id, "", 0.0, "cancelled")
    except Exception as e:
        job_queue.update(conn, job_id, status="error", error=describe_error(e))
        broker.publish(job_id, "", 0.0, "error")
    finally:
        job_queue.clear_cancel(job_id)   # флаг не должен переехать на повтор
        try:
            if os.path.exists(wav):
                os.remove(wav)
        except Exception:
            pass


def process_analysis(conn, broker, engine, provider, row, *, settings_global: Settings,
                     output_dir: str) -> None:
    """Один анализ: транскрипт с диска → LLM → markdown в БД и на диск.

    Порядок выгрузок обязателен: Whisper уходит из VRAM ДО первого вызова LLM
    (14B рядом с large-v3 не помещается), Ollama освобождает карту ПОСЛЕ."""
    analysis_id = row["id"]
    job_id = row["job_id"]

    def report(stage: str, progress: float) -> None:
        analyses.update(conn, analysis_id, stage=stage, progress=progress)
        broker.publish(job_id, stage, progress, "processing", analysis_id=analysis_id)

    try:
        job = job_queue.get(conn, job_id)
        if job is None:
            raise analyze.AnalyzeError("встреча не найдена")
        job_out = job["output_dir"] or os.path.join(output_dir, str(job_id))
        basename = os.path.splitext(job["filename"])[0]
        json_path = os.path.join(job_out, f"{basename}.json")
        if not os.path.isfile(json_path):
            raise analyze.AnalyzeError(f"нет файла транскрипта: {json_path}")
        with open(json_path, encoding="utf-8") as f:
            raw = f.read()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            # Отдельный перехват: json.JSONDecodeError.__str__ — английский текст
            # ("Expecting value: line 1 column 1..."), а тексты ошибок в проекте
            # по-русски и должны быть понятны не разработчику, а пользователю.
            raise analyze.AnalyzeError(
                f"файл транскрипта повреждён: {json_path} — расшифруйте встречу заново"
            ) from e
        result = TranscriptResult.from_dict(data)

        report("unload", 0.02)
        engine.unload()   # освобождаем VRAM под LLM

        md = analyze.run_analysis(provider, result, row["prompt_snapshot"],
                                  num_ctx=provider.num_ctx,
                                  vocabulary=settings_global.vocabulary,
                                  report=report)

        # БД — источник истины: результат должен сохраниться, даже если запись
        # файла на диск не удастся (нет прав, диск полон, файл занят и т.п.).
        # Иначе минуты работы GPU выбрасываются из-за постороннего сбоя ввода-вывода.
        analyses.update(conn, analysis_id, status="done", progress=1.0,
                        stage="reduce", result_md=md)
        broker.publish(job_id, "reduce", 1.0, "done", analysis_id=analysis_id)

        # На диске — последняя версия по метке (удобство); история версий живёт
        # в БД. Best-effort, как move_to_processed и provider.unload() ниже.
        try:
            os.makedirs(job_out, exist_ok=True)
            with open(os.path.join(job_out, f"{basename}.{row['label']}.md"),
                      "w", encoding="utf-8") as f:
                f.write(md)
        except Exception as e:
            _log.warning("Не удалось записать .md на диск для анализа %s: %s",
                         analysis_id, e)
    except Exception as e:
        text = str(e) if isinstance(e, analyze.AnalyzeError) else f"{type(e).__name__}: {e}"
        analyses.update(conn, analysis_id, status="error", error=text)
        broker.publish(job_id, "", 0.0, "error", analysis_id=analysis_id)
    finally:
        try:
            provider.unload()   # keep_alive=0: карта свободна под следующий Whisper
        except Exception:
            pass


class Worker:
    def __init__(self, conn, broker, engine, settings_provider, formats_provider,
                 tmp_dir: str, output_dir: str,
                 inbox_dir: str | None = None, processed_dir: str | None = None,
                 provider=None) -> None:
        self.conn = conn
        self.broker = broker
        self.engine = engine
        self.settings_provider = settings_provider
        self.formats_provider = formats_provider
        self.tmp_dir = tmp_dir
        self.output_dir = output_dir
        self.inbox_dir = inbox_dir
        self.processed_dir = processed_dir
        # provider=None — стадия analyze выключена (ANALYZE_ENABLED=false):
        # очередь анализов не трогаем вовсе, Ollama не нужна.
        self.provider = provider
        self._thread: threading.Thread | None = None
        # Простой отсчитывается с момента запуска воркера (нет работы — нет и
        # активности) и переустанавливается каждый раз, когда очередная работа
        # завершена и обе очереди снова проверены (см. run_forever).
        self._last_active = time.monotonic()
        self._idle_unloaded = False

    def idle_tick(self, now: float) -> bool:
        """Выгружает все модели из VRAM, если простой затянулся дольше
        config.IDLE_UNLOAD_SECONDS. Возвращает True, если в этот вызов выгрузка
        произошла.

        Не чаще одного раза за простой (флаг сбрасывается, когда воркер берёт
        следующую работу) — иначе выгрузка дёргалась бы на каждом тике впустую.
        Обе выгрузки best-effort: сбой одной не должен ронять воркер и не мешает
        второй."""
        if config.IDLE_UNLOAD_SECONDS <= 0 or self._idle_unloaded:
            return False
        if now - self._last_active < config.IDLE_UNLOAD_SECONDS:
            return False
        try:
            self.engine.unload()
        except Exception:
            pass
        if self.provider is not None:
            try:
                self.provider.unload()
            except Exception:
                pass
        self._idle_unloaded = True
        return True

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            row = job_queue.claim_next(self.conn)
            if row is not None:
                self._idle_unloaded = False
                process_job(self.conn, self.broker, self.engine, row,
                            settings_global=self.settings_provider(),
                            formats=self.formats_provider(),
                            tmp_dir=self.tmp_dir, output_dir=self.output_dir,
                            inbox_dir=self.inbox_dir, processed_dir=self.processed_dir)
                # Простой отсчитывается с момента, когда очередь ОПУСТЕЛА, а не с
                # момента, когда работа была взята: расшифровка часового видео
                # длится дольше порога простоя сама по себе, и если отметить
                # активность на старте, воркер выгрузит модель немедленно вслед
                # за только что законченной работой — ровно тогда, когда следующий
                # файл вероятнее всего появится через минуту-другую.
                self._last_active = time.monotonic()
                continue
            # Транскрибация в приоритете: за анализ беремся только когда очередь
            # jobs пуста — GPU один, и ждать расшифровки хуже, чем анализа.
            arow = analyses.claim_next(self.conn) if self.provider is not None else None
            if arow is not None:
                self._idle_unloaded = False
                process_analysis(self.conn, self.broker, self.engine, self.provider, arow,
                                 settings_global=self.settings_provider(),
                                 output_dir=self.output_dir)
                self._last_active = time.monotonic()
                continue
            self.idle_tick(time.monotonic())
            time.sleep(1.0)

    def start(self, stop_event: threading.Event) -> None:
        self._thread = threading.Thread(target=self.run_forever, args=(stop_event,), daemon=True)
        self._thread.start()
