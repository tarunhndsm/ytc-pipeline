"""The Instagram studio for @ankahi.kathayein: Hindi Reels of lesser-known, sourced stories from the Ramayana, the
Mahabharata and the Puranas. Every Reel is judged against the one before it and published only if it is at least
as good (bar), so quality can only ratchet up.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import check, ff, instagram, llm
from .spec import ShortSpec

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
REELS = ROOT / "content" / "instagram"
IST = ZoneInfo("Asia/Kolkata")
# What Instagram rewards (watch time and replays, sends to friends, likes), then what the page owes its audience.
GATE = ["hook", "retention", "shareability", "payoff", "ending", "narration", "visuals", "accuracy"]
# Reviews that scored a criterion under its old name: "loop" judged how the last line led back into the first.
RENAMED = {"ending": "loop"}
PASS_SCORE = 7
# A Reel runs until its story ends, up to this many seconds.
LONGEST = 90
REVIEW = "review.json"
# Independent judgments averaged per Reel: one Gemini review of the same Reel can move by 5 points.
RUNS = 2
# A new Reel must reach the mean total of this many recent Reels. Held to the last Reel's total alone, one lucky
# review raised the bar for every Reel after it.
BAR_REELS = 5
# Every review is judged by this one model, because totals from different models aren't comparable and each Reel
# is held to the recent ones' totals. (ak001 and ak003 were judged by other Flash models before it was fixed.)
JUDGE = "gemini-3-flash-preview"
# What each judgment keeps; the verdict's scores are their means.
RUN_KEYS = ("judge", "scores", "notes", "frames", "speech", "rewrite", "better_than_last", "worse_than_last", "next_time")
# How each shot moves, as the reviewer is told (it sees one still frame per shot).
SHOT_MOTION = {"depth": "a picture moving in depth, near parts against far ones",
               "camera": "a slow camera move over a flat picture", "video": "video footage"}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {"type": "object", "properties": {k: {"type": "integer"} for k in GATE}, "required": GATE},
        "notes": {"type": "object", "properties": {k: {"type": "string"} for k in GATE}, "required": GATE},
        "frames": {"type": "array", "items": {"type": "object", "properties": {
            "beat": {"type": "integer"}, "problem": {"type": "string"}}, "required": ["beat", "problem"]}},
        "speech": {"type": "array", "items": {"type": "object", "properties": {
            "beat": {"type": "integer"}, "word": {"type": "string"}, "problem": {"type": "string"}},
            "required": ["beat", "word", "problem"]}},
        "rewrite": {"type": "array", "items": {"type": "string"}},
        "better_than_last": {"type": "string"},
        "worse_than_last": {"type": "string"},
        "next_time": {"type": "string"},
    },
    "required": ["scores", "notes", "frames", "speech", "rewrite", "better_than_last", "worse_than_last", "next_time"],
}

REVIEW_PROMPT = """You are the quality gate for अनकही कथाएँ (@ankahi.kathayein), an Instagram page of lesser-known,
sourced stories from the Ramayana, the Mahabharata and the Puranas, told in Hindi to Indian families. Judge this Reel
as a demanding editor who knows what Instagram rewards: watch time and replays, sends to friends, then likes.
Below are the script with each line's timing, one frame from the middle of each shot (what viewers see, captions
included; a long line can cut to a close-up, so it has two), the automatic checks, and the audio of the Reel.

The page runs a quiz from Reel to Reel, which its viewers expect, since comments carry a Reel on Instagram: a line
marked "quiz answer" gives the answer to the question the previous Reel asked, over that Reel's first picture, and
the line marked "quiz question" asks this Reel's question for the comments over line 1's picture, promising the
answer and a shout-out in the next Reel. Judge those lines on being short, clear and inviting (under retention and
accuracy); judge payoff and ending on the story's last line, the one before the question. A title with "भाग" is one
part of a series: it must still feel whole, ending on a turn that makes viewers want the next part.

