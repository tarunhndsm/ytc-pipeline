"""A Reel the judge failed on its pictures alone still goes out, without the pictures it found fault with, so the
calendar never runs dry while the picture step can't find art the judge accepts (owner, 16 Oct: posting on Instagram,
YouTube and Facebook must never stop). From 13 to 16 Oct every Reel the factory made scored 4 to 5.5 on visuals with
every other score 7 or more, nothing could be booked, and all three channels went quiet.

A Reel can be rescued when every score but visuals passes, its automatic checks found nothing, and no other Reel that
can still go out answers the same quiz question (reel_cloud.unanswered would hold one of them for ever). Each line
whose picture the judgments found fault with, for anything but being shown again (the picture step's rule), shows
a painting made for it (reel_art, owner 20 Oct) when one passes the framing check, else one of the Reel's own
unfaulted pictures again, the nearest earlier one that keeps a picture off screen for a third line in a row; line 1,
the first frame in the feed, takes the first unfaulted picture. A Reel with no picture faulted goes out as it is
(`mark`, from the reels run). Otherwise the factory renders it again (`prepare`, reel_tools render, `seal`) with its
render's own narration, so the framing check is the only Gemini call, and without the QA step, whose spares the
judge never saw. A Reel left with too few pictures of its own is rescued with paintings only once it can't go back
to the picture step (reel_factory.decide), and while the factory can paint (reel_factory._can_paint).

The record (reels.RESCUE) names the video it vouches for. A rescued Reel isn't turned down (reel_writer.turned_down),
so the next Reel answers its question and the runs bring its render; reel_cloud books it below the bar, after the
Reels whose every score passed, and it never sets the bar.

    python -m ytc.reel_rescue status                  each Reel the judge failed: can it be rescued, and how
    python -m ytc.reel_rescue prepare <reel folder>   the rescued short.yaml and pictures.json, and the narration
    python -m ytc.reel_rescue seal <reel folder>      check the new render and record its video
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

from . import reel_art, reel_pictures, reel_writer, reels
from .spec import ShortSpec

log = logging.getLogger(__name__)

# Fewest pictures a rescued Reel keeps: fewer would stay on screen too long (a Reel has 12 to 15 lines).
MIN_PICTURES = 5
# Where `prepare` keeps the Reel's short.yaml and pictures.json as the judge saw them (out of git, as work/ is).
BEFORE = "work/rescue-before"
# A complaint about a picture shown again is the picture step's rule, not a fault of the picture, unless it also
# says the picture is wrong ("Same off-topic image as line 10").
SHOWN_AGAIN = re.compile(r"same (image|picture)|repeat|repetit|again|reuse|consecutive", re.I)
WRONG = re.compile(r"off[- ]?topic|unrelated|irrelevant|nothing to do|inappropriate|offensive|disrespect|nud|breast|"
                   r"gore|blood|mughal|persian|islamic|minaret|urdu|arabic|european|western|chinese|foreign|script|"
                   r"watermark|wrong|inaccura|anachron|contradict|generic|photograph|modern|blurr|crop|"
                   r"(does ?n[o']t|not) (match|represent|show|fit|depict)", re.I)


def _spec(folder: Path) -> dict:
    return yaml.safe_load((folder / "short.yaml").read_text(encoding="utf-8")) or {}


def _shows(spec: dict) -> dict[int, int]:
    """Each line and the line whose picture it shows."""
    return {n: beat.get("visual", {}).get("reuse") or n for n, beat in enumerate(spec["beats"], start=1)}


def faulted(folder: Path) -> list[int]:
    """The lines whose own picture a judgment found fault with for anything but being shown again. The quiz answer's
    picture is the previous Reel's first, which went out with it; judges took it for off-topic (ak046 to ak051)."""
    spec = _spec(folder)
    verdict, shows = reels._read(folder / reels.REVIEW) or {}, _shows(spec)
    found = set()
    for frame in verdict.get("frames") or []:
        problem, beat = str(frame.get("problem", "")), frame.get("beat")
        if beat in shows and spec["beats"][beat - 1].get("role") != "answer" and (
                WRONG.search(problem) or not SHOWN_AGAIN.search(problem)):
            found.add(shows[beat])
    return sorted(found)


