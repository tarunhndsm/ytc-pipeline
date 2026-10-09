"""Make a Short with no one at the keyboard: research, script, pictures, render, checks, a look at the result
with fixes, and a hosted copy that waits for a Buffer slot.

Each stage saves its result in the episode folder, so a run that stops (out of quota, out of time) picks the
episode up where it left off.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from PIL import Image

from . import llm, pick, writer
from .research import research as do_research

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
EPISODES = ROOT / "content" / "episodes"
REJECTED = ROOT / "content" / "rejected"
IST = ZoneInfo("Asia/Kolkata")
# Reads a format 1 Short only when no Gemini take is usable.
VOICE = {"voice": "af_heart", "speed": 1.05}
# The narrator of format 2 (strategy/LEARNINGS.md, 2026-10-07): Gemini's Zubenelgenubi, a casual American man,
# told to talk like someone telling a friend. A listening model, given the same script read by five voices and
# the channel's old narrators in two different orders, ranked it first both times; the old documentary Sulafat
# came last ("overly methodical and slow") and Kokoro next to last ("formal and dry"). Its own pace is quick, so
# it plays only 1.05 times as fast, with the pauses between lines cut to a quarter second.
GEMINI_VOICE = {"engine": "gemini", "voice": "Zubenelgenubi", "language": "en-US", "speed": 1.05, "max_pause": 0.25,
                "direction": "fast, energetic, conversational American storyteller telling a friend a true story they "
                             "can hardly believe; punchy and playful, natural rises and falls, never an announcer or "
                             "documentary voice"}
# A format 2 Short runs this long, in seconds (strategy/SCRIPT-RULES.md: 22-32 s aimed for).
DURATION = (20, 40)
# Format 2's music bed (bed.py) by series: a mood that fits the kind of story.
MOODS = {"Wait, That Happened?": "chase", "History's Luckiest People": "wonder", "History's Unluckiest People": "mystery",
         "Hoaxes That Fooled Everyone": "mystery", "Trials You Won't Believe": "chase",
         "Bad Ideas That Seemed Good": "wonder", "History Got It Wrong": "chase"}
# What tts raises when no Gemini model would give a usable take: out of daily quota, every take strayed, or a TTS
# model refused the request (its URL is in the HTTP error).
GEMINI_FAILED = ("no Gemini TTS model read the script", "every take said things that aren't in the script",
                 "-tts:generateContent")
GATE = ["hook", "clarity", "payoff", "visuals", "loop"]
PASS_SCORE = 4
FIX_ROUNDS = 2
MAX_ATTEMPTS = 3
# Set per job (auto.LAST_RESORT_BELOW) when so few Shorts are ready that the channel would soon post nothing.
LAST_RESORT = False

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {"type": "object", "properties": {k: {"type": "integer"} for k in GATE}, "required": GATE},
        "notes": {"type": "object", "properties": {k: {"type": "string"} for k in GATE}, "required": GATE},
        "frames": {"type": "array", "items": {"type": "object", "properties": {
            "beat": {"type": "integer"}, "problem": {"type": "string"}}, "required": ["beat", "problem"]}},
        "speech": {"type": "array", "items": {"type": "object", "properties": {
            "beat": {"type": "integer"}, "word": {"type": "string"}, "respelling": {"type": "string"}},
            "required": ["beat", "word", "respelling"]}},
        "rewrite": {"type": "array", "items": {"type": "string"}},
        "better_than_last": {"type": "string"},
    },
    "required": ["scores", "notes", "frames", "speech", "rewrite", "better_than_last"],
}

REVIEW_PROMPT = """You are the quality gate for Days of Odd, a YouTube Shorts channel of strange-but-true
history. Judge this rendered Short as a demanding editor would. Below are the script with each beat's timing,
one frame from the middle of each shot (what viewers see, captions included; a long beat can cut to a close-up,
so it has two), and the automatic checks.

