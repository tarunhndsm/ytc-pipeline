"""Narration with Kokoro or Gemini TTS, returning the audio plus the timing of every spoken word."""

from __future__ import annotations

import base64
import difflib
import io
import json
import logging
import os
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests
import soundfile as sf

from . import ff, llm
from .spec import ShortSpec, Voice

log = logging.getLogger(__name__)

SAMPLE_RATE = 24000
SPEECH_FLOOR = 0.01  # fraction of a clip's peak; quieter samples at its ends count as silence
EDGE_KEEP = 0.05
_PUNCTUATION = set(".,!?;:\u2026\u2014\u2013-\"'()[]\u201c\u201d\u2018\u2019")
_pipelines: dict[str, object] = {}

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models"
# Newest first; each has its own free daily quota.
GEMINI_TTS = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts", "gemini-3.1-flash-tts-preview",
              "gemini-2.5-flash-preview-tts")
# Free TTS allows a few requests a minute, so a 429 that asks for a short wait is waited out on the same model.
MAX_QUOTA_WAIT = 90.0
# Notes in the script's language get read out, so they are in English, ahead of a TRANSCRIPT label.
GEMINI_DIRECTION = "Read the transcript below aloud as a warm, natural storyteller. Speak only the transcript."
# Whisper times the words of a Gemini take; for Hindi, small misheard about one letter in seven.
ALIGN_MODEL = os.environ.get("YTC_ALIGN_MODEL", "large-v3-turbo")
WHISPER_RATE = 16000
# Whisper's timestamps can jump ahead past a stretch of speech: every take of ak016 lost 12 s that, heard on its own,
# was two lines read word for word. A gap between heard words longer than this (an ellipsis pause runs under a second)
# that still has a second of speech in it is heard again on its own.
REHEAR_GAP = 2.0
# No word takes longer than this to say (s). The word before a skipped stretch comes back stretched across it, and is
# heard again with the rest.
LONGEST_WORD = 1.5
# A tenth of a second this loud against the take's peak (dB) counts as speech.
SPEECH_DB = -35.0
WHISPER_LANGUAGE = {"a": "en", "b": "en", "h": "hi", "e": "es", "f": "fr", "i": "it", "p": "pt", "j": "ja", "z": "zh"}
# The share of each line's letters Whisper (large-v3-turbo) must find in a take. Every line of a good Hindi take
# scored 91% or more; a take that skipped a line scored 0% on it and 45-67% on the garbled lines around it.
MIN_COVERAGE = 0.75
# The longest stretch of a take, in seconds, that may match nothing in the script. Line coverage can't see a take
# that adds sentences of its own: one read 80 s of invented story between two lines and still scored 86% or more on
# every line. Whisper's mishearing left at most 0.4 s unmatched in six good Hindi takes; ak043's take, kept at 1.5,
# said "कैसी रही ये जनरेशन?" (1.3 s) and laughed between two lines.
MAX_UNSCRIPTED = 1.0
TAKES = 3
# Beside narration.wav: the narration_key it was read from (reel_rescue writes it when it brings a render's
# narration back to render new pictures).
KEPT_NARRATION = "narration.json"
# A line the recognizer heard less of than this is listed for the reviewer to listen to.
SPEECH_CLEAR = 0.9
# A beat's picture comes in this long before its first word.
CUT_LEAD = 0.08


@dataclass
class Word:
    text: str
    start: float
    end: float
    beat: int


@dataclass
class Narration:
    wav_path: Path
    duration: float
    words: list[Word]
    beat_spans: list[tuple[float, float]]


def _pipeline(lang_code: str):
    from kokoro import KPipeline

    if lang_code not in _pipelines:
        _pipelines[lang_code] = KPipeline(lang_code=lang_code, repo_id="hexgrad/Kokoro-82M")
    return _pipelines[lang_code]


def phonemes(text: str, lang_code: str = "a") -> str:
    """How Kokoro will say the text, in the alphabet a [word](/phonemes/) override takes."""
    return _pipeline(lang_code).g2p(text)[0]