def _answered_by(folder: Path) -> str:
    """Another Reel that answers the same quiz question and can still go out, or ""."""
    if not (before := (_spec(folder).get("answers") or {}).get("reel")):
        return ""
    for other in sorted(reels.REELS.glob("ak*/short.yaml")):
        if other.parent == folder or (yaml.safe_load(other.read_text(encoding="utf-8")).get("answers") or {}).get(
                "reel") != before:
            continue
        record = reels._read(other.parent / "instagram.json")
        if (record and not record.get("withdrawn")) or (not record and not reel_writer.turned_down(other.parent)):
            return other.parent.name
    return ""


def why_not(folder: Path) -> str:
    """Why a Reel can't be rescued, or "" when it can."""
    verdict = reels._read(folder / reels.REVIEW)
    if (folder / reels.RESCUE).exists():
        return "it has a rescue already"
    if not (folder / "short.yaml").exists() or (folder / "instagram.json").exists():
        return "it isn't a made Reel waiting to go out"
    if not verdict or reels.judged(verdict) < reels.RUNS:
        return "its review isn't finished"
    if not reel_writer.turned_down(folder):
        return "the judge didn't fail it"
    if warnings := (verdict.get("check") or {}).get("warnings"):
        return f"its automatic checks failed: {'; '.join(warnings)}"
    if low := [f"{k} {reels.score(verdict['scores'], k)}" for k in reels.GATE
               if k != "visuals" and reels.score(verdict["scores"], k) < reels.PASS_SCORE]:
        return f"more than its pictures failed ({', '.join(low)})"
    if other := _answered_by(folder):
        return f"{other} answers the same question"
    return ""


def paintable(folder: Path) -> list[int]:
    """The faulted lines a painting can take the place of: the story's, line 1 first, each with the brief its painting
    is made from (reel_writer's "picture")."""
    spec = _spec(folder)
    beats = ((reels._read(folder / reel_writer.SCRIPT) or {}).get("script") or {}).get("beats") or []
    return sorted((n for n in faulted(folder) if n <= len(beats) and beats[n - 1].get("picture")
                   and spec["beats"][n - 1].get("role", "story") == "story"), key=lambda n: (n != 1, n))


def paintings(folder: Path) -> dict[int, dict]:
    """Paintings for the faulted lines that passed the framing check, by line; {} when Cloudflare isn't set up or no
    model made one."""
    if not reel_art.ready() or not (lines := paintable(folder)):
        return {}
    beats = json.loads((folder / reel_writer.SCRIPT).read_text(encoding="utf-8"))["script"]["beats"]
    with reel_writer.gemini_only():
        return reel_pictures.paint_framed(folder.name, beats, lines)


def _stand_ins(folder: Path, count: int) -> dict[int, dict]:
    """Paintings as `plan` gets them, for up to `count` faulted lines, to tell whether a rescue with them would do."""
    return {n: {"ref": f"ai:{n}", "title": "", "url": "https://res.cloudinary.com/stand-in.jpg", "source": reel_art.SOURCE,
                "license": reel_art.LICENSE, "size": [reel_art.WIDTH, reel_art.HEIGHT], "subject": [0.2, 0.2, 0.8, 0.8],
                "faces": [], "kind": "painting", "hindi": ""} for n in paintable(folder)[:count]}