Score 1 to 5 (5 excellent, 4 good enough to publish, 3 or lower must be fixed), with a one-line note each:
- hook: would a stranger stop swiping within the first 2 seconds? The first line must state the story's most
  surprising fact plainly, with concrete stakes. Score it 3 or lower if it is a riddle that hides who did what
  ("An actor saved the president's son right before his brother struck" hides that the brother of Lincoln's
  assassin saved Lincoln's son), if it opens with a year or a date, if it names nothing a stranger can picture
  ("an English town", a title nobody knows), if beat 2 is background rather than a stranger fact, or if a
  later beat holds a more surprising fact than the first line; for a hook of 3 or lower, the first rewrite
  change is a new first line.
- clarity: can someone who knows nothing about it follow on one listen? One idea per beat, in plain spoken
  English a 14-year-old would follow, with nothing that slows the story down.
- payoff: is there a real surprise or twist near the end that pays off the hook?
- visuals: is every frame on-topic, clear at phone size, and striking?
- loop: does the last line run straight into the first, so a replay feels seamless? A last line that repeats
  the first scores 2: the replay says it twice in a row.

Also:
- frames: each beat whose frame has a problem (off-topic, anachronistic, gore, nudity, a big watermark,
  mostly text, the subject cropped out, blurry, or the same picture as another beat), with the problem.
  Leave out beats that are fine. {loop_note}
- speech: the recognizer's differences are listed. For each real mispronunciation (not another spelling of
  a correctly spoken name, not a skipped short word), give the beat, the word exactly as written in the
  beat, and a respelling in plain lowercase words that a text-to-speech voice would read correctly
  (Formosus: "for moe sus").
- rewrite: if hook, clarity, payoff, or loop is under 4, or the duration is outside {low}-{high} s, up to 4 concrete
  changes (which beat, what to say instead), using only facts already in the script. Otherwise empty.
- better_than_last: one sentence on what this Short does better than the recent ones listed, or "" if nothing.

Recent Shorts and their scores:
{recent}
"""


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def next_id() -> str:
    numbers = [int(p.name[2:]) for folder in (EPISODES, REJECTED) if folder.exists()
               for p in folder.glob("ep[0-9][0-9][0-9]") if p.is_dir()]
    return f"ep{max(numbers, default=0) + 1:03d}"


def _clean(visual: dict) -> dict:
    return {k: v for k, v in visual.items() if not k.startswith("_")}


def _voice(meta: dict) -> dict:
    """The episode's voice, with the respellings the reviewer asked of a Gemini voice."""
    gemini = meta["voice"].get("engine") == "gemini"
    return meta["voice"] | ({"say": meta["say"]} if gemini and meta.get("say") else {})


def spec_from(script: dict, visuals: list[dict], research: dict, episode_id: str, voice: dict = VOICE,
              fmt: int = 1) -> dict:
    cited = [c for beat in script["beats"] for c in beat.get("claims", [])]
    labels = [s for n in dict.fromkeys(cited) if 1 <= n <= len(research["claims"]) for s in research["claims"][n - 1]["sources"]]
    by_label = {s["label"]: s["url"] for s in research["sources"]}
    beats = []
    for beat, visual in zip(script["beats"], visuals):
        item = {"text": beat["text"], "emphasis": beat["emphasis"]}
        if beat.get("sfx", "none") != "none":
            item["sfx"] = beat["sfx"]
        if abs(beat.get("pause_after", 0.12) - 0.12) > 0.001:
            item["pause_after"] = beat["pause_after"]
        item["visual"] = _clean(visual)
        beats.append(item)
    spec = {
        "id": episode_id,
        "title": script["title"],
        "series": research["series"],
        "description": script["description"],
        "sources": list(dict.fromkeys(by_label[l] for l in labels if l in by_label)),
        "hashtags": script["hashtags"],
        "tags": script["tags"],
        "voice": voice,
        "beats": beats,
    }
    if script.get("hook_text"):
        spec["hook_text"] = script["hook_text"]
    if fmt >= 2:
        spec["look"] = "bold"
        spec["music"] = {"path": f"bed:{MOODS.get(research['series'], 'mystery')}", "gain_db": -17.0, "fade_out": 1.0}
        spec["captions"] = {"hook_size": 150}
    return spec


def _save_spec(folder: Path, spec: dict) -> Path:
    path = folder / "short.yaml"
    path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    return path