def _words(tokens, offset: float, beat: int) -> list[Word]:
    """Join sub-word tokens into display words and fold stray punctuation into the previous word."""
    merged: list[list] = []
    pending: list | None = None
    for tok in tokens:
        if tok.start_ts is None or tok.end_ts is None:
            continue
        if pending is None:
            pending = [tok.text, offset + tok.start_ts, offset + tok.end_ts]
        else:
            pending[0] += tok.text
            pending[2] = offset + tok.end_ts
        if tok.whitespace:
            merged.append(pending)
            pending = None
    if pending is not None:
        merged.append(pending)

    words: list[Word] = []
    for text, start, end in merged:
        text = text.strip()
        if not text:
            continue
        if all(ch in _PUNCTUATION for ch in text):
            if words:
                words[-1].text += text
            continue
        words.append(Word(text, start, max(end, start + 0.05), beat))
    return words


def _tighten(audio: np.ndarray, longest: float) -> np.ndarray:
    """The take with every silence inside it longer than `longest` seconds cut down to that, from its middle."""
    frame = int(0.01 * SAMPLE_RATE)
    count = len(audio) // frame
    if not longest or count < 3:
        return audio
    level = np.sqrt(np.mean(audio[: count * frame].reshape(count, frame) ** 2, axis=1))
    quiet = level < 10 ** (SPEECH_DB / 20) * level.max()
    keep, cuts, run = int(longest / 2 * SAMPLE_RATE), [], 0
    for i, still in enumerate(np.append(quiet, False)):
        if still:
            run += 1
            continue
        # Only a silence between sounds: the take's two ends are trimmed separately (_speech_bounds).
        if run and i - run > 0 and i < count and run * frame > 2 * keep:
            cuts.append(((i - run) * frame + keep, i * frame - keep))
        run = 0
    if not cuts:
        return audio
    fade = int(0.005 * SAMPLE_RATE)
    ramp = np.linspace(0.0, 1.0, fade, dtype=audio.dtype)
    out = audio[: cuts[0][0]]
    for (_, end), following in zip(cuts, [c[0] for c in cuts[1:]] + [len(audio)]):
        piece = audio[end:following]
        out = np.concatenate([out[:-fade], out[-fade:] * (1 - ramp) + piece[:fade] * ramp, piece[fade:]])
    return out


def _speech_bounds(audio: np.ndarray) -> tuple[int, int]:
    loud = np.flatnonzero(np.abs(audio) > SPEECH_FLOOR * np.abs(audio).max(initial=0.0))
    if not loud.size:
        return 0, len(audio)
    keep = int(EDGE_KEEP * SAMPLE_RATE)
    return max(int(loud[0]) - keep, 0), min(int(loud[-1]) + keep + 1, len(audio))


def _quota(resp: requests.Response) -> tuple[str, float | None]:
    """The quotas a 429 names (id=limit), and how many seconds Google asks us to wait, if it says."""
    try:
        details = resp.json()["error"].get("details", [])
    except (ValueError, KeyError, AttributeError):
        return "", None
    quota, delay = "", None
    for detail in details:
        kind = detail.get("@type", "")
        if kind.endswith("QuotaFailure"):
            quota = ", ".join(f"{v.get('quotaId', '?')}={v.get('quotaValue', '?')}" for v in detail.get("violations", []))
        elif kind.endswith("RetryInfo"):
            try:
                delay = float(str(detail.get("retryDelay", "")).rstrip("s"))
            except ValueError:
                pass
    return quota, delay