Score each 1 to 10 (10 exceptional, 8 strong, 7 publishable, 6 or lower must be fixed), with a one-line note:
- hook: in the first 2 seconds (spoken line, on-screen text, first frame), would a devotee scrolling past stop? On
  YouTube, Reels that opened on a grey sculpture, a flower or a snake, after half a second of silence, lost half to
  three quarters of viewers in the first seconds; one that opened on a vivid painting of Ravana with "रावण का असली
  नाम… रावण नहीं था।" drew 65,000 plays.
- retention: does every line make the viewer want the next, with no slack, so people watch to the end?
- shareability: would viewers send this to family or friends? Is the revelation worth passing on?
- payoff: does the ending deliver a real revelation that pays off the hook?
- ending: does the story's last line finish the story in a complete sentence, so it feels whole to someone who
  watches it once, with the voice and music coming to a natural close rather than stopping mid-thought?
- narration: listen. Does it sound like a real, warm Indian storyteller speaking natural Hindi? Name any
  mispronounced word, odd pause, flat or robotic moment.
- visuals: is every frame on-topic, sharp at phone size, striking, and different from the others? Instagram won't
  monetize "content that primarily displays static images played in succession": going by each line's shots,
  does the Reel move like film, or is it a slideshow?
- accuracy: is every claim faithful to the named sources, respectful to the deities and traditions, free of
  politics and of anything that would offend a devotee? Instagram limits the money from religion told in a
  polarizing way, and from death, injury or destruction unless told in an explicitly uplifting way.

Also:
- frames: each line whose frame has a problem (off-topic, blurry, the subject cropped out, nudity, gore, a big
  watermark, the same picture as another line without reason), with the problem. Leave out lines that are fine.
- speech: each word the narrator gets wrong, with its line, the word as written, and what is wrong.
- rewrite: if any score is under 7, up to 4 concrete changes (which line, what instead), using only the facts
  in the script and sources. Otherwise empty.
- better_than_last and worse_than_last: compared with the previous Reel below, what this one does better and
  worse, one sentence each ("" if there is no previous Reel).
- next_time: the single change that would most improve the next Reel.

{previous}
"""


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def score(scores: dict, key: str) -> float:
    return scores[key] if key in scores else scores[RENAMED[key]]


def total(verdict: dict) -> float:
    return round(sum(score(verdict["scores"], k) for k in GATE), 1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _earlier_runs(verdict: dict | None, video_sha: str) -> list[dict]:
    """The judgments already made of this exact video, which a new review adds to instead of replacing."""
    if not verdict or verdict.get("video_sha256") != video_sha:
        return []
    runs = verdict.get("runs") or [{k: verdict[k] for k in RUN_KEYS if k in verdict}]
    return [r for r in runs if r.get("judge") == JUDGE]


def _combine(runs: list[dict]) -> dict:
    """One verdict from several judgments: mean scores, every problem any of them found, the first one's notes."""
    scores = {k: round(sum(score(r["scores"], k) for r in runs) / len(runs), 1) for k in GATE}
    first = runs[0]
    return {"scores": scores, "notes": first["notes"],
            **{key: [item for r in runs for item in r.get(key, [])] for key in ("frames", "speech", "rewrite")},
            "better_than_last": first["better_than_last"], "worse_than_last": first["worse_than_last"],
            "next_time": " / ".join(dict.fromkeys(r["next_time"] for r in runs if r.get("next_time"))),
            "judge": ", ".join(dict.fromkeys(r["judge"] for r in runs)), "runs": runs}


def published() -> list[Path]:
    """The brand's Reels that went to Instagram, oldest first."""
    return sorted((p.parent for p in REELS.glob(f"*/{instagram.RECORD}")),
                  key=lambda f: _read(f / instagram.RECORD)["dueAt"])


def _sets_bar(folder: Path) -> bool:
    """A Reel sent below the bar to keep the queue full (reel_cloud.fill_queue) neither lowers the bar nor is
    the Reel the next one is compared with."""
    return not (_read(folder / instagram.RECORD) or {}).get("below_bar")


