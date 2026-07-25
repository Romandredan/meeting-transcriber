import io
import json
import socket
import urllib.error

import pytest

from app import llm


def ndjson(*objs) -> io.BytesIO:
    body = "".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objs)
    return io.BytesIO(body.encode("utf-8"))


class SlowStream:
    """Живой, но медленный поток: каждая строка приходит с задержкой, но приходит."""

    def __init__(self, objs):
        self._lines = [json.dumps(o, ensure_ascii=False).encode("utf-8") + b"\n" for o in objs]
        self.reads = 0

    def readline(self):
        self.reads += 1
        return self._lines.pop(0) if self._lines else b""

    def close(self):
        pass


class SilentStream:
    """Ollama зависла: соединение живо, но байты не идут — сокет бросает таймаут."""

    def readline(self):
        raise socket.timeout("timed out")

    def close(self):
        pass


def provider():
    return llm.OllamaProvider("http://localhost:11434", "qwen3:14b",
                              num_ctx=4096, temperature=0.2, idle_timeout=5)


def test_generate_concatenates_streamed_chunks(monkeypatch):
    monkeypatch.setattr(llm, "_open", lambda req, timeout: ndjson(
        {"message": {"content": "Прото"}, "done": False},
        {"message": {"content": "кол"}, "done": False},
        {"message": {"content": ""}, "done": True},
    ))
    assert provider().generate("сис", "польз") == "Протокол"


def test_generate_sends_num_ctx_and_stream(monkeypatch):
    seen = {}

    def fake_open(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return ndjson({"message": {"content": "ок"}, "done": True})

    monkeypatch.setattr(llm, "_open", fake_open)
    provider().generate("сис", "польз")
    assert seen["url"] == "http://localhost:11434/api/chat"
    assert seen["body"]["stream"] is True
    assert seen["body"]["options"]["num_ctx"] == 4096
    assert seen["body"]["options"]["temperature"] == 0.2
    assert seen["body"]["messages"][0] == {"role": "system", "content": "сис"}
    assert seen["body"]["messages"][1] == {"role": "user", "content": "польз"}


def test_generate_raises_on_idle_timeout(monkeypatch):
    monkeypatch.setattr(llm, "_open", lambda req, timeout: SilentStream())
    with pytest.raises(llm.LlmError) as e:
        provider().generate("сис", "польз")
    assert "не отвечает" in str(e.value)


def test_generate_survives_slow_but_live_stream(monkeypatch):
    stream = SlowStream([{"message": {"content": "мед"}, "done": False},
                         {"message": {"content": "ленно"}, "done": True}])
    seen = {}

    def fake_open(req, timeout):
        seen["timeout"] = timeout
        return stream

    monkeypatch.setattr(llm, "_open", fake_open)
    p = provider()
    # Таймаут задан на ЧТЕНИЕ, а не на весь ответ: много медленных чтений — не ошибка.
    assert p.generate("сис", "польз") == "медленно"
    # timeout, переданный в _open, — это ИМЕННО idle_timeout провайдера: подтверждает,
    # что таймаут отдан сокету на КАЖДОЕ чтение, а не отсчитывается нами по wall-clock
    # поверх всего ответа.
    assert seen["timeout"] == p.idle_timeout
    # readline вызывался построчно (по разу на каждую переданную строку), а не был
    # прочитан одним куском — иначе сама идея сокетного таймаута на чтение была бы
    # неотличима от общего таймера на весь ответ.
    assert stream.reads == 2


def test_generate_raises_on_stream_cut_before_done(monkeypatch):
    """Соединение обрывается ДО объекта с done:true — это отказ, а не успех:
    частичный результат никогда не должен просачиваться наружу как ответ."""
    monkeypatch.setattr(llm, "_open", lambda req, timeout: ndjson(
        {"message": {"content": "часть"}, "done": False},
    ))
    with pytest.raises(llm.LlmError) as e:
        provider().generate("сис", "польз")
    assert "оборвала" in str(e.value)


def test_generate_reports_missing_model(monkeypatch):
    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(llm, "_open", boom)
    with pytest.raises(llm.LlmError) as e:
        provider().generate("сис", "польз")
    assert "ollama pull qwen3:14b" in str(e.value)


def test_generate_reports_dead_daemon(monkeypatch):
    def boom(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(llm, "_open", boom)
    with pytest.raises(llm.LlmError) as e:
        provider().generate("сис", "польз")
    assert "запустите Ollama" in str(e.value)


def test_generate_raises_on_error_field(monkeypatch):
    monkeypatch.setattr(llm, "_open", lambda req, timeout: ndjson(
        {"error": "model requires more system memory"}))
    with pytest.raises(llm.LlmError) as e:
        provider().generate("сис", "польз")
    assert "more system memory" in str(e.value)


def test_health_warns_on_partial_vram_offload(monkeypatch):
    def fake_open(req, timeout):
        if req.full_url.endswith("/api/show"):
            return io.BytesIO(b'{"details": {}}')
        return io.BytesIO(json.dumps({"models": [
            {"name": "qwen3:14b", "size": 10_000_000_000, "size_vram": 6_000_000_000}
        ]}).encode("utf-8"))

    monkeypatch.setattr(llm, "_open", fake_open)
    h = provider().health()
    assert h["ok"] is True
    assert h["installed"] is True
    assert "частично" in h["warning"]


def test_health_has_no_warning_when_fully_on_gpu(monkeypatch):
    def fake_open(req, timeout):
        if req.full_url.endswith("/api/show"):
            return io.BytesIO(b'{"details": {}}')
        return io.BytesIO(json.dumps({"models": [
            {"name": "qwen3:14b", "size": 10_000_000_000, "size_vram": 10_000_000_000}
        ]}).encode("utf-8"))

    monkeypatch.setattr(llm, "_open", fake_open)
    assert provider().health()["warning"] is None


def test_health_has_no_warning_when_model_missing_from_ps(monkeypatch):
    """Модель установлена, но ещё не загружена в память: /api/ps о ней ничего не
    знает. Это не ошибка и не "всё хорошо" — просто нет сведений о VRAM."""
    def fake_open(req, timeout):
        if req.full_url.endswith("/api/show"):
            return io.BytesIO(b'{"details": {}}')
        return io.BytesIO(json.dumps({"models": []}).encode("utf-8"))

    monkeypatch.setattr(llm, "_open", fake_open)
    h = provider().health()
    assert h["ok"] is True
    assert h["installed"] is True
    assert h["warning"] is None
    assert h["error"] is None


def test_health_reports_missing_model(monkeypatch):
    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(llm, "_open", boom)
    h = provider().health()
    assert h["ok"] is False
    assert h["installed"] is False
    assert "ollama pull qwen3:14b" in h["error"]


def test_unload_sends_keep_alive_zero(monkeypatch):
    seen = {}

    def fake_open(req, timeout):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return ndjson({"message": {"content": ""}, "done": True})

    monkeypatch.setattr(llm, "_open", fake_open)
    provider().unload()
    assert seen["body"]["keep_alive"] == 0
    assert seen["body"]["messages"] == []
