"""Sound effects synthesized from scratch, so no license or Content ID question can arise."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 48000


def _normalize(signal: np.ndarray, peak: float) -> np.ndarray:
    return (signal / (np.abs(signal).max() + 1e-9) * peak).astype(np.float32)


def _swept_lowpass(noise: np.ndarray, cutoff_hz: np.ndarray) -> np.ndarray:
    alpha = 1.0 - np.exp(-2.0 * np.pi * cutoff_hz / SAMPLE_RATE)
    out = np.empty_like(noise)
    acc = 0.0
    for i in range(len(noise)):
        acc += alpha[i] * (noise[i] - acc)
        out[i] = acc
    return out


def whoosh(duration: float = 0.55, seed: int = 7) -> np.ndarray:
    n = int(duration * SAMPLE_RATE)
    t = np.linspace(0.0, 1.0, n, dtype=np.float32)
    noise = np.random.default_rng(seed).standard_normal(n).astype(np.float32)
    # float32 pi*t overshoots pi at t=1, and a negative base with a fractional power is NaN.
    arch = np.clip(np.sin(np.pi * t), 0.0, 1.0)
    filtered = _swept_lowpass(noise, 250.0 + 5500.0 * arch**2)
    return _normalize(filtered * arch**1.6, 0.7)


def pop(duration: float = 0.09) -> np.ndarray:
    t = np.arange(int(duration * SAMPLE_RATE)) / SAMPLE_RATE
    freq = 1100.0 * np.exp(-t * 28.0) + 170.0
    phase = 2.0 * np.pi * np.cumsum(freq) / SAMPLE_RATE
    return _normalize(np.sin(phase) * np.exp(-t * 42.0), 0.8)


def riser(duration: float = 1.2, seed: int = 11) -> np.ndarray:
    n = int(duration * SAMPLE_RATE)
    t = np.linspace(0.0, 1.0, n, dtype=np.float32)
    noise = np.random.default_rng(seed).standard_normal(n).astype(np.float32)
    airy = _swept_lowpass(noise, 400.0 + 7000.0 * t**2)
    tone = np.sin(2.0 * np.pi * np.cumsum(180.0 + 600.0 * t**2) / SAMPLE_RATE)
    return _normalize((0.7 * airy + 0.3 * tone) * t**2, 0.6)


_MAKERS = {"whoosh": whoosh, "pop": pop, "riser": riser}


def ensure(name: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{name}.wav"
    if not path.exists():
        sf.write(path, _MAKERS[name](), SAMPLE_RATE)
    return path
