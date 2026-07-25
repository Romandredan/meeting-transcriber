class FakeProvider:
    """Провайдер-заглушка: отдаёт заготовленные ответы по очереди и пишет все вызовы.

    responses — список str или Exception (Exception будет брошен вместо ответа).
    Когда список кончился, отдаётся default: удобно для «сколько бы раз ни спросили».
    num_ctx нужен воркеру: он берёт окно контекста из провайдера.
    """

    def __init__(self, responses=None, default="ответ модели", num_ctx=32768):
        self.responses = list(responses or [])
        self.default = default
        self.num_ctx = num_ctx
        self.calls: list[tuple[str, str]] = []   # [(system, user), ...]
        self.unloaded = 0

    def generate(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if not self.responses:
            return self.default
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def health(self) -> dict:
        return {"ok": True, "model": "fake", "installed": True,
                "warning": None, "error": None}

    def unload(self) -> None:
        self.unloaded += 1