def previous(exclude: str = "") -> tuple[Path, dict] | None:
    """The last published Reel that has a review: the one the judge compares the next Reel with."""
    for folder in reversed(published()):
        if folder.name != exclude and _sets_bar(folder) and (verdict := _read(folder / REVIEW)):
            return folder, verdict
    return None


def recent(exclude: str = "") -> list[tuple[Path, dict]]:
    """The last BAR_REELS published Reels reviewed by JUDGE, newest first."""
    found = []
    for folder in reversed(published()):
        if folder.name != exclude and _sets_bar(folder) and (verdict := _read(folder / REVIEW)) and \
                verdict.get("judge") == JUDGE:
            found.append((folder, verdict))
            if len(found) == BAR_REELS:
                break
    return found


def _previous_note(exclude: str) -> str:
    found = previous(exclude)
    if not found:
        return "There is no previous Reel yet."
    folder, verdict = found
    spec = ShortSpec.load(folder / "short.yaml")
    scores = ", ".join(f"{k} {score(verdict['scores'], k)}" for k in GATE)
    return (f"The previous Reel, \"{spec.title}\", opened with \"{spec.beats[0].text}\" and scored {scores} "
            f"(total {total(verdict)}). What its reviewer said to do next time: {verdict.get('next_time') or 'nothing'}")


