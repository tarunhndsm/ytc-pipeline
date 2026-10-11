"""Look at a rendered Reel as a viewer will, and fix what the picture step got wrong before the judge sees it.

A frame from the middle of each line goes to a vision model with the line's words and its picture's title. Captions
it finds over a face move to another height, and a hook box over a face moves down. A picture it finds off the story,
or too poor to make out, gives way to the line's next candidate from the picture step (work/spares.json), or to an
earlier line's picture when none is left. Then the Reel is rendered again with the same narration, for up to ROUNDS
rounds. What each look found and changed is kept in work/qa.json.

    python -m ytc.reel_qa <reel folder>             check, fix and re-render a Reel the pipeline made
    python -m ytc.reel_qa <reel folder> --look      only report what's wrong, for any Reel
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import ff, llm, reel_pictures, reel_tools, reel_writer

log = logging.getLogger(__name__)

ROUNDS = 2
QA = "work/qa.json"
FRAME_WIDTH = 540
HEIGHTS = {"top": reel_pictures.CAPTION_MOVES[1], "middle": reel_pictures.CAPTION_Y,
           "bottom": reel_pictures.CAPTION_MOVES[0]}
# CaptionStyle.hook_y's default: the hook box sits there unless the picture step moved it down.
HOOK_HIGH = 0.15
PROBLEMS = ("none", "captions_on_face", "hook_on_face", "wrong_picture", "poor_picture")
SWAPS = ("wrong_picture", "poor_picture")

CHECK_SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {"type": "object", "properties": {
        "line": {"type": "integer"},
        "problem": {"type": "string", "enum": list(PROBLEMS)},
        "detail": {"type": "string"},
        "captions_to": {"type": "string", "enum": list(HEIGHTS)},
    }, "required": ["line", "problem", "detail", "captions_to"]}}},
    "required": ["lines"],
}

CHECK_PROMPT = """You check a Reel of अनकही कथाएँ (stories from the Ramayana, the Mahabharata and the Puranas, told in
Hindi on Instagram) before it goes out. Below is a frame from the middle of each line as a phone shows it, with the
words spoken over it and the title of its picture. The white and yellow words are the captions; on line 1 the box of
text is the Reel's hook.

For each line give the worst problem a viewer would notice, or none:
- captions_on_face: the captions cross a face's eyes, nose or mouth. Captions over a beard, a body, clothes, an
  animal, rocks, or the lower edge of a picture shown in a frame are expected and not a problem.