def plan(folder: Path, painted: dict[int, dict] | None = None) -> tuple[dict, dict, dict[int, str]]:
    """The rescued short.yaml and pictures.json, and what each changed line shows now: a faulted line its painting
    (`painted`, by line, from `paintings`) where one was made. Raises ValueError when too few pictures are left."""
    spec, pictures = _spec(folder), json.loads((folder / reel_pictures.PICTURES).read_text(encoding="utf-8"))
    bad, shows = set(faulted(folder)), _shows(spec)
    changes: dict[int, str] = {}
    for number, hit in sorted((painted or {}).items()):
        if number not in bad:
            continue
        reel_pictures.place(spec, number, hit)
        pictures[str(number)] = {k: hit[k] for k in (*reel_pictures.HIT_KEYS, *reel_pictures.FRAMED_KEYS)
                                 if hit.get(k) is not None}
        bad.discard(number)
        changes[number] = "a painting made for it"
        for other in [n for n in shows if n != number and shows[n] == number]:
            reel_pictures.reuse(spec, other, number)
            if spec["beats"][other - 1].get("label"):
                spec["beats"][other - 1]["label_y"] = (spec["captions"].get("hook_y", reel_pictures.HOOK_Y)
                                                       if number == 1 else reel_pictures.HOOK_Y)
            changes[other] = f"line {number}'s painting again"
    good = [n for n in sorted(shows) if shows[n] == n and str(n) in pictures and n not in bad
            and spec["beats"][n - 1].get("role", "story") == "story"]
    if len(good) < MIN_PICTURES:
        raise ValueError(f"only {len(good)} of its pictures weren't faulted; at least {MIN_PICTURES} are needed")
    if 1 in bad or 1 not in good:
        first = next(n for n in good if n != 1)
        reel_pictures.place(spec, 1, pictures[str(first)])
        pictures["1"] = pictures.pop(str(first))
        for number in [n for n in shows if n == first or (n != 1 and shows[n] == first)]:
            reel_pictures.reuse(spec, number, 1)
            shows[number], changes[number] = 1, "line 1's picture again"
        good = [1] + [n for n in good if n not in (1, first)]
        bad.discard(1)
        changes[1] = f"line {first}'s picture"
    for number in range(2, len(spec["beats"]) + 1):
        if shows[number] not in bad:
            continue
        to = reel_pictures.least_run([n for n in good if n < number], shows, number)
        if to is None:
            raise ValueError(f"every unfaulted picture before line {number} would stay on screen for a third line")
        reel_pictures.reuse(spec, number, to)
        beat = spec["beats"][number - 1]
        if beat.get("label"):
            # The label sits where line `to`'s own text does, clear of its faces.
            beat["label_y"] = spec["captions"].get("hook_y", reel_pictures.HOOK_Y) if to == 1 else reel_pictures.HOOK_Y
        pictures.pop(str(number), None)
        shows[number] = to
        changes[number] = f"line {to}'s picture again"
    if chained := [n for n, beat in enumerate(spec["beats"], 1)
                   if (to := beat["visual"].get("reuse")) and spec["beats"][to - 1]["visual"].get("reuse")]:
        raise ValueError(f"lines {chained} would show a line that shows another's picture")
    kept = {key: hit for key, hit in pictures.items() if shows.get(int(key)) == int(key)}
    spec["description"] = reel_pictures.with_credits(spec["description"], [kept[k] for k in sorted(kept, key=int)])
    ShortSpec.model_validate(spec)
    return spec, kept, changes


def to_render(failed: list[str], stored: set[str] | None = None, paintings: int = 0) -> list[str]:
    """Of the Reels the judge failed, those the factory can rescue by rendering them again (with `stored`, only those
    whose render is in the releases), the one keeping the most pictures first, then the newest. With `paintings`
    (as many as the factory can paint now), only those that need paintings to keep enough pictures of their own."""
    found = []
    for reel in failed:
        folder = reels.REELS / reel
        # One odd Reel mustn't stop the factory's run.
        try:
            if (stored is not None and reel not in stored) or why_not(folder) or not faulted(folder):
                continue
            if paintings:
                try:
                    plan(folder)
                    continue
                except ValueError:
                    kept = plan(folder, _stand_ins(folder, min(paintings, reel_art.MAX_PAINTED)))[1]
            else:
                kept = plan(folder)[1]
            found.append((-len(kept), -int(re.sub(r"\D", "", reel) or 0), reel))
        except Exception as err:
            log.info("%s can't be rescued: %s: %s", reel, type(err).__name__, err)
    return [reel for *_, reel in sorted(found)]


def mark(folder: Path) -> bool:
    """Rescue a Reel whose pictures the judge found no fault with (only repeats) as it is; whether it did."""
    if why_not(folder) or faulted(folder):
        return False
    verdict = reels._read(folder / reels.REVIEW)
    _write(folder, {"at": _now(), "as_is": True, "from_total": verdict["total"], "faulted": [],
                    "video_sha256": verdict["video_sha256"]})
    return True


