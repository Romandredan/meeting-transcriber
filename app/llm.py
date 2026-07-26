from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

from app import config


class LlmError(RuntimeError):
    """Ошибка обращения к Ollama с текстом, который можно показать пользователю."""


def _open(req: urllib.request.Request, timeout: float):
    """Единственная точка сетевого ввода-вывода — шов для тестов.

    timeout здесь СОКЕТНЫЙ: он ограничивает одну операцию чтения, а не весь ответ.
    Именно это даёт «таймаут по простою»: пока приходят токены, чтения успешны,
    сколько бы минут ответ ни занял целиком."""
    return urllib.request.urlopen(req, timeout=timeout)


class OllamaProvider:
    """Нативный API Ollama. OpenAI-совместимый эндпоинт не подходит: он не принимает
    num_ctx и keep_alive, а без них не управлять ни окном, ни освобождением VRAM."""

    def __init__(self, base_url: str, model: str, *, num_ctx: int,
                 temperature: float, idle_timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.idle_timeout = idle_timeout

    # --- внутреннее ---------------------------------------------------------

    def _request(self, path: str, payload: dict | None) -> urllib.request.Request:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        return urllib.request.Request(
            f"{self.base_url}{path}", data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )

    def _explain(self, exc: Exception) -> str:
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code == 404:
                return (f"модель `{self.model}` не установлена, "
                        f"выполните `ollama pull {self.model}`")
            # HTTPError — подкласс URLError, поэтому проверяем ДО общей ветки URLError:
            # иначе, например, 500 (неудачная загрузка модели) объяснялся бы как
            # «Ollama не отвечает» — ложный совет при живом, но упавшем демоне.
            return f"Ollama ответила ошибкой {exc.code}: {exc.reason}"
        if isinstance(exc, urllib.error.URLError):
            return f"Ollama не отвечает на {self.base_url} — запустите Ollama"
        return f"{type(exc).__name__}: {exc}"

    def _chat(self, messages: list[dict], keep_alive, timeout: float | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": keep_alive,
            "options": {"num_ctx": self.num_ctx, "temperature": self.temperature},
        }
        effective_timeout = self.idle_timeout if timeout is None else timeout
        try:
            stream = _open(self._request("/api/chat", payload), effective_timeout)
        except urllib.error.URLError as e:      # HTTPError — подкласс URLError
            raise LlmError(self._explain(e)) from e
        parts: list[str] = []
        finished = False  # обрыв соединения ДО этого флага — отказ, а не успех
        try:
            while True:
                try:
                    line = stream.readline()
                except socket.timeout as e:
                    raise LlmError(
                        f"Ollama не отвечает {effective_timeout:.0f} с — "
                        f"ни одного токена; возможно, модель зависла") from e
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                obj = json.loads(text)
                if obj.get("error"):
                    raise LlmError(f"Ollama вернула ошибку: {obj['error']}")
                parts.append(obj.get("message", {}).get("content", ""))
                if obj.get("done"):
                    finished = True
                    break
        finally:
            try:
                stream.close()
            except Exception:
                pass
        result = "".join(parts)
        if not finished:
            # Соединение оборвалось до "done": true — сервер упал, сеть разорвалась
            # и т.п. Частичный результат не отдаём никогда: он выглядит как
            # завершённый анализ, а на деле обрезан произвольно на токене.
            raise LlmError(
                f"Ollama оборвала ответ, не завершив его "
                f"(получено {len(result)} символов) — попробуйте ещё раз")
        return result

    # --- публичное ----------------------------------------------------------

    def generate(self, system: str, user: str) -> str:
        """Один вызов модели. Модель остаётся в памяти (keep_alive по умолчанию),
        чтобы следующий чанк не платил за загрузку заново."""
        return self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            keep_alive="5m",
        )

    def unload(self, timeout: float = 5.0) -> None:
        """keep_alive=0 — Ollama выгружает модель и освобождает видеопамять
        под следующую транскрибацию Whisper. Best-effort: ошибка здесь не важна.

        Короткий таймаут (5с, как в health()), а не self.idle_timeout (по умолчанию
        180с): unload() дёргается и из idle_tick воркера — на зависшей Ollama поток
        воркера иначе блокировался бы на все три минуты вместо мгновенного отказа."""
        try:
            self._chat([], keep_alive=0, timeout=timeout)
        except Exception:
            pass

    def health(self) -> dict:
        """Жива ли Ollama, установлена ли модель, влезла ли она в VRAM целиком.

        Пока модель не загружена, /api/ps о ней ничего не знает — warning остаётся
        None и появится после первого анализа. Это честно, а не «всё хорошо»."""
        info: dict = {"ok": False, "model": self.model, "installed": False,
                      "warning": None, "error": None}
        try:
            _open(self._request("/api/show", {"model": self.model}), 5).close()
        except Exception as e:
            info["error"] = self._explain(e)
            return info
        info["ok"] = True
        info["installed"] = True
        try:
            with _open(self._request("/api/ps", None), 5) as resp:
                loaded = json.loads(resp.read().decode("utf-8")).get("models", [])
        except Exception:
            return info  # /api/ps недоступен — просто нет сведений о VRAM
        for m in loaded:
            if m.get("name") != self.model:
                continue
            size, vram = float(m.get("size") or 0), float(m.get("size_vram") or 0)
            if size > 0 and vram < size * 0.99:
                gb = 1024 ** 3
                info["warning"] = (
                    f"модель загружена в видеопамять частично "
                    f"({vram / gb:.1f} из {size / gb:.1f} ГБ) — часть слоёв считается "
                    f"на процессоре, анализ пойдёт медленнее")
        return info


def make_provider() -> OllamaProvider:
    """Провайдер по текущему конфигу. Значения читаются в момент вызова —
    так их можно подменить в тестах через monkeypatch.setattr(config, ...)."""
    return OllamaProvider(config.LLM_BASE_URL, config.LLM_MODEL,
                          num_ctx=config.LLM_NUM_CTX,
                          temperature=config.LLM_TEMPERATURE,
                          idle_timeout=config.LLM_IDLE_TIMEOUT)