- hook_on_face: (line 1 only) the hook's box crosses a face's eyes, nose or mouth.
- wrong_picture: the picture plainly shows a different deity, person, creature or scene than the words are about (the
  title helps), a deity for a person or a person for a deity, a figure of another faith for the story's (a Jain monk
  for a rishi, a Tibetan Buddhist Yama), or art from outside South Asia (a European artist's view of India counts).
- poor_picture: too blurred, dark, damaged or small to make out, or a large watermark or writing across it.
Not problems: a painting's style, a picture that sets the scene without showing the exact moment, a figure seen from
afar.{repeats}
detail: the problem in a few words, or "".
captions_to: where on this frame the captions would cover no face: top, middle or bottom (where they are now when
nothing covers a face).
"""


def _frame(video: Path, at: float, out: Path) -> bytes:
    subprocess.run([ff.exe(), "-v", "error", "-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1",
                    "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "3", str(out)], check=True)
    return out.read_bytes()


def _spec(folder: Path) -> dict:
    return yaml.safe_load((folder / "short.yaml").read_text(encoding="utf-8"))


def check(folder: Path) -> list[dict]:
    """Each line's worst problem in the rendered Reel, as the model sees it: one dict per line, in order."""
    spec = _spec(folder)
    manifest = json.loads((folder / "work" / "manifest.json").read_text(encoding="utf-8"))
    last = len(spec["beats"])
    repeats = [f"line {n} shows line {earlier}'s picture again on purpose" + (", to close the loop" if n == last else "")
               for n, beat in enumerate(spec["beats"], start=1) if (earlier := beat.get("visual", {}).get("reuse"))]
    parts: list = [CHECK_PROMPT.format(repeats=f" In this Reel {'; '.join(repeats)}." if repeats else "")]
    with tempfile.TemporaryDirectory() as tmp:
        for number, beat in enumerate(manifest["beats"], start=1):
            shot = max(beat.get("shots") or [beat], key=lambda s: s["end"] - s["start"])
            frame = _frame(folder / f"{manifest['id']}.mp4", (shot["start"] + shot["end"]) / 2, Path(tmp) / f"{number}.jpg")
            title = (beat.get("asset") or {}).get("title") or ""
            parts += [f"\nLine {number}: \"{beat['text']}\"" + (f" (picture: {title[:90]})" if title else ""), frame]
    with reel_writer.gemini_only():
        answer = llm.generate(parts, schema=CHECK_SCHEMA, models=reel_writer.WRITERS, purpose=f"{manifest['id']} qa")
    found = {item["line"]: item for item in answer["lines"]}
    return [found.get(n) or {"line": n, "problem": "none", "detail": "", "captions_to": "middle"}
            for n in range(1, len(manifest["beats"]) + 1)]


def _face_spans(spec: dict, chosen: dict[int, dict], number: int) -> list[tuple[float, float]] | None:
    """Where the faces the picture step found are on the screen for this line, or None for a card or no record."""
    own = spec["beats"][number - 1]["visual"].get("reuse") or number
    crop = spec["beats"][own - 1]["visual"].get("crop")
    if own not in chosen or not crop:
        return None
    return reel_pictures._spans(chosen[own].get("faces", []), crop)


def _move_captions(spec: dict, number: int, wanted: str, spans: list[tuple[float, float]] | None) -> str | None:
    beat = spec["beats"][number - 1]
    now = beat.get("caption_y") or reel_pictures.CAPTION_Y
    hook_low = spec["captions"].get("hook_y", HOOK_HIGH) >= reel_pictures.HOOK_LOW
    # Line 1's captions keep out of the hook's box.
    barred = {"middle" if hook_low else "top"} if number == 1 else set()
    names = [name for name in dict.fromkeys([wanted, "bottom", "top", "middle"])
             if name in HEIGHTS and name not in barred and abs(HEIGHTS[name] - now) > 0.05]
    if spans is not None:
        # The model's pick first, unless the faces the picture step found say another height is clear and it isn't.
        reach = reel_pictures.CAPTION_REACH
        names.sort(key=lambda name: not reel_pictures._clear(spans, HEIGHTS[name] - reach, HEIGHTS[name] + reach))
    if not names:
        return None
    if names[0] == "middle":
        beat.pop("caption_y", None)
    else:
        beat["caption_y"] = HEIGHTS[names[0]]
    return f"line {number}: captions to the {names[0]}"


def _move_hook(spec: dict) -> str:
    if spec["captions"].get("hook_y", HOOK_HIGH) >= reel_pictures.HOOK_LOW:
        spec["captions"].pop("hook_y", None)
        return "line 1: hook box to the top"
    spec["captions"]["hook_y"] = reel_pictures.HOOK_LOW
    first = spec["beats"][0]
    if (first.get("caption_y") or reel_pictures.CAPTION_Y) < HEIGHTS["bottom"]:
        first["caption_y"] = HEIGHTS["bottom"]
    return "line 1: hook box lower, captions to the bottom"


def _swap(folder: Path, spec: dict, lines: list[int], chosen: dict[int, dict],
          spares: dict[int, list[dict]]) -> dict[int, str]:
    """Give each line its next spare picture, or failing that an earlier line's picture again; the change by line."""
    beats, last = spec["beats"], len(spec["beats"])
    in_use = {hit["ref"] for hit in chosen.values()}
    pools = {n: [s for s in spares.get(n, []) if s["ref"] not in in_use] for n in lines}
    got = {}
    if tried := {n: pool for n, pool in pools.items() if pool}:
        with reel_writer.gemini_only():
            got = reel_pictures.frame_all(beats, tried, {n: [] for n in tried}, f"{folder.name} qa framing")
    changes = {}
    for number in lines:
        old = chosen.get(number, {}).get("title") or f"line {beats[number - 1]['visual'].get('reuse')}'s picture"
        if hit := got.get(number):
            chosen[number] = hit
            reel_pictures.place(spec, number, hit)
            for later, beat in enumerate(beats, start=1):
                if beat["visual"].get("reuse") == number and later != last:
                    reel_pictures.reuse(spec, later, number)
            changes[number] = f"line {number}: {old[:60]} gave way to {hit['title'][:60]}"
            continue
        reused = sum(1 for n, beat in enumerate(beats, start=1) if "reuse" in beat["visual"] and n != last)
        if number == 1 or "reuse" in beats[number - 1]["visual"] or reused >= reel_pictures.MAX_REUSED:
            log.info("line %d: no spare picture left, and it can't show an earlier one", number)
            continue
        own = [n for n in range(1, number) if "reuse" not in beats[n - 1]["visual"]]
        shows = {n: beat["visual"].get("reuse") or n for n, beat in enumerate(beats, start=1)}
        earlier = reel_pictures.least_run(own, shows, number) or max(own)
        chosen.pop(number, None)
        reel_pictures.reuse(spec, number, earlier)
        for later, beat in enumerate(beats, start=1):
            if beat["visual"].get("reuse") == number:
                reel_pictures.reuse(spec, later, earlier)
        changes[number] = f"line {number}: {old[:60]} dropped; shows line {earlier}'s picture again"
    in_use = {hit["ref"] for hit in chosen.values()}
    for number in spares:
        spares[number] = reel_pictures.spares_for(number, spares[number], in_use)
    return changes


def fix(folder: Path, problems: list[dict]) -> list[str]:
    """Change the Reel's short.yaml for what check found; returns the changes, empty when none could be made."""
    if not (folder / reel_pictures.SPARES).exists():
        raise SystemExit(f"{folder.name} wasn't made by the pipeline: it has no {reel_pictures.SPARES}")
    chosen = {int(n): hit for n, hit in json.loads((folder / reel_pictures.PICTURES).read_text(encoding="utf-8")).items()}
    spares = {int(n): hits for n, hits in json.loads((folder / reel_pictures.SPARES).read_text(encoding="utf-8")).items()}
    spec = _spec(folder)
    last = len(spec["beats"])
    # The loop line shows line 1's picture on purpose: a mismatch there isn't line 1's picture's fault.
    swaps = sorted({p["line"] for p in problems if p["problem"] in SWAPS and p["line"] != last})
    swapped = _swap(folder, spec, swaps, chosen, spares) if swaps else {}
    changes = list(swapped.values())
    if any(p["problem"] == "hook_on_face" and p["line"] == 1 for p in problems) and 1 not in swapped:
        changes.append(_move_hook(spec))
    for problem in problems:
        if problem["problem"] == "captions_on_face" and problem["line"] not in swapped:
            spans = _face_spans(spec, chosen, problem["line"])
            if change := _move_captions(spec, problem["line"], problem["captions_to"], spans):
                changes.append(change)
    if changes:
        reel_pictures.save(folder, spec, chosen, spares)
    return changes


def _record(folder: Path, record: dict) -> None:
    (folder / QA).write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def run(folder: Path, rounds: int = ROUNDS) -> dict:
    """Check the render and fix it, rendering again after each fix, for up to `rounds` fixes."""
    # Saved after each fix as well; "finished" tells the factory a run cut short before its last look isn't done.
    record: dict = {"rounds": [], "finished": False}
    for turn in range(rounds + 1):
        problems = [p for p in check(folder) if p["problem"] != "none"]
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": llm.answered_by(),
                 "problems": problems}
        record["rounds"].append(entry)
        record["left"] = problems
        if not problems or turn == rounds:
            break
        entry["changes"] = fix(folder, problems)
        _record(folder, record)
        if not entry["changes"]:
            break
        log.info("%s: %s; rendering again", folder.name, "; ".join(entry["changes"]))
        reel_tools.render(folder / "short.yaml")
    record["finished"] = True
    _record(folder, record)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ytc.reel_qa", description="check a rendered Reel's frames and fix them")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--look", action="store_true", help="only report what's wrong; change nothing")
    args = parser.parse_args()

    from .cli import PIPELINE_ROOT, _load_env

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _load_env(PIPELINE_ROOT / ".env")
    llm.use_brand_project()
    os.environ.setdefault("HF_HOME", reel_tools.MODEL_CACHE)
    folder = args.folder.resolve()
    result = {"lines": check(folder)} if args.look else run(folder)
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
