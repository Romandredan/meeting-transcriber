from __future__ import annotations


def _cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def pick(force: str | None = None) -> tuple[str, str]:
    if force == "cpu":
        return ("cpu", "int8")
    if force == "cuda":
        return ("cuda", "float16")
    if _cuda_available():
        return ("cuda", "float16")
    return ("cpu", "int8")


def torch_device(force: str | None = None):
    import torch
    dev, _ = pick(force)
    return torch.device(dev)
