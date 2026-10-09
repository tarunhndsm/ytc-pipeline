"""Inspect a rendered Short: stream facts, loudness, per-beat assets, one review frame per beat, and what the
narration sounds like to a speech recognizer."""

from __future__ import annotations

import difflib
import json
import re
import subprocess
from pathlib import Path

from . import ff

_LUFS = re.compile(r"I:\s+(-?[\d.]+) LUFS")
_PEAK = re.compile(r"Peak:\s+(-?[\d.]+) dBFS")
# Kokoro's pronunciation override, [Formosus](/fɔɹmˈOsəs/), is spoken and captioned as the bracketed word.
_OVERRIDE = re.compile(r"\[([^\]]+)\]\(/[^)]*/\)")
# Single letters are left alone: "X-ray", "V-2".
_ROMAN = re.compile(r"\b[IVX]{2,}\b")
_WAR = re.compile(r"\bWar (I{1,2})\b")
ASR_MODEL = "small.en"


def check(video: Path, longest: float = 58, shortest: float = 35) -> dict:
    video = video.resolve()
    work = video.parent / "work"
    frames = work / "frames"
    frames.mkdir(parents=True, exist_ok=True)

    info = subprocess.run([ff.exe(), "-hide_banner", "-i", str(video)], capture_output=True, text=True).stderr
    streams = [line.strip() for line in info.splitlines() if "Stream #" in line]
    loud = subprocess.run(
        [ff.exe(), "-hide_banner", "-nostats", "-i", str(video), "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True,
        text=True,
    ).stderr
    summary = loud[loud.rfind("Summary:") :]
    lufs, peak = _LUFS.search(summary), _PEAK.search(summary)

    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    beats = []
    for i, beat in enumerate(manifest["beats"]):
        # One frame from the middle of each shot: a long beat can cut from the whole picture to a close-up.
        paths = []
        for k, shot in enumerate(beat.get("shots") or [{"start": beat["start"], "end": beat["end"]}]):
            frame = frames / (f"frame{i:02d}.jpg" if k == 0 else f"frame{i:02d}_{k}.jpg")
            ff.run("-ss", f"{(shot['start'] + shot['end']) / 2:.2f}", "-i", video, "-frames:v", "1", "-vf", "scale=540:-1", frame)
            paths.append(str(frame))
        asset = beat["asset"]
        beats.append(
            {
                "beat": i,
                "seconds": round(beat["end"] - beat["start"], 2),
                "text": beat["text"],
                "source": asset.get("source"),
                "title": asset.get("title"),
                "license": asset.get("license"),
                "frame": paths[0],
                "frames": paths,
            }
        )
    duration = round(ff.duration(video), 2)
    integrated = float(lufs.group(1)) if lufs else None
    true_peak = float(peak.group(1)) if peak else None
    return {
        "video": str(video),
        "duration": duration,
        "words": len(manifest["words"]),
        "streams": streams,
        "integrated_lufs": integrated,
        "true_peak_dbfs": true_peak,
        "warnings": _warnings(duration, integrated, true_peak, beats, longest, shortest),
        "speech": _saved_speech(work) or speech(work / "narration.wav", manifest["beats"]),
        "beats": beats,
    }


def write_speech(work: Path) -> None:
    """Save the speech check next to the narration, so a cloud render brings it back with the Short."""
    beats = json.loads((work / "manifest.json").read_text(encoding="utf-8"))["beats"]
    (work / "speech.json").write_text(json.dumps(speech(work / "narration.wav", beats), indent=2), encoding="utf-8")


def _saved_speech(work: Path) -> dict | None:
    saved, narration = work / "speech.json", work / "narration.wav"
    # A later render on this Mac rewrites the narration and leaves the cloud's check stale.
    if saved.exists() and narration.exists() and saved.stat().st_mtime >= narration.stat().st_mtime:
        return json.loads(saved.read_text(encoding="utf-8"))
    return None


def speech(narration: Path, beats: list[dict]) -> dict:
    """Where a speech recognizer heard something other than the script: a mispronounced, skipped, or extra word."""
    try:
        from faster_whisper import WhisperModel
        from transformers.models.whisper.english_normalizer import EnglishTextNormalizer

        model = WhisperModel(ASR_MODEL, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(
            str(narration), language="en", temperature=0.0, condition_on_previous_text=False
        )
        heard_text = " ".join(segment.text.strip() for segment in segments)
    except Exception as error:  # the rest of the check is still worth having
        return {"error": f"{type(error).__name__}: {error}"}
    # Whisper's own normalizer, so "four million" in the script matches "4 million" in the transcript.
    normalize = EnglishTextNormalizer({})
    script = _words(normalize(" ".join(_OVERRIDE.sub(r"\1", beat["text"]) for beat in beats)))
    heard = _words(normalize(_numerals(heard_text)))
    differences = []
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(a=script, b=heard, autojunk=False).get_opcodes():
        wrote, said, before = " ".join(script[a1:a2]), " ".join(heard[b1:b2]), " ".join(script[max(a1 - 3, 0) : a1])
        if op == "equal" or wrote.replace(" ", "") == said.replace(" ", ""):
            continue
        differences.append(f"{wrote or '(nothing)'!r} heard as {said or '(nothing)'!r}" + (f" after {before!r}" if before else ""))
    return {"heard": heard_text, "differences": differences}


def _words(text: str) -> list[str]:
    return [word for word in text.split() if any(char.isalnum() for char in word)]


def _numerals(text: str) -> str:
    """Whisper writes "World War One" as "World War I" and "Stephen the Sixth" as "Stephen VI"."""
    return _ROMAN.sub(_ordinal, _WAR.sub(lambda war: f"War {len(war.group(1))}", text))


def _ordinal(numeral: re.Match) -> str:
    values = [{"I": 1, "V": 5, "X": 10}[letter] for letter in numeral.group()]
    n = sum(-v if i + 1 < len(values) and v < values[i + 1] else v for i, v in enumerate(values))
    return f"the {n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _warnings(duration: float, integrated: float | None, true_peak: float | None, beats: list[dict],
              longest: float = 58, shortest: float = 35) -> list[str]:
    """The quality gate's measurable rules (PLAYBOOK.md step 6), plus beats that fell back to a placeholder."""
    found = []
    if not shortest <= duration <= longest:
        found.append(f"duration {duration} s is outside {shortest:g}-{longest:g} s")
    if integrated is None or abs(integrated + 14) > 1:
        found.append(f"loudness {integrated} LUFS is outside -14 +/- 1")
    if true_peak is None or true_peak > -1:
        found.append(f"true peak {true_peak} dBFS is above -1")
    for beat in beats:
        where = f"beat {beat['beat'] + 1} ({Path(beat['frame']).name})"
        if beat["source"] == "generated gradient":
            found.append(f"{where} is a plain gradient: every image source failed")
        elif beat["source"] == "AI generated":
            found.append(f"{where} is AI-generated: set synthetic_media if it looks real")
    return found