def _audio(video: Path) -> Path:
    """The Reel's sound as the reviewer hears it: mono 24 kHz WAV."""
    wav = video.parent / "work" / "review.wav"
    subprocess.run([ff.exe(), "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "24000", str(wav)],
                   check=True)
    return wav


def _first_frame(video: Path) -> Path:
    frame = video.parent / "work" / "first_frame.jpg"
    subprocess.run([ff.exe(), "-v", "error", "-y", "-ss", "0.04", "-i", str(video), "-frames:v", "1", "-q:v", "3",
                    str(frame)], check=True)
    return frame


class Unfit(Exception):
    """A Reel its automatic checks already fail, so no review could pass it."""


def review(folder: Path, runs: int = RUNS) -> dict:
    """Judge a rendered Reel with a Gemini model that watches the frames and listens to the audio, `runs` times
    independently, keeping earlier judgments of the same video."""
    spec = ShortSpec.load(folder / "short.yaml")
    video = folder / f"{spec.id}.mp4"
    video_sha = sha256(video)
    done = _earlier_runs(_read(folder / REVIEW), video_sha)
    result = check.check(video, longest=LONGEST)
    if result["warnings"]:
        # The judge's daily quota is small; it goes to Reels that can still pass.
        raise Unfit("; ".join(result["warnings"]))
    manifest = _read(folder / "work" / "manifest.json")
    parts: list = [REVIEW_PROMPT.format(previous=_previous_note(spec.id))]
    parts.append(f"\nTitle: {spec.title}\nCaption: {spec.description}\nSources: {', '.join(spec.sources)}")
    if spec.hook_text:
        parts.append(f"On-screen text over line 1: \"{spec.hook_text}\"")
    if spec.answers:
        parts.append(f"The previous Reel asked: \"{spec.answers.question}\" Its answer: \"{spec.answers.answer}\"")
    elif spec.quiz:
        # Told nothing, judges mark such a Reel down on accuracy for a missing quiz answer.
        parts.append("No question from an earlier Reel is waiting for its answer, so this Reel rightly has no quiz "
                     "answer line.")
    if spec.quiz:
        parts.append(f"This Reel's question: \"{spec.quiz.question}\" Its answer: \"{spec.quiz.answer}\", which the "
                     f"source gives as: \"{spec.quiz.evidence}\"")
    parts += ["\nThe first frame, which the feed shows before anyone decides to stay:", _first_frame(video)]
    roles = {"answer": " (quiz answer)", "quiz": " (quiz question)"}
    for i, (beat, frame) in enumerate(zip(manifest["beats"], result["beats"]), start=1):
        shots = "; ".join(SHOT_MOTION.get(s.get("motion") or s["kind"], SHOT_MOTION["camera"]) for s in beat["shots"])
        role = roles.get(spec.beats[i - 1].role, "") if i <= len(spec.beats) else ""
        parts.append(f"\nLine {i}{role} ({beat['start']:.1f}-{beat['end']:.1f} s): \"{beat['text']}\" Shots: {shots}.")
        parts += [Path(f) for f in frame.get("frames", [frame["frame"]]) if Path(f).exists()]
    speech = result.get("speech", {})
    parts.append(
        f"\nChecks: duration {result['duration']} s, loudness {result['integrated_lufs']} LUFS, true peak "
        f"{result['true_peak_dbfs']} dBFS; warnings: {'; '.join(result['warnings']) or 'none'}; lines the speech "
        f"recognizer heard poorly: {'; '.join(speech.get('differences', [])) or 'none'}")
    parts += ["\nThe Reel's audio:", _audio(video)]
    # Only a Gemini model can listen to the narration, so no backup judges it.
    fallback, llm.CURSOR_FALLBACK = llm.CURSOR_FALLBACK, False
    try:
        while len(done) < runs:
            run = llm.generate(parts, schema=REVIEW_SCHEMA, models=(JUDGE,), purpose=f"{spec.id} review")
            done.append({k: run[k] for k in RUN_KEYS if k in run} | {"judge": llm.answered_by()})
            log.info("%s judgment %d: total %s", spec.id, len(done), total(run))
            # Saved after each judgment: the next one can fail on the judge's daily quota.
            _save(folder, done, video_sha, result)
    finally:
        llm.CURSOR_FALLBACK = fallback
    return _save(folder, done, video_sha, result)


def _save(folder: Path, runs: list[dict], video_sha: str, result: dict) -> dict:
    verdict = _combine(runs)
    verdict |= {"total": total(verdict), "video_sha256": video_sha,
                "reviewed_at": datetime.now(IST).isoformat(timespec="seconds"),
                "check": {k: result[k] for k in ("duration", "integrated_lufs", "true_peak_dbfs", "warnings")}}
    (folder / REVIEW).write_text(json.dumps(verdict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return verdict


def bar(exclude: str = "") -> float:
    """The total a new Reel must reach: the mean of the recent Reels' totals."""
    totals = [total(verdict) for _, verdict in recent(exclude)]
    return round(sum(totals) / len(totals), 1) if totals else 0


def judgments(folder: Path) -> int:
    """How many judgments by JUDGE the review of a rendered Reel has of its current video."""
    spec = ShortSpec.load(folder / "short.yaml")
    return len(_earlier_runs(_read(folder / REVIEW), sha256(folder / f"{spec.id}.mp4")))


def passes(verdict: dict, episode_id: str, with_bar: bool = True) -> bool:
    return (not verdict["check"]["warnings"] and all(score(verdict["scores"], k) >= PASS_SCORE for k in GATE)
            and (not with_bar or verdict["total"] >= bar(episode_id)))


def unshippable(folder: Path, with_bar: bool = True) -> str:
    """Why a rendered Reel can't be scheduled, or "" if it can: it passed review as this exact video. Without the
    bar, a Reel whose every score reaches PASS_SCORE passes."""
    spec = ShortSpec.load(folder / "short.yaml")
    verdict = _read(folder / REVIEW)
    if not verdict:
        return "it has no review"
    if verdict.get("video_sha256") != sha256(folder / f"{spec.id}.mp4"):
        return "the video isn't the one that was reviewed"
    if (judged := sum(r.get("judge") == JUDGE for r in verdict.get("runs") or [])) < RUNS:
        return f"its review has {judged} of {RUNS} judgments by {JUDGE}"
    if not passes(verdict, spec.id, with_bar):
        return f"its review ({verdict['total']}) doesn't pass; the bar is {bar(spec.id)}"
    return ""
