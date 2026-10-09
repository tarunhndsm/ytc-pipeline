"""A tanpura drone synthesized from scratch, so no license or Content ID question can arise.

Four strings tuned Pa, Sa, Sa and low Sa are plucked in turn, round after round. Each pluck is a sum of
harmonics whose upper partials swell and ebb after the attack: the shimmer a tanpura's curved bridge (jawari)
gives it. The loop wraps around, reverb included, so it repeats without a seam.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 48000
# Sa at C sharp, a usual pitch for a man's voice.
SA_HZ = 138.59
# Pa below Sa, Sa, Sa, and Sa an octave down, with how loud each string is.
STRINGS = ((0.75, 0.8), (1.0, 1.0), (1.0, 0.9), (0.5, 1.1))
# One round: three even plucks, then a longer rest after the low Sa.
PLUCK_AT = (0.0, 0.75, 1.5, 2.25)
CYCLE = 3.45
RING = 7.0
VARIANTS = 4
REVERB_SECONDS = 2.2


def _pluck(freq: float, rng: np.random.Generator) -> np.ndarray:
    """One pluck. Every pluck blooms, brightens and fades a little differently, as on a real instrument."""
    t = np.arange(int(RING * SAMPLE_RATE)) / SAMPLE_RATE
    # A tiny, slow pitch wander (about a cent) keeps the tone from sounding frozen.
    clock = np.cumsum(1.0 + 0.0006 * np.sin(2 * np.pi * 0.23 * t + rng.uniform(0, 2 * np.pi))) / SAMPLE_RATE
    # The jawari's bright band climbs from the 2nd harmonic to around the 18th in the seconds after the pluck,
    # then drifts up and down as it rings.
    rise, top, width = rng.uniform(0.6, 1.3), rng.uniform(14.0, 22.0), rng.uniform(14.0, 24.0)
    wobble = 1.5 * np.sin(2 * np.pi * t / rng.uniform(1.5, 3.0) + rng.uniform(0, 2 * np.pi)) * (1 - np.exp(-t / 0.5))
    centre = 2.0 + top * (1.0 - np.exp(-t / rise)) + wobble
    ring = 4.5 * rng.uniform(0.85, 1.15)
    out = np.zeros_like(t)
    for h in range(1, int(min(48, 9000 / freq)) + 1):
        swell = 0.25 + np.exp(-((h - centre) ** 2) / width)
        decay = np.exp(-t * (1 + 0.035 * h) / ring)
        out += h**-0.9 * swell * decay * np.sin(2 * np.pi * freq * h * clock + rng.uniform(0, 2 * np.pi))
    thump = rng.standard_normal(len(t)) * np.exp(-t / 0.004) * 0.05
    return (out + thump) * np.minimum(t / 0.006, 1.0)


def tanpura(seconds: float = 55.0, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    rounds = max(1, round(seconds / CYCLE))
    total = int(rounds * CYCLE * SAMPLE_RATE)
    out = np.zeros(total)
    plucks = [[_pluck(SA_HZ * ratio, rng) * gain for _ in range(VARIANTS)] for ratio, gain in STRINGS]
    for r in range(rounds):
        for string, at in enumerate(PLUCK_AT):
            # A player's hand: a little early or late, a little louder or softer.
            start = int((r * CYCLE + at + rng.normal(0.0, 0.03)) * SAMPLE_RATE) % total
            pluck = plucks[string][rng.integers(VARIANTS)] * 10 ** (rng.normal(0.0, 0.8) / 20)
            head = min(len(pluck), total - start)
            out[start:start + head] += pluck[:head]
            out[: len(pluck) - head] += pluck[head:]
    n = int(REVERB_SECONDS * SAMPLE_RATE)
    ir = rng.standard_normal(n) * np.exp(-np.arange(n) / SAMPLE_RATE * 6.9 / REVERB_SECONDS)
    wet = np.fft.irfft(np.fft.rfft(out) * np.fft.rfft(ir, total), total)
    out = out + 0.35 * wet * np.abs(out).max() / (np.abs(wet).max() + 1e-9)
    return (out / (np.abs(out).max() + 1e-9) * 0.7).astype(np.float32)


def ensure(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "tanpura.wav"
    if not path.exists():
        sf.write(path, tanpura(), SAMPLE_RATE)
    return path
