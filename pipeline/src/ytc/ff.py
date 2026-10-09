"""Locating and running the pinned FFmpeg build (imageio-ffmpeg ships one with libass)."""

from __future__ import annotations

import json
import re
import subprocess
from functools import cache
from pathlib import Path

import imageio_ffmpeg

_DURATION = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")
_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


@cache
def exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(*args: str | Path) -> None:
    cmd = [exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *map(str, args)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {proc.stderr.strip()[-3000:]}")


def loudnorm_stats(path: Path, loudnorm: str) -> dict[str, str]:
    """Run a measuring pass of the given loudnorm filter and return its JSON statistics."""
    cmd = [exe(), "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-af", f"{loudnorm}:print_format=json", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    blocks = _LOUDNORM_JSON.findall(proc.stderr)
    if proc.returncode != 0 or not blocks:
        raise RuntimeError(f"loudnorm measurement failed for {path}: {proc.stderr.strip()[-2000:]}")
    return json.loads(blocks[-1])


def duration(path: Path) -> float:
    proc = subprocess.run([exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    match = _DURATION.search(proc.stderr)
    if not match:
        raise RuntimeError(f"could not read the duration of {path}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def filter_path(path: Path) -> str:
    """Escape a path for use as a filter option value."""
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