def prepare(folder: Path) -> dict:
    """The factory's first rescue step: the rescued short.yaml and pictures.json (the judged ones kept in BEFORE),
    and the narration of the Reel's render from its "media" release, which the new render uses as it is."""
    from . import reel_media, tts

    before = folder / BEFORE
    for name in ("short.yaml", reel_pictures.PICTURES):
        # A try that stopped after writing these starts again from what the judge saw.
        if (before / Path(name).name).exists():
            shutil.copy2(before / Path(name).name, folder / name)
    if why := why_not(folder):
        raise RuntimeError(f"{folder.name} can't be rescued: {why}")
    store, release = reel_media.Store(), f"media {folder.name}"
    manifest = folder / "work" / "manifest.json"
    if store.read(release, reel_media.asset_name("work/manifest.json")) != manifest.read_bytes():
        raise RuntimeError(f"the render in {release} isn't the one the judge saw")
    for name in ("work/narration.wav", "work/speech.json"):
        if not store.get(release, reel_media.asset_name(name), folder / name):
            raise RuntimeError(f"{release} has no {name}")
    bad = faulted(folder)
    spec, kept, changes = plan(folder, paintings(folder))
    before.mkdir(parents=True, exist_ok=True)
    for name in ("short.yaml", reel_pictures.PICTURES):
        shutil.copy2(folder / name, before / Path(name).name)
    (folder / "short.yaml").write_text(yaml.dump(spec, Dumper=reel_pictures._Dumper, allow_unicode=True,
                                                 sort_keys=False, width=100000), encoding="utf-8")
    (folder / reel_pictures.PICTURES).write_text(json.dumps(kept, ensure_ascii=False, indent=1) + "\n",
                                                 encoding="utf-8")
    (folder / "work" / tts.KEPT_NARRATION).write_text(json.dumps(tts.narration_key(ShortSpec.load(
        folder / "short.yaml")), ensure_ascii=False, indent=1), encoding="utf-8")
    verdict = reels._read(folder / reels.REVIEW)
    record = {"at": _now(), "as_is": False, "from_total": verdict["total"], "faulted": bad,
              "changes": {str(n): what for n, what in sorted(changes.items())}}
    _write(folder, record)
    return record


def seal(folder: Path) -> dict:
    """The factory's last rescue step: the new render checked as the judge's run would check it, and its video
    recorded. A render the checks fail raises, so the step is tried again and the Reel set aside after that."""
    from . import check

    video = folder / f"{folder.name}.mp4"
    record = reels._read(folder / reels.RESCUE) or {}
    result = check.check(video, longest=reels.LONGEST)
    record["check"] = {k: result[k] for k in ("duration", "integrated_lufs", "true_peak_dbfs", "warnings")}
    if result["warnings"]:
        _write(folder, record)
        raise RuntimeError(f"the rescued render fails its checks: {'; '.join(result['warnings'])}")
    record["video_sha256"] = reels.sha256(video)
    _write(folder, record)
    return record


def unshippable(folder: Path) -> str:
    """Why a rescued Reel can't be scheduled, or "" when its render is the video the rescue vouches for."""
    record = reels.rescued(folder)
    if not record:
        return "it isn't rescued"
    if reels.sha256(folder / f"{folder.name}.mp4") != record["video_sha256"]:
        return "its video isn't the one rescued"
    return ""


def note(folder: Path) -> dict:
    """What a rescued Reel's Instagram record says of its rescue."""
    record = reels.rescued(folder) or {}
    return {k: record[k] for k in ("at", "as_is", "from_total", "changes") if k in record}


def told(folder: Path) -> str:
    """How the runs' reports name a Reel's rescue."""
    record = reels.rescued(folder) or {}
    if record.get("as_is"):
        return "rescued as it is"
    if any("painting" in what for what in (record.get("changes") or {}).values()):
        return "rescued with paintings for the pictures the judge faulted"
    return "rescued without the pictures the judge faulted"


def _now() -> str:
    return datetime.now(reels.IST).isoformat(timespec="seconds")


def _write(folder: Path, record: dict) -> None:
    (folder / reels.RESCUE).parent.mkdir(parents=True, exist_ok=True)
    (folder / reels.RESCUE).write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ytc.reel_rescue", description="rescue Reels failed on pictures")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    for name in ("prepare", "seal"):
        commands.add_parser(name).add_argument("folder", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if args.command == "status":
        for folder in sorted(p.parent for p in reels.REELS.glob("ak*/short.yaml")):
            if not reel_writer.turned_down(folder) and not reels.rescued(folder):
                continue
            if reels.rescued(folder):
                print(f"{folder.name}: rescued {json.dumps(note(folder), ensure_ascii=False)}")
            elif why := why_not(folder):
                print(f"{folder.name}: no ({why})")
            else:
                try:
                    changes = plan(folder)[2]
                    print(f"{folder.name}: yes, {'as it is' if not changes else json.dumps(changes)}")
                except ValueError as err:
                    print(f"{folder.name}: no ({err})")
        return
    from .cli import PIPELINE_ROOT, _load_env

    _load_env(PIPELINE_ROOT / ".env")
    folder = args.folder.resolve()
    try:
        print(json.dumps(prepare(folder) if args.command == "prepare" else seal(folder), ensure_ascii=False))
    except (RuntimeError, ValueError) as err:
        print(f"{type(err).__name__}: {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
