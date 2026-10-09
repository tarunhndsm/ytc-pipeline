"""A music bed composed for one Short from scratch, so no license or Content ID question can arise, and unlike
any other Short's: YouTube's spam policy names the same background music across many videos as a sign of mass
production. The Short's id seeds the key, tempo, chords, and patterns; the mood comes from the spec.

A soft pad holds each chord, a plucked or mallet arpeggio runs over it in eighth notes, a bass note and a soft
kick mark the beat, and a shaker ticks underneath; a little reverb puts them in one room.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 48000
MINOR = (0, 2, 3, 5, 7, 8, 10)
MAJOR = (0, 2, 4, 5, 7, 9, 11)
# Per mood: the scale, chord progressions as scale degrees (0 is the root's chord), the tempo range in beats a
# minute, the arpeggio's instrument, and whether the kick lands on every beat rather than on 1 and 3.
MOODS = {
    "mystery": (MINOR, ((0, 5, 2, 6), (0, 3, 5, 4), (0, 5, 3, 4), (0, 6, 5, 6)), (84, 98), "pluck", False),
    "chase": (MINOR, ((0, 6, 5, 6), (0, 0, 5, 6), (0, 3, 6, 4)), (100, 114), "pluck", True),
    "wonder": (MAJOR, ((0, 4, 5, 3), (0, 5, 3, 4), (3, 0, 4, 5)), (88, 104), "mallet", False),
}
# Which chord tone each eighth note of a bar plays (0 root, 1 third, 2 fifth, 3 the root an octave up).
PATTERNS = ((0, 1, 2, 3, 2, 1, 0, 1), (0, 2, 1, 2, 0, 2, 1, 2), (0, 1, 2, 1, 3, 2, 1, 2), (2, 1, 0, 1, 2, 3, 2, 1))
REVERB_SECONDS = 1.4


def _hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def _t(seconds: float) -> np.ndarray:
    return np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE


def _chord(scale: tuple, degree: int) -> list[int]:
    """The chord's root, third, fifth, and seventh, in semitones above the key's root."""
    return [scale[(degree + step) % 7] + 12 * ((degree + step) // 7) for step in (0, 2, 4, 6)]


def _pad(freqs: list[float], seconds: float, rng: np.random.Generator) -> np.ndarray:
    """Soft, slightly detuned tones that swell in and fade out, for one chord."""
    t = _t(seconds)
    out = np.zeros_like(t)
    for freq in freqs:
        for detune in (-0.0012, 0.0012):
            phase = rng.uniform(0, 2 * np.pi)
            for h in range(1, 7):
                if freq * h > 5000:
                    break
                out += h**-1.6 * np.sin(2 * np.pi * freq * (1 + detune) * h * t + phase * h)
    swell = 0.85 + 0.15 * np.sin(2 * np.pi * 0.25 * t + rng.uniform(0, 2 * np.pi))
    envelope = np.minimum(t / 0.35, 1.0) * np.clip((seconds - t) / 0.5, 0.0, 1.0)
    return out * swell * envelope / len(freqs)


def _pluck(freq: float, rng: np.random.Generator, seconds: float = 1.2) -> np.ndarray:
    """A plucked string: harmonics that die away faster the higher they are, and a little pick noise."""
    t = _t(seconds)
    out = np.zeros_like(t)
    for h in range(1, 13):
        if freq * h > 9000:
            break
        out += h**-1.1 * np.exp(-t * (1.5 + 0.9 * h)) * np.sin(2 * np.pi * freq * h * t + rng.uniform(0, 2 * np.pi))
    out += rng.standard_normal(len(t)) * np.exp(-t / 0.003) * 0.08
    return out * np.minimum(t / 0.003, 1.0)


def _mallet(freq: float, rng: np.random.Generator, seconds: float = 1.0) -> np.ndarray:
    """A marimba-like bar: its inharmonic partials, the upper ones gone almost at once."""
    t = _t(seconds)
    out = np.zeros_like(t)
    for ratio, amp, rate in ((1.0, 1.0, 3.2), (3.93, 0.32, 11.0), (9.3, 0.1, 22.0)):
        if freq * ratio < 12000:
            out += amp * np.exp(-t * rate) * np.sin(2 * np.pi * freq * ratio * t + rng.uniform(0, 2 * np.pi))
    return out * np.minimum(t / 0.002, 1.0)


def _bass(freq: float, seconds: float) -> np.ndarray:
    t = _t(seconds)
    tone = np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(4 * np.pi * freq * t)
    return np.tanh(1.4 * tone * np.exp(-t * 2.6) * np.minimum(t / 0.005, 1.0))


def _kick() -> np.ndarray:
    t = _t(0.45)
    pitch = 45 + 70 * np.exp(-t / 0.035)
    return np.sin(2 * np.pi * np.cumsum(pitch) / SAMPLE_RATE) * np.exp(-t / 0.16)


def _shaker(rng: np.random.Generator) -> np.ndarray:
    t = _t(0.12)
    return np.diff(rng.standard_normal(len(t) + 1)) * np.exp(-t / 0.03)


def _add(out: np.ndarray, sound: np.ndarray, at: float, gain: float) -> None:
    start = max(0, int(at * SAMPLE_RATE))
    if start >= len(out):
        return
    end = min(len(out), start + len(sound))
    out[start:end] += gain * sound[:end - start]


def _reverb(dry: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    n = int(REVERB_SECONDS * SAMPLE_RATE)
    ir = rng.standard_normal(n) * np.exp(-np.arange(n) / SAMPLE_RATE * 6.9 / REVERB_SECONDS)
    size = len(dry) + n
    wet = np.fft.irfft(np.fft.rfft(dry, size) * np.fft.rfft(ir, size), size)[: len(dry)]
    return wet * np.abs(dry).max() / (np.abs(wet).max() + 1e-9)


def music(seed: str, seconds: float, mood: str = "mystery") -> np.ndarray:
    rng = np.random.default_rng(int(hashlib.sha256(f"{seed}:{mood}".encode()).hexdigest()[:12], 16))
    scale, progressions, (slow, fast), lead, four = MOODS.get(mood, MOODS["mystery"])
    beat = 60.0 / rng.integers(slow, fast + 1)
    bar = 4 * beat
    root = 45 + int(rng.integers(0, 8))  # A2 to E3
    progression = progressions[rng.integers(len(progressions))]
    pattern = PATTERNS[rng.integers(len(PATTERNS))]
    octave = 24 + 12 * int(rng.integers(0, 2)) if lead == "mallet" else 24
    total = int((seconds + 0.5) * SAMPLE_RATE)
    tonal, drums = np.zeros(total), np.zeros(total)
    kick = _kick()
    bars = int(np.ceil(seconds / bar)) + 1
    for b in range(bars):
        at = b * bar
        chord = _chord(scale, progression[b % len(progression)])
        _add(tonal, _pad([_hz(root + 12 + n) for n in chord], bar + 0.5, rng), at, 0.22)
        tones = [chord[0], chord[1], chord[2], chord[0] + 12]
        for e, step in enumerate(pattern):
            note = _hz(root + octave + tones[step])
            sound = _mallet(note, rng) if lead == "mallet" else _pluck(note, rng)
            accent = 1.0 if e % 2 == 0 else 0.72
            _add(tonal, sound, at + e * beat / 2 + rng.normal(0, 0.006), 0.3 * accent * 10 ** (rng.normal(0, 1.2) / 20))
        for k in range(4):
            if four or k % 2 == 0:
                _add(drums, kick, at + k * beat, 0.45 if k == 0 else 0.38)
                _add(drums, _bass(_hz(root + chord[0]), beat * (1 if four else 2)), at + k * beat, 0.28)
        for e in range(8):
            _add(drums, _shaker(rng), at + e * beat / 2 + rng.normal(0, 0.004), 0.05 if e % 2 else 0.03)
    out = tonal + 0.25 * _reverb(tonal, rng) + drums
    out = out[: int(seconds * SAMPLE_RATE)] * np.minimum(_t(seconds) / 0.2, 1.0)
    return (out / (np.abs(out).max() + 1e-9) * 0.8).astype(np.float32)


def compose(seed: str, seconds: float, path: Path, mood: str = "mystery") -> Path:
    """The bed for a Short, `seconds` long, written to `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, music(seed, seconds, mood), SAMPLE_RATE)
    return path