def _samples(data: bytes, mime: str) -> np.ndarray:
    """Gemini's audio as float samples at SAMPLE_RATE: raw 16-bit PCM from older models, a WAV file from 3.8."""
    if data[:4] == b"RIFF":
        audio, rate = sf.read(io.BytesIO(data), dtype="float32")
        audio = audio.mean(axis=1) if audio.ndim > 1 else audio
    else:
        audio = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        rate = next((int(p[5:]) for p in mime.replace(" ", "").split(";") if p.startswith("rate=")), SAMPLE_RATE)
    if rate != SAMPLE_RATE:
        n = round(len(audio) * SAMPLE_RATE / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
    return audio


def _gemini_take(text: str, voice: Voice, models: list[str] | None = None) -> tuple[np.ndarray, str]:
    """One reading of the whole script, so the delivery carries from line to line, and the model that read it."""
    key = os.environ.get("YTC_GEMINI_API_KEY")
    if not key:
        raise RuntimeError("YTC_GEMINI_API_KEY is not set")
    if WHISPER_LANGUAGE.get(voice.lang_code) == "en":
        part = {"text": text}
        if voice.direction:
            part["speechMetadata"] = {"style": voice.direction}
    else:
        part = {"text": f"{voice.direction or GEMINI_DIRECTION}\n\nTRANSCRIPT:\n{text}"}
    chosen = {"voice": voice.voice} if voice.voice.startswith("voice_") else {"prebuiltVoiceConfig": {"voiceName": voice.voice}}
    speech = {"voiceConfig": chosen} | ({"languageCode": voice.language} if voice.language else {})
    body = {"contents": [{"parts": [part]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": speech}}
    errors = []
    for model in models or ([voice.model] if voice.model else GEMINI_TTS):
        for attempt in range(3):
            started = time.monotonic()
            resp = requests.post(f"{GEMINI_API}/{model}:generateContent", headers={"x-goog-api-key": key},
                                 json=body, timeout=300)
            if resp.status_code == 200:
                reply = resp.json()
                usage = reply.get("usageMetadata", {})
                llm.record({"model": model, "purpose": "narration", "seconds": round(time.monotonic() - started, 1),
                            "input_tokens": usage.get("promptTokenCount"),
                            "output_tokens": usage.get("candidatesTokenCount"),
                            "thinking_tokens": usage.get("thoughtsTokenCount")})
                try:
                    part = reply["candidates"][0]["content"]["parts"][0]["inlineData"]
                    return _samples(base64.b64decode(part["data"]), part.get("mimeType", "")), model
                except (KeyError, IndexError):
                    errors.append(f"{model}: no audio")
                    continue
            if resp.status_code == 402:
                raise llm.OutOfCredits(f"{model}: HTTP 402, the Gemini project's prepaid credits ran out")
            if resp.status_code == 429:
                quota, delay = _quota(resp)
                errors.append(f"{model}: HTTP 429 {quota}".rstrip())
                log.info("%s: HTTP 429 %s; Google asks for a %s s wait", model, quota or "(no quota named)", delay)
                # A per-minute limit clears in seconds; a per-day one means this model is done until tomorrow.
                if delay is not None and delay <= MAX_QUOTA_WAIT and "PerDay" not in quota:
                    time.sleep(delay + 1)
                    continue
                break
            if resp.status_code == 400 and "speechMetadata" in part and "Speech metadata is not supported" in resp.text:
                # The TTS models before 3.8 take no style; they read the script without it.
                del part["speechMetadata"]
                continue
            errors.append(f"{model}: HTTP {resp.status_code}")
            if resp.status_code < 500:
                resp.raise_for_status()
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"no Gemini TTS model read the script: {'; '.join(errors)}")


def _letters(text: str) -> str:
    """A text's letters, vowel signs and digits: what a transcript and the script can be matched on."""
    return "".join(ch for ch in unicodedata.normalize("NFC", text).lower() if unicodedata.category(ch)[0] in "LMN")


def _heard(wav: Path, language: str, prompt: str | None = None) -> list[tuple[str, float, float]]:
    """The words Whisper hears in `wav`, with their times. A `prompt` (the script) steers how it spells what it
    hears, such as English names written in Devanagari, without supplying words the audio doesn't have."""
    from faster_whisper import WhisperModel, decode_audio

    model = WhisperModel(ALIGN_MODEL, device="cpu", compute_type="int8")
    audio = decode_audio(str(wav), sampling_rate=WHISPER_RATE)

    def hear(start: float, end: float) -> list[tuple[str, float, float]]:
        segments, _ = model.transcribe(audio[int(start * WHISPER_RATE):int(end * WHISPER_RATE)], language=language,
                                       word_timestamps=True, temperature=0.0, condition_on_previous_text=False,
                                       initial_prompt=prompt)
        # Words stamped at the very end of the audio are Whisper repeating itself, not speech.
        return [(w.word, start + w.start, start + w.end) for segment in segments for w in segment.words or []
                if start + w.start < end - 0.1]

    duration = len(audio) / WHISPER_RATE
    heard = [word for word in hear(0.0, duration) if word[2] - word[1] <= LONGEST_WORD]
    tenth = WHISPER_RATE // 10
    floor = float(np.abs(audio).max()) * 10 ** (SPEECH_DB / 20)
    loud = [float(np.sqrt(np.mean(audio[i:i + tenth] ** 2))) > floor for i in range(0, len(audio), tenth)]
    edges = [0.0, *(t for _, start, end in heard for t in (start, end)), duration]
    for start, end in zip(edges[::2], edges[1::2]):
        if end - start > REHEAR_GAP and sum(loud[int(start * 10):int(end * 10)]) >= 10:
            heard += hear(start, end)
    return sorted(heard, key=lambda word: word[1])


def _align(tokens: list[tuple[str, int]], heard: list[tuple[str, float, float]]) -> tuple[list[Word], list[float]]:
    """Time the script's own words (text, beat) by matching their letters to the ones Whisper heard, and say what
    share of each beat's letters it heard. Words it missed share the time between their neighbors."""
    script, owner = "", []
    beats = max(beat for _, beat in tokens) + 1
    total, found = [0] * beats, [0] * beats
    for index, (text, beat) in enumerate(tokens):
        letters = _letters(text)
        script += letters
        owner += [index] * len(letters)
        total[beat] += len(letters)
    spoken, clock = "", []
    for text, start, end in heard:
        letters = _letters(text)
        spoken += letters
        step = (end - start) / max(len(letters), 1)
        clock += [(start + k * step, start + (k + 1) * step) for k in range(len(letters))]
    first: list[float | None] = [None] * len(tokens)
    last: list[float | None] = [None] * len(tokens)
    for a, b, size in difflib.SequenceMatcher(None, script, spoken, autojunk=False).get_matching_blocks():
        for k in range(size):
            i = owner[a + k]
            found[tokens[i][1]] += 1
            if first[i] is None:
                first[i] = clock[b + k][0]
            last[i] = clock[b + k][1]

    known = [i for i in range(len(tokens)) if first[i] is not None]
    if not known:
        raise RuntimeError("Whisper heard none of the script")
    words, floor = [], 0.0
    for i, (text, beat) in enumerate(tokens):
        if first[i] is None:
            before = max((j for j in known if j < i), default=None)
            after = min((j for j in known if j > i), default=None)
            lo = last[before] if before is not None else heard[0][1]
            hi = first[after] if after is not None else heard[-1][2]
            span = range(before + 1 if before is not None else 0, after if after is not None else len(tokens))
            sizes = [max(len(_letters(tokens[j][0])), 1) for j in span]
            done = sum(sizes[: i - span.start])
            first[i] = lo + (hi - lo) * done / sum(sizes)
            last[i] = lo + (hi - lo) * (done + sizes[i - span.start]) / sum(sizes)
        start = max(first[i], floor)
        words.append(Word(text, start, max(last[i], start + 0.05), beat))
        floor = start
    return words, [f / max(t, 1) for f, t in zip(found, total)]


def _unscripted(tokens: list[tuple[str, int]], heard: list[tuple[str, float, float]]) -> tuple[float, float]:
    """The longest run of words Whisper heard that mostly match nothing in the script, as (start, end) seconds."""
    script = "".join(_letters(text) for text, _ in tokens)
    words = [(_letters(text), start, end) for text, start, end in heard]
    spoken = "".join(letters for letters, _, _ in words)
    matched = [False] * len(spoken)
    for block in difflib.SequenceMatcher(None, script, spoken, autojunk=False).get_matching_blocks():
        matched[block.b:block.b + block.size] = [True] * block.size
    longest, since, at = (0.0, 0.0), None, 0
    for letters, start, end in words:
        found = sum(matched[at:at + len(letters)])
        at += len(letters)
        if not letters:
            continue
        if found < len(letters) / 2:
            since = start if since is None else since
            longest = max(longest, (since, end), key=lambda run: run[1] - run[0])
        else:
            since = None
    return longest


def speech_report(coverage: list[float]) -> dict:
    """What check.speech reports, from how much of each line Whisper heard in a Gemini take."""
    return {"coverage": [round(c, 3) for c in coverage],
            "differences": [f"beat {i}: the recognizer heard only {c:.0%} of the line"
                            for i, c in enumerate(coverage, start=1) if c < SPEECH_CLEAR]}


def _tempo(audio: np.ndarray, speed: float) -> np.ndarray:
    """The take `speed` times as fast at the same pitch: Gemini's TTS has no rate setting."""
    if speed == 1.0:
        return audio
    with tempfile.TemporaryDirectory() as tmp:
        take, faster = Path(tmp) / "take.wav", Path(tmp) / "faster.wav"
        sf.write(take, audio, SAMPLE_RATE)
        ff.run("-i", take, "-filter:a", f"atempo={speed}", faster)
        return sf.read(faster, dtype="float32")[0]


def narration_key(spec: ShortSpec) -> dict:
    """What a kept take or narration must have been read from to be used again: the script and the voice."""
    # Without respellings or a language, a voice still matches the takes kept before those fields existed.
    unset = {name for name in ("say", "language", "max_pause") if not getattr(spec.voice, name)}
    return {"script": "\n".join(beat.text for beat in spec.beats),
            **spec.voice.model_dump(exclude={"lead_in", *unset})}


def _gemini(spec: ShortSpec, out_dir: Path) -> Narration:
    """The best of up to TAKES readings. The chosen take is kept (take.wav) and used again while the script and
    voice stay the same, so re-rendering the pictures doesn't change the performance. A finished narration.wav that
    KEPT_NARRATION says was read from this script and voice is used as it is, with no Gemini call."""
    script = "\n".join(beat.text for beat in spec.beats)
    spoken = script
    for word, respelled in spec.voice.say.items():
        spoken = spoken.replace(word, respelled)
    tokens = [(text, index) for index, beat in enumerate(spec.beats) for text in beat.text.split()]
    language = WHISPER_LANGUAGE.get(spec.voice.lang_code, "en")
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path, take_path, take_json = out_dir / "narration.wav", out_dir / "take.wav", out_dir / "take.json"
    wanted = narration_key(spec)
    lead_in = spec.voice.lead_in
    saved = json.loads(take_json.read_text(encoding="utf-8")) if take_json.exists() else {}
    # "served_by" names the model that read the kept take; it isn't part of what the take must match. Nor are the
    # speed and the longest pause, which only change how the kept take plays.
    served = saved.pop("served_by", "")
    played_as = ("speed", "max_pause")
    kept = take_path.exists() and {k: v for k, v in saved.items() if k not in played_as} == \
        {k: v for k, v in wanted.items() if k not in played_as}
    finished = out_dir / KEPT_NARRATION
    if not kept and wav_path.exists() and finished.exists() and \
            json.loads(finished.read_text(encoding="utf-8")) == wanted:
        audio = sf.read(wav_path, dtype="float32")[0]
        words, coverage = _align(tokens, _heard(wav_path, language))
        log.info("%s: the kept narration; Whisper heard %s of each line", spec.id, " ".join(f"{c:.0%}" for c in coverage))
        if min(coverage) < MIN_COVERAGE:
            log.warning("%s: in the kept narration Whisper heard only %.0f%% of one line", spec.id, 100 * min(coverage))
        return _narration(spec, out_dir, wav_path, audio, words, coverage, language)
    best: tuple[np.ndarray, np.ndarray, list[Word], list[float], str] | None = None
    for take in range(1, TAKES + 1):
        if kept and take == 1:
            raw = sf.read(take_path, dtype="float32")[0]
        else:
            # Later takes stay on the model that read the first, so the best of them is chosen on delivery alone.
            try:
                raw, served = _gemini_take(spoken, spec.voice, [served] if served and take > 1 else None)
            except RuntimeError as error:
                if best is None:
                    raise
                log.warning("%s: no take %d (%s); keeping the best so far", spec.id, take, error)
                break
        played = _tighten(_tempo(raw, spec.voice.speed), spec.voice.max_pause)
        head, tail = _speech_bounds(played)
        audio = np.concatenate([np.zeros(int(lead_in * SAMPLE_RATE), dtype=np.float32), played[head:tail]])
        sf.write(wav_path, audio, SAMPLE_RATE)
        heard = _heard(wav_path, language)
        words, coverage = _align(tokens, heard)
        start, end = _unscripted(tokens, heard)
        log.info("%s take %d (%s): Whisper heard %s of each line; the longest stretch not in the script is %.1f s",
                 spec.id, take, served or "model not recorded", " ".join(f"{c:.0%}" for c in coverage), end - start)
        if end - start > MAX_UNSCRIPTED:
            log.warning("%s: take %d says things that aren't in the script at %.0f-%.0f s; not using it", spec.id,
                        take, start, end)
            continue
        if best is None or min(coverage) > min(best[3]):
            best = (raw, audio, words, coverage, served)
        if min(coverage) >= MIN_COVERAGE:
            break
    if best is None:
        raise RuntimeError(f"{spec.id}: every take said things that aren't in the script")
    raw, audio, words, coverage, served = best
    if min(coverage) < MIN_COVERAGE:
        log.warning("%s: in the best of %d takes Whisper heard only %.0f%% of one line", spec.id, TAKES, 100 * min(coverage))
    sf.write(wav_path, audio, SAMPLE_RATE)
    sf.write(take_path, raw, SAMPLE_RATE)
    take_json.write_text(json.dumps({**wanted, "served_by": served}, ensure_ascii=False, indent=1), encoding="utf-8")
    return _narration(spec, out_dir, wav_path, audio, words, coverage, language)


def _narration(spec: ShortSpec, out_dir: Path, wav_path: Path, audio: np.ndarray, words: list[Word],
               coverage: list[float], language: str) -> Narration:
    lead_in = spec.voice.lead_in
    # Whisper stamps the first word from the start of the file, inside the lead-in silence.
    words = [Word(w.text, max(w.start, lead_in), max(w.end, lead_in + 0.05), w.beat) for w in words]
    # check.speech's English recognizer can't judge Hindi; this takes its place (check._saved_speech).
    if language != "en":
        (out_dir / "speech.json").write_text(json.dumps(speech_report(coverage), indent=2), encoding="utf-8")
    duration = len(audio) / SAMPLE_RATE
    starts = [min(w.start for w in words if w.beat == i) for i in range(len(spec.beats))]
    cuts = [lead_in, *(max(s - CUT_LEAD, prev + 0.2) for prev, s in zip(starts, starts[1:])), duration]
    return Narration(wav_path, duration, words, list(zip(cuts, cuts[1:])))


def synthesize(spec: ShortSpec, out_dir: Path) -> Narration:
    if spec.voice.engine == "gemini":
        return _gemini(spec, out_dir)
    if spec.voice.engine != "kokoro":
        raise ValueError(f"unsupported TTS engine: {spec.voice.engine}")
    pipeline = _pipeline(spec.voice.lang_code)

    clips = []
    for beat in spec.beats:
        results = pipeline(beat.text, voice=spec.voice.voice, speed=spec.voice.speed)
        clips.append([[r.audio.numpy().astype(np.float32), r.tokens or []] for r in results])
    # The Short loops, so Kokoro's silence before the first word and after the last would add up at the seam.
    first, last = clips[0][0], clips[-1][-1]
    head, _ = _speech_bounds(first[0])
    _, tail = _speech_bounds(last[0])
    # Tail first: when one clip is both first and last, cutting the head would move the tail index.
    last[0] = last[0][:tail]
    first[0] = first[0][head:]

    pieces = [np.zeros(int(spec.voice.lead_in * SAMPLE_RATE), dtype=np.float32)]
    t = spec.voice.lead_in
    words: list[Word] = []
    spans: list[tuple[float, float]] = []
    for index, beat in enumerate(spec.beats):
        beat_start = t
        for audio, tokens in clips[index]:
            shift = head / SAMPLE_RATE if audio is first[0] else 0.0
            words.extend(_words(tokens, t - shift, index))
            pieces.append(audio)
            t += len(audio) / SAMPLE_RATE
        pieces.append(np.zeros(int(beat.pause_after * SAMPLE_RATE), dtype=np.float32))
        t += beat.pause_after
        spans.append((beat_start, t))

    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / "narration.wav"
    sf.write(wav_path, np.concatenate(pieces), SAMPLE_RATE)
    return Narration(wav_path, t, words, spans)
