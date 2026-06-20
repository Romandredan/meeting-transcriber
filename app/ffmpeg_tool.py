from __future__ import annotations

import shutil
import subprocess


class FFmpegError(Exception):
    pass


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def build_extract_cmd(src: str, dst: str, ffmpeg: str = "ffmpeg") -> list[str]:
    return [
        ffmpeg, "-y", "-i", src,
        "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        dst,
    ]


def extract_audio(src: str, dst: str) -> None:
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise FFmpegError(
            "ffmpeg не найден в PATH. Установите ffmpeg или укажите путь к нему."
        )
    cmd = build_extract_cmd(src, dst, ffmpeg)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise FFmpegError("Ошибка извлечения аудио: " + " | ".join(tail))