def contact_sheet(folder: Path, frames: list[Path]) -> Path:
    width, per_row = 180, 6
    tiles = [Image.open(f).convert("RGB") for f in frames if f.exists()]
    tiles = [t.resize((width, round(t.height * width / t.width))) for t in tiles]
    height = max((t.height for t in tiles), default=320)
    rows = (len(tiles) + per_row - 1) // per_row or 1
    sheet = Image.new("RGB", (width * min(per_row, max(len(tiles), 1)), height * rows), "black")
    for i, tile in enumerate(tiles):
        sheet.paste(tile, ((i % per_row) * width, (i // per_row) * height))
    path = folder / "sheet.jpg"
    sheet.save(path, quality=78)
    return path


def recent_scores(count: int = 3, exclude: str = "") -> str:
    lines = []
    for folder in sorted(EPISODES.glob("ep[0-9][0-9][0-9]"), reverse=True):
        if folder.name == exclude or not ((folder / "publish.json").exists() or (folder / "hold.json").exists()):
            continue
        spec = yaml.safe_load((folder / "short.yaml").read_text(encoding="utf-8"))
        text = (folder / "research.md").read_text(encoding="utf-8") if (folder / "research.md").exists() else ""
        match = re.search(r"\| Hook \| Clarity \| Payoff \| Visuals \| Loop \|\n\|[-| ]+\|\n\|([^\n]+)\|", text)
        scores = match.group(1).replace(" ", "") if match else "unscored"
        lines.append(f"- {folder.name} \"{spec['title']}\": hook|clarity|payoff|visuals|loop = {scores}; "
                     f"beat 1: \"{spec['beats'][0]['text']}\"")
        if len(lines) == count:
            break
    return "\n".join(lines) or "- none yet"


def _loop_reuse(script: dict, visuals: list[dict]) -> bool:
    return bool(script.get("loop")) and len(visuals) > 1 and visuals[-1].get("reuse") == 1


def review(folder: Path, script: dict, result: dict, episode_id: str, visuals: list[dict]) -> dict:
    manifest = _read(folder / "work" / "manifest.json")
    last = len(script["beats"])
    loop = _loop_reuse(script, visuals)
    reuse_notes = []
    for n, visual in enumerate(visuals, start=1):
        if loop and n == last:
            reuse_notes.append(f"Beat {n} deliberately shows beat 1's picture again so the replay is seamless: "
                               f"never flag beat {n}'s frame.")
        elif visual.get("reuse"):
            reuse_notes.append(f"Beat {n} deliberately shows beat {visual['reuse']}'s picture again, framed differently, "
                               f"because no better picture was found: flag it only if the picture doesn't fit what beat {n} says.")
        elif visual.get("source") == "card":
            reuse_notes.append(f"Beat {n} is a designed title card (a date, number, or quote on a dark background), chosen "
                               f"on purpose: flag it only if it is hard to read or doesn't fit what beat {n} says.")
    parts: list = [REVIEW_PROMPT.format(recent=recent_scores(exclude=episode_id), loop_note=" ".join(reuse_notes),
                                        low=DURATION[0], high=DURATION[1])]
    parts.append(f"\nTitle: {script['title']}\nDescription: {script['description']}")
    if script.get("hook_text"):
        parts.append(f"On-screen text over beat 1: \"{script['hook_text']}\"")
    for i, (beat, frame) in enumerate(zip(manifest["beats"], result["beats"]), start=1):
        parts.append(f"\nBeat {i} ({beat['start']:.1f}-{beat['end']:.1f} s): \"{beat['text']}\"")
        parts += [Path(f) for f in frame.get("frames", [frame["frame"]]) if Path(f).exists()]
    speech = result.get("speech", {})
    parts.append(
        "\nChecks: duration {d} s, {w} words, loudness {l} LUFS, true peak {p} dBFS; warnings: {warn}; "
        "speech recognizer differences: {diff}".format(
            d=result["duration"], w=result["words"], l=result["integrated_lufs"], p=result["true_peak_dbfs"],
            warn="; ".join(result["warnings"]) or "none",
            diff="; ".join(speech.get("differences", [])) or ("check failed: " + speech["error"] if speech.get("error") else "none"),
        )
    )
    # The gate is a Flash model while plenty of Shorts are ready: drafts from the fallback models are judged to the
    # same standard, and the Short waits for Gemini's reset rather than being passed by a weaker judge. When few are
    # ready (llm.CURSOR_FALLBACK), a strong backup model (llm.JUDGES) or Cursor judges, and a backup passes only a
    # Short it finds nothing to fix in (_passes). Only when the channel is about to run out of Shorts (LAST_RESORT)
    # does Gemma judge instead of nobody.
    judges = llm.FLASH + (llm.JUDGES if llm.CURSOR_FALLBACK else ())
    try:
        verdict = llm.generate(parts, schema=REVIEW_SCHEMA, models=judges, purpose=f"{episode_id} review")
    except (llm.OutOfQuota, llm.Overloaded):
        if not LAST_RESORT:
            raise
        verdict = llm.generate(parts, schema=REVIEW_SCHEMA, models=("gemma-4-31b-it",),
                               purpose=f"{episode_id} review (last resort)")
    verdict["judge"] = llm.answered_by()
    if loop:
        verdict["frames"] = [f for f in verdict["frames"] if f.get("beat") != last]
    return verdict


def _passes(result: dict, verdict: dict, final: bool) -> bool:
    if result["warnings"] or any(verdict["scores"][k] < PASS_SCORE for k in GATE):
        return False
    if final and ":" not in verdict.get("judge", ""):
        # With every score 4 or more the reviewer already judged the visuals good enough to publish; public-domain
        # archives rarely have a picture that shows exactly what every one of nine lines says.
        return len(verdict["frames"]) <= 2 and len(verdict["speech"]) <= 1
    return not verdict["frames"] and not verdict["speech"]


# A round's files, kept while it is the best so far: a fix can make a Short worse, and the final round then
# falls back to the best one if that one would pass.
BEST = "best"
_BEST_FILES = ("script.json", "visuals.json", "short.yaml", "sheet.jpg", "work/manifest.json", "work/speech.json",
               "work/captions.srt", "work/captions.ass")


def _standing(round_: dict) -> tuple:
    """How good a reviewed round is: passing by the final rule first, then its lowest score, total, and frames."""
    scores = round_["scores"]
    passes = _passes({"warnings": round_["check"]["warnings"]}, round_, final=True)
    return (passes, min(scores.values()), sum(scores.values()), -len(round_["frames"]), -len(round_["speech"]))


def _keep_best(folder: Path) -> None:
    best = folder / BEST
    shutil.rmtree(best, ignore_errors=True)
    for name in (*_BEST_FILES, f"{folder.name}.mp4"):
        if (folder / name).exists():
            (best / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(folder / name, best / name)


def _restore_best(folder: Path) -> None:
    best = folder / BEST
    for path in sorted(best.rglob("*")):
        if path.is_file():
            target = folder / path.relative_to(best)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    shutil.rmtree(best)


def _override(text: str, word: str, respelling: str) -> str:
    from .tts import phonemes

    if f"[{word}](" in text:
        return text
    sounds = phonemes(respelling.lower()).replace(" ", "")
    return re.sub(rf"(?<![\w\[]){re.escape(word)}(?!\w)", lambda _: f"[{word}](/{sounds}/)", text, count=1)


renders: list[str] = []
# Shorts made side by side (auto.LANES) render and check one at a time: those share the CPU and the voice and
# speech models.
CPU_LOCK = threading.Lock()


def _render(spec_path: Path) -> Path:
    """On Modal when it's set up and working (or when this is a Modal worker), otherwise on this machine."""
    if (os.environ.get("MODAL_TOKEN_ID") or os.environ.get("YTC_ON_MODAL")) and os.environ.get("YTC_RENDER_HERE") != "1":
        try:
            from .cloud import render

            video = render(spec_path)
            renders.append("modal")
            return video
        except Exception as err:  # quota, network, image build
            if os.environ.get("YTC_ON_MODAL"):
                # A studio worker is sized for waiting on Gemini, not for rendering.
                raise
            log.warning("Modal render failed (%s); rendering on this machine", err)
    from .render import make_short

    video = make_short(spec_path)
    renders.append("runner")
    return video


def _summary(result: dict) -> dict:
    return {k: result.get(k) for k in ("duration", "words", "integrated_lufs", "true_peak_dbfs", "warnings")} | {
        "speech_differences": result.get("speech", {}).get("differences"),
        "speech_error": result.get("speech", {}).get("error"),
        "sources": [b["source"] for b in result["beats"]],
    }


def reject(folder: Path, reason: str) -> Path:
    REJECTED.mkdir(parents=True, exist_ok=True)
    notes = _read(folder / "review.json") or {}
    _write(folder / "review.json", notes | {"verdict": "rejected", "reason": reason,
                                            "rejected_at": datetime.now(IST).isoformat(timespec="seconds")})
    for bulky in (folder / f"{folder.name}.mp4", folder / "work"):
        if bulky.is_dir():
            shutil.rmtree(bulky)
        else:
            bulky.unlink(missing_ok=True)
    target = REJECTED / folder.name
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(folder), target)
    log.warning("%s rejected: %s", folder.name, reason)
    return target


def produce(topic: str, series: str, episode_id: str | None = None, *, at: str | None = None) -> dict:
    """Make one Short (or finish a half-made one) and host it. Returns what happened, for the run's status."""
    from .check import check
    from .publish import hold

    episode_id = episode_id or next_id()
    folder = EPISODES / episode_id
    folder.mkdir(parents=True, exist_ok=True)
    meta = _read(folder / "topic.json") or {"topic": topic, "series": series, "at": at, "format": writer.FORMAT,
                                              "started_at": datetime.now(IST).isoformat(timespec="seconds"), "attempts": 0}
    fmt = meta.get("format", 1)
    if (folder / "hold.json").exists() or (folder / "publish.json").exists():
        return {"id": episode_id, "outcome": "ready", "topic": meta["topic"]}
    meta["attempts"] += 1
    meta.setdefault("voice", dict(GEMINI_VOICE))
    _write(folder / "topic.json", meta)
    if meta["attempts"] > MAX_ATTEMPTS:
        reject(folder, f"still unfinished after {MAX_ATTEMPTS} runs")
        return {"id": episode_id, "outcome": "rejected", "topic": meta["topic"], "reason": "too many attempts"}

    research = _read(folder / "research.json")
    if research is None:
        research = do_research(meta["topic"], meta["series"])
        _write(folder / "research.json", research)
    if not research.get("viable"):
        reason = f"research: {research.get('reason')}"
        reject(folder, reason)
        return {"id": episode_id, "outcome": "rejected", "topic": meta["topic"], "reason": reason}

    script = _read(folder / "script.json")
    if script is None:
        script = writer.write_script(research, episode_id)
        _write(folder / "script.json", script)
    visuals = _read(folder / "visuals.json")
    if visuals is None:
        visuals = pick.pick(script, research, episode_id)
        _write(folder / "visuals.json", visuals)

    notes = _read(folder / "review.json") or {"rounds": []}
    spec_path = _save_spec(folder, spec_from(script, visuals, research, episode_id, _voice(meta), fmt))
    video = folder / f"{episode_id}.mp4"
    shortest, longest = DURATION if fmt >= 2 else (35, 58)
    while True:
        with CPU_LOCK:
            if not video.exists() or video.stat().st_mtime < spec_path.stat().st_mtime:
                try:
                    video = _render(spec_path)
                except Exception as err:
                    if meta["voice"].get("engine") != "gemini" or not any(m in str(err) for m in GEMINI_FAILED):
                        raise
                    if fmt >= 2:
                        # Never Kokoro, which viewers heard as robotic: out of TTS quota, the Short waits for the reset.
                        if "HTTP 429" in str(err):
                            raise llm.OutOfQuota(f"{episode_id}: no Gemini TTS quota left ({err})") from err
                        raise
                    # The voice recorded in short.yaml is the one heard, so the test counts this Short for VOICE.
                    log.warning("%s: no usable Gemini take (%s); narrating with %s", episode_id, err, VOICE["voice"])
                    meta["voice"] = VOICE
                    _write(folder / "topic.json", meta)
                    spec_path = _save_spec(folder, spec_from(script, visuals, research, episode_id, _voice(meta), fmt))
                    video = _render(spec_path)
            result = check(video, shortest=shortest, longest=longest)
            frames = [Path(f) for b in result["beats"] for f in b.get("frames", [b["frame"]])]
            contact_sheet(folder, frames)
        verdict = review(folder, script, result, episode_id, visuals)
        final = len(notes["rounds"]) >= FIX_ROUNDS
        passed = _passes(result, verdict, final)
        notes["rounds"].append({"at": datetime.now(IST).isoformat(timespec="seconds"), "check": _summary(result), **verdict,
                                "passed": passed})
        best = notes.get("best_round")
        if best is None or not (folder / BEST).exists() or _standing(notes["rounds"][-1]) > _standing(notes["rounds"][best - 1]):
            _keep_best(folder)
            notes["best_round"] = len(notes["rounds"])
        _write(folder / "review.json", notes)
        log.info("%s review %d: %s%s", episode_id, len(notes["rounds"]), verdict["scores"], " (passed)" if passed else "")
        if passed:
            break
        if final:
            best = notes["best_round"]
            if best != len(notes["rounds"]) and _standing(notes["rounds"][best - 1])[0]:
                log.info("%s: round %d was better and passes; keeping it", episode_id, best)
                _restore_best(folder)
                notes["rounds"][best - 1]["passed"] = True
                notes["kept_round"] = best
                _write(folder / "review.json", notes)
                script, visuals = _read(folder / "script.json"), _read(folder / "visuals.json")
                spec_path = folder / "short.yaml"
                break
            low = [k for k in GATE if verdict["scores"][k] < PASS_SCORE]
            reason = "; ".join(filter(None, [
                f"low scores: {', '.join(low)}" if low else "", "; ".join(result["warnings"]),
                f"{len(verdict['frames'])} frame problems" if len(verdict["frames"]) > 1 else ""]))
            shutil.rmtree(folder / BEST, ignore_errors=True)
            reject(folder, reason or "failed the quality gate")
            return {"id": episode_id, "outcome": "rejected", "topic": meta["topic"], "reason": reason}

        story = ("hook", "clarity", "payoff", "loop")
        rewrite = verdict["rewrite"] or [f"{k}: {verdict['notes'][k]}" for k in story if verdict["scores"][k] < PASS_SCORE]
        if any(verdict["scores"][k] < PASS_SCORE for k in story) or not shortest <= result["duration"] <= longest:
            if not shortest <= result["duration"] <= longest:
                rewrite.append(f"The render ran {result['duration']} s; it must be {shortest}-{longest} s.")
            old_script, script = script, writer.write_script(research, episode_id, feedback=rewrite, draft=script)
            # Beats the rewrite left alone keep their pictures, unless the reviewer flagged them.
            kept = pick.unchanged(old_script, visuals, script, skip={f["beat"] for f in verdict["frames"]})
            visuals = pick.pick(script, research, episode_id, keep=kept)
        else:
            bad = sorted({f["beat"] for f in verdict["frames"] if 1 <= f["beat"] <= len(script["beats"])})
            bad += [n for n, b in enumerate(result["beats"], 1) if b["source"] == "generated gradient" and n not in bad]
            if bad:
                why = "Avoid: " + "; ".join(f"beat {f['beat']}: {f['problem']}" for f in verdict["frames"])
                visuals = pick.replace(script, research, episode_id, visuals, bad, why)
            for fix in verdict["speech"][:3]:
                n = fix["beat"]
                if not 1 <= n <= len(script["beats"]):
                    continue
                # A Gemini voice would read Kokoro's phoneme markup out; it reads the respelling in place of the word.
                if meta["voice"].get("engine") == "gemini":
                    meta.setdefault("say", {})[fix["word"]] = fix["respelling"]
                else:
                    beat = script["beats"][n - 1]
                    beat["text"] = _override(beat["text"], fix["word"], fix["respelling"])
            _write(folder / "topic.json", meta)
        _write(folder / "script.json", script)
        _write(folder / "visuals.json", visuals)
        spec_path = _save_spec(folder, spec_from(script, visuals, research, episode_id, _voice(meta), fmt))

    shutil.rmtree(folder / BEST, ignore_errors=True)
    verdict = notes["rounds"][notes.get("kept_round", len(notes["rounds"])) - 1]
    write_research_md(folder, research, script, visuals, notes, episode_id)
    held = hold(spec_path, {"scores": verdict["scores"], "anniversary": meta.get("at")})
    return {"id": episode_id, "outcome": "ready", "topic": meta["topic"], "title": script["title"],
            "scores": verdict["scores"], "media_url": held["media_url"], "renders": len(notes["rounds"])}


def write_research_md(folder: Path, research: dict, script: dict, visuals: list[dict], notes: dict, episode_id: str) -> None:
    today = datetime.now(IST).date().isoformat()
    final = notes["rounds"][notes.get("kept_round", len(notes["rounds"])) - 1]
    beats_for_claim: dict[int, list[int]] = {}
    for b, beat in enumerate(script["beats"], start=1):
        for c in beat.get("claims", []):
            beats_for_claim.setdefault(c, []).append(b)
    models = sorted({c["model"] for c in llm.calls})
    lines = [
        f"# {episode_id} research: {script['title']} ({research['series']})",
        "",
        f"Researched {today} by the cloud studio ({', '.join(models) or 'Gemini'}) from the sources below, read in "
        "full by the studio. Only claims backed by two or more independent sites were allowed into the script.",
        "",
        f"Story: {research.get('story', '')}",
        "",
        "## Sources",
        "",
        "| Key | Source | URL |",
        "|---|---|---|",
        *[f"| {s['label']} | {s['title']} ({s['site']}) | {s['url']} |" for s in research["sources"]],
        "",
        "## Claims table",
        "",
        "| # | Beats | Claim | Sources | Confidence |",
        "|---|---|---|---|---|",
        *[f"| {n} | {', '.join(map(str, beats_for_claim.get(n, []))) or '-'} | {c['claim']} | {', '.join(c['sources'])} | {c['confidence']} |"
          for n, c in enumerate(research["claims"], start=1)],
        "",
        "## Disputed or left out",
        "",
        *([f"- {d}" for d in research.get("disputed", [])] or ["- nothing"]),
        "",
        "## Images",
        "",
    ]
    for n, visual in enumerate(visuals, start=1):
        if visual.get("reuse"):
            why = "for the loop" if n == len(visuals) and script.get("loop") else "no better picture was found"
            lines.append(f"- Beat {n}: beat {visual['reuse']}'s picture again, {why}.")
        elif visual.get("source") == "card":
            card = visual["card"]
            lines.append(f"- Beat {n}: a designed {card['kind']} card, \"{card['big']}\"" + (f" / \"{card['small']}\"" if card.get("small") else ""))
        elif visual.get("_choice"):
            c = visual["_choice"]
            fit = f", judged {visual['_fit']}" if visual.get("_fit") else ""
            lines.append(f"- Beat {n}: {c['title'].removeprefix('File:')} ({c['license']}{fit}), {c.get('page')}")
        else:
            lines.append(f"- Beat {n}: searched `{visual.get('query')}` (not checked by eye)")
    lines += ["", "## Render log", ""]
    for i, r in enumerate(notes["rounds"], start=1):
        ch = r["check"]
        lines.append(
            f"- Render {i}: {ch['duration']} s, {ch['words']} words, {ch['integrated_lufs']} LUFS, "
            f"{ch['true_peak_dbfs']} dBFS true peak, warnings: {'; '.join(ch['warnings']) or 'none'}; speech differences: "
            f"{'; '.join(ch['speech_differences'] or []) or ch.get('speech_error') or 'none'}. Scores "
            + ", ".join(f"{k} {r['scores'][k]}" for k in GATE)
            + (f". Fixes asked: {'; '.join(r['rewrite'] + [f['problem'] for f in r['frames']] + [s['word'] for s in r['speech']])}"
               if not r["passed"] else ". Passed.")
        )
    if kept := notes.get("kept_round"):
        lines.append(f"- Published render {kept}: the fixes after it scored lower.")
    lines += [
        "",
        f"## Quality gate (cloud studio, {today})",
        "",
        "| Hook | Clarity | Payoff | Visuals | Loop |",
        "|---|---|---|---|---|",
        "| " + " | ".join(str(final["scores"][k]) for k in GATE) + " |",
        "",
        *[f"- {k.capitalize()}: {final['notes'][k]}" for k in GATE],
        "",
        f"Better than the last: {final.get('better_than_last') or script.get('better_than_last', '')}",
        "",
    ]
    (folder / "research.md").write_text("\n".join(lines), encoding="utf-8")
