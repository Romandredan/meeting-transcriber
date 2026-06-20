# GPU De-risking Spike — результат

**Дата:** 2026-06-21
**Машина:** Windows 11, NVIDIA GeForce RTX 5070 Ti (Blackwell, sm_120), драйвер 581.80
**Окружение:** venv Python 3.10.11, torch 2.11.0+cu128, ctranslate2 4.8.0, faster-whisper 1.2.1, numpy 2.2.6

## Вывод спайка

```
torch=2.11.0+cu128 cuda_available=True
device=NVIDIA GeForce RTX 5070 Ti capability=(12, 0)
OK faster-whisper: lang=ru segments=1
FAIL pyannote на cuda: ModuleNotFoundError: No module named 'pyannote'
=== ИТОГ ===
faster-whisper(CT2) GPU: OK
pyannote GPU: False
=== spike exit 0 ===
```

## Вердикт

- **Основной движок = faster-whisper (CTranslate2).** CT2 **работает на Blackwell sm_120** на `device=cuda, compute_type=float16` без ошибок «no kernel image» / cuDNN. Транскрипция тонового клипа прошла (`lang=ru`). PyTorch-native fallback (transformers) **не требуется** — оставлен в коде как страховка, но не активируется.
- **`nvidia-cudnn-cu12` отдельно не понадобился** — torch cu128 принёс совместимый cuDNN.
- **pyannote** в спайке упал только из-за того, что пакет ещё не установлен (не GPU-проблема). Проверка диаризации на GPU отложена до установки `pyannote.audio` и получения `HF_TOKEN` (см. Task 8 / end-to-end). Поскольку pyannote тоже на PyTorch cu128 (который уже подтверждён), риск низкий.

## Итог для плана

Гейт движка (§6 спеки) **пройден**. Дальнейшая реализация идёт по основному пути faster-whisper; ветку transformers-fallback не удаляем, но как основную не используем.
