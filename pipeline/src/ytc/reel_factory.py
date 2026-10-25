"""@ankahi.kathayein's Reel factory: makes the next backlog story into a Reel whenever the Instagram calendar runs
short, so every slot (reel_cloud.SLOTS) has one and the YouTube and Facebook mirrors have them too.

A factory run (reel_host.factory, on CircleCI; reel_cloud.factory_run on Modal) takes a fresh clone and calls `work`
on it. `work` makes at most
one Reel, at most DAILY_CAP a day, and only while the supply is short: fewer than PENDING_MIN made Reels waiting for
the judge or a slot, or fewer than AHEAD_MIN booked on Instagram and waiting together. It finishes a Reel an earlier
run left half-made, or writes the next backlog story (moving its row to Made), then picks the pictures, renders,
lets QA fix the frames, and reports the Reel as made; the run then puts the render in the reels volume and pushes the
folder, and the hourly reel_run judges and books it. These steps and the judge share the brand's daily Gemini quota
(reset 12:30 IST), so a quota error pauses the factory until then instead of counting as a failure, and so does a
Gemini outage or a network drop, until it has happened TRANSIENT_GIVE_UP times to the same step. A step that fails
GIVE_UP times for any other reason sets the Reel aside; a story the writer can't write moves to "## Set aside".
Before it starts a new story, a Reel the judge failed on its pictures alone is rescued (reel_rescue: rendered again
with paintings or its own unfaulted pictures in place of the faulted ones, and booked below the bar), or, when too
few of its pictures are left for that, goes back to the picture step once (send_back), without the pictures the judge
found fault with; after that, paintings alone can still rescue it while the day's Cloudflare neurons last
(reel_art.DAILY_NEURONS, counted in the state by UTC day). A rescue step that fails for any reason but an outage sets
the Reel aside at once. A picture step left short of pictures while the day's neurons are out waits for them, and
the factory goes on with other Reels meanwhile.
With --until (CircleCI stops a job at one hour), a step that can't finish by then waits for the next run. When
BACKLOG_LOW or fewer stories the writer can read are left, reel_backlog adds new ones that check out.

The state (each Reel's progress and failures, set-aside Reels whose number a later story took, Reels made per day,
the quota pause, alerts sent) is kept by the run (content/instagram/automation/factory.json on CircleCI); `work`
reads and writes it as the JSON file the run hands over. A FACTORY_PAUSE file next to the backlog stops the factory.

    python -m ytc.reel_factory work --state STATE.json [--force] [--until ISO]   (on a checkout: one run's step)
    python -m ytc.reel_factory status --state STATE.json                         (supply and decisions; changes nothing)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import reel_art, reel_pictures, reel_rescue, reel_writer, reels

IST = ZoneInfo("Asia/Kolkata")
PENDING_MIN = 3
AHEAD_MIN = 10
# Reels a day that go to the judge, new or sent back for new pictures: about what its two judgments a Reel leave of
# the day's quota. Instagram has 4 slots a day (3 to 4 Reels a day, owner 20 Oct) and about half the Reels pass;
# rescues go out unjudged and don't count. The supply check (PENDING_MIN, AHEAD_MIN) stops the factory well before
# this once the queue is full.
DAILY_CAP = 10
GIVE_UP = 3
# Lines without a picture of their own the picture step allows on its last try, so the story isn't set aside for
# want of one more picture (ak041 was made with 4); a Reel the judge then fails on its pictures can still be rescued.
LAST_TRY_REUSED = 4
# How long a new story waits for the judge of the Reel whose question it answers, after that Reel was made: one the
# checks keep from the judge (too long, too loud) would otherwise stop the factory.
JUDGE_WAIT = timedelta(hours=6)
# A series part the judge fails goes back to the top of Next, so the next part doesn't go out without it; twice.
PART_TRIES = 2
TRANSIENT_GIVE_UP = 8
BACKLOG_LOW = 4
STUCK = timedelta(days=5)
RESET = (12, 40)
# Gemini TTS names only the quota it ran out of (tts.py: "HTTP 429 GenerateRequestsPerDayPerProjectPerModel-FreeTier").
QUOTA = re.compile(r"OutOfQuota|free quota left today|RESOURCE_EXHAUSTED|PerDay", re.I)
# The paid project's prepaid credits ran out (llm.OutOfCredits, HTTP 402). Nothing answers until the owner adds
# credits, which can be any time, so the factory looks again after CREDITS_WAIT rather than at the quota's reset.
CREDITS = re.compile(r"OutOfCredits|Payment Required", re.I)
CREDITS_WAIT = timedelta(hours=1)
TRANSIENT = re.compile(r"Overloaded|\b503\b|UNAVAILABLE|high demand|Read timed out|TimeoutError|NameResolution|"
                       r"ClientConnector|ConnectionError|Connection reset|RemoteDisconnected|"
                       r"Temporary failure in name resolution|SSLError", re.I)
# The picture step's error when too few lines have a picture of their own (reel_pictures.picture).
SHORT_OF_PICTURES = re.compile(r"LookupError: no usable picture for lines")
# Stories that may wait for paintings at once; past that a story short of pictures uses up its tries as before, so a
# long Cloudflare outage can't turn the backlog into half-made Reels.
WAITING_MAX = 3
# Painted line by line (reel_art.CINEMATIC), a Reel takes about 15 paintings and the day's neurons paint two or three,
# one of which may pass: Instagram had nothing booked from Tue 27 Oct. While one of the next three days has no Reel
# booked and none is made for it, the next story is made the older way, museum pictures with a painting only where
# none fits, one at a time; a painted Reel still takes its slot (reel_cloud.swap_for). False: every story is painted.
# A Reel pictured again repaints only the lines the judge faulted, so it still goes first while the neurons last.
MUSEUM_WHEN_IDLE = True
# A judge's complaint about a picture shown again, which the picture step's rules settle, not about the picture itself.
SHOWN_AGAIN = reel_rescue.SHOWN_AGAIN
# Where a Reel sent back to the picture step keeps its old picks, QA, render and review, out of git.
REDO = "work/redo"
PAUSE = reels.REELS / "FACTORY_PAUSE"
BACKLOG = reels.REELS / "backlog.md"
RECORD = "instagram.json"
# Minutes a step needs on CircleCI's 4-CPU machine; with --until, a step isn't started with less time left. Measured
# on 20 Oct: write 6.5, pictures 4.5-8.5, render 8-12, QA 3-21 (two fix rounds, each rendering again); a QA cut short
# looks again at the render it left, and a cut render starts over.
NEEDS = {"write": 10, "pictures": 12, "render": 15, "qa": 25, "rescue": 8, "seal": 5}


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _due(record: dict | None) -> datetime | None:
    try:
        return datetime.fromisoformat(record["dueAt"].replace("Z", "+00:00"))
    except Exception:
        return None


def _judged_and_failed(folder: Path) -> bool:
    return reel_writer.turned_down(folder)


def _faulted(folder: Path) -> list[str]:
    """The pictures the judge found fault with in a Reel, not counting its complaints about a picture shown again."""
    review, chosen = _read(folder / reels.REVIEW) or {}, _read(folder / reel_pictures.PICTURES) or {}
    return sorted({ref for f in review.get("frames") or [] if not SHOWN_AGAIN.search(str(f.get("problem", "")))
                   and (ref := (chosen.get(str(f.get("beat"))) or {}).get("ref"))})


def _pictures_failed(folder: Path, mine: dict) -> bool:
    """Whether the judge failed a Reel on its pictures alone, every other score passing, and it hasn't been sent back
    to the picture step before. A Reel that had more lines without a picture of their own than the picture step now
    allows (ak041, made while it allowed 4) would only fail there. Nor one that answers the same question as a Reel
    that can go out (ak052, pictured again after ak051 was booked), which never goes out."""
    review = _read(folder / reels.REVIEW)
    if mine.get("redo") or not review or (review.get("check") or {}).get("warnings") or not _judged_and_failed(folder) \
            or reel_rescue._answered_by(folder):
        return False
    try:
        if [k for k in reels.GATE if reels.score(review["scores"], k) < reels.PASS_SCORE] != ["visuals"]:
            return False
    except (KeyError, TypeError):
        return False
    beats = ((_read(folder / reel_writer.SCRIPT) or {}).get("script") or {}).get("beats") or []
    pictured = {int(n) for n in _read(folder / reel_pictures.PICTURES) or {}}
    missing = [n for n in reel_pictures.own_lines(beats) if n not in pictured]
    return bool(beats) and len(missing) <= reel_pictures.MAX_REUSED


def send_back(reel: str, state: dict) -> list[str]:
    """Send a Reel the judge failed on its pictures alone back to the picture step, once; the pictures left out.

    Its picks, QA, render and review move to REDO, out of git, so neither run takes the old render for this Reel, and
    avoid.json keeps the review and the pictures it found fault with, which the picture step leaves out."""
    folder = reels.REELS / reel
    review, refs = _read(folder / reels.REVIEW), _faulted(folder)
    (folder / REDO).mkdir(parents=True, exist_ok=True)
    for name in ("short.yaml", f"{reel}.mp4", reel_pictures.PICTURES, reel_pictures.SPARES, "work/qa.json",
                 "work/manifest.json", reels.REVIEW):
        if (folder / name).exists():
            shutil.move(folder / name, folder / REDO / Path(name).name)
    (folder / reel_pictures.AVOID).write_text(json.dumps({"refs": refs, "review": review}, ensure_ascii=False, indent=1)
                                              + "\n", encoding="utf-8")
    mine = state["reels"].setdefault(reel, {})
    mine.pop("pushed", None)
    mine.update(fails={}, transient={}, redo={"at": datetime.now(IST).isoformat(timespec="seconds"),
                                             "visuals": review["scores"]["visuals"], "left_out": refs})
    mine.setdefault("story", ((_read(folder / reel_writer.SCRIPT) or {}).get("row") or {}).get("story", ""))
    return refs


def supply(state: dict, now: datetime, media: Path | None = None, stored: set[str] | None = None) -> dict:
    """Reels booked on Instagram from now on, Reels made and waiting for the judge or a slot, those judged and
    failed or waiting longer than STUCK, and the factory's half-made ones. With `media` (the reels volume) or
    `stored` (the Reels whose render is in the releases), a Reel waits only once its render is there, as the hourly
    run needs."""
    found = {"booked": [], "pending": [], "failed": [], "stuck": [], "unfinished": [], "waiting": []}
    for folder in sorted(p for p in reels.REELS.glob("ak*") if p.is_dir()):
        mine = state["reels"].get(folder.name, {})
        if mine and not mine.get("pushed") and not mine.get("set_aside"):
            found["waiting" if mine.get("waits") else "unfinished"].append(folder.name)
            continue
        if not (folder / "short.yaml").exists() or mine.get("set_aside"):
            continue
        if record := _read(folder / RECORD):
            if not record.get("withdrawn") and (when := _due(record)) and when > now:
                found["booked"].append(folder.name)
            continue
        # An older Reel only fills a slot no painted one takes (reel_cloud), so it is neither waiting nor rescued; one
        # made the older way for a day ahead with nothing booked (MUSEUM_WHEN_IDLE) is.
        if reel_art.CINEMATIC and not reel_pictures.all_painted(folder) and not mine.get("museum"):
            continue
        if _judged_and_failed(folder):
            found["failed"].append(folder.name)
        elif media and not (media / folder.name / f"{folder.name}.mp4").exists():
            continue
        elif stored is not None and folder.name not in stored:
            continue
        elif (pushed := mine.get("pushed")) and now - datetime.fromisoformat(pushed) > STUCK:
            found["stuck"].append(folder.name)
        else:
            found["pending"].append(folder.name)
    return found


def readable_left() -> int:
    return sum(bool(reel_writer.source_urls(row["source"])) and not reel_writer.held(row)
               for row in reel_writer.backlog())


def _row_line(lines: list[str], story: str, start: int) -> int | None:
    for index in range(start, len(lines)):
        cells = [c.strip() for c in lines[index].strip().strip("|").split("|")]
        if lines[index].startswith("|") and cells[0] == story:
            return index
    return None


def move_row(reel: str, row: dict, note: str = "") -> bool:
    """Move a story's row from Next to Made as `reel`, or to Set aside with a note when reel is ""."""
    lines = BACKLOG.read_text(encoding="utf-8").split("\n")
    nxt = lines.index("## Next")
    index = _row_line(lines, row["story"], nxt)
    if index is None:
        return False
    del lines[index]
    if reel:
        last = max(i for i, line in enumerate(lines[:nxt]) if re.match(r"\| ak\d+ ", line))
        lines.insert(last + 1, f"| {reel} | {row['story']} | {row['source']} |")
    else:
        if "## Set aside" not in lines:
            lines[nxt:nxt] = ["## Set aside", "", "Stories the Reel factory couldn't make; fix the row and move it back"
                              " to Next.", "", "| Story | Source | Why |", "|---|---|---|", ""]
            nxt = lines.index("## Next")
        aside = lines.index("## Set aside")
        last = max(i for i in range(aside, nxt) if lines[i].startswith("|"))
        lines.insert(last + 1, f"| {row['story']} | {row['source']} | {note} |")
    BACKLOG.write_text("\n".join(lines), encoding="utf-8")
    return True


def requeue(row: dict) -> bool:
    """Put a story's row back at the top of Next; False when Next has it already."""
    lines = BACKLOG.read_text(encoding="utf-8").split("\n")
    nxt = lines.index("## Next")
    if _row_line(lines, row["story"], nxt) is not None:
        return False
    header = next(i for i in range(nxt, len(lines)) if lines[i].startswith("| Story"))
    cells = [row.get(key, "") for key in reel_writer.COLUMNS] + [row.get(reel_writer.SERIES, "")]
    lines.insert(header + 2, "| " + " | ".join(cells) + " |")
    BACKLOG.write_text("\n".join(lines), encoding="utf-8")
    return True


def requeue_parts(state: dict) -> list[str]:
    """Put each series part every Reel of which failed (the judge, or a step GIVE_UP times) back at the top of Next,
    up to PART_TRIES Reels a part; the stories put back. Done again each run until the part is written again, since
    a run that makes nothing doesn't push the backlog."""
    made: dict[tuple[str, int], list[tuple[Path, dict]]] = {}
    for folder in sorted(p for p in reels.REELS.glob("ak*") if p.is_dir()):
        row = (_read(folder / reel_writer.SCRIPT) or {}).get("row") or {}
        if part := reel_writer.part_of(row):
            made.setdefault((part["series"], part["number"]), []).append((folder, row))
    queued = {(p["series"], p["number"]) for row in reel_writer.backlog() if (p := reel_writer.part_of(row))}
    back = []
    for key, tries in made.items():
        failed = [folder for folder, _ in tries if (mine := state["reels"].get(folder.name, {})).get("set_aside") or
                  (_judged_and_failed(folder) and not _pictures_failed(folder, mine))]
        if key not in queued and len(failed) == len(tries) < PART_TRIES and requeue(tries[-1][1]):
            back.append(tries[-1][1]["story"])
    return back


def waits_for_judge(reel: str, state: dict, now: datetime) -> str:
    """Why the next story waits ("" when it needn't): the Reel whose question it will answer isn't booked or judged
    yet, and was made less than JUDGE_WAIT ago. Written now, it might answer a question no viewer is asked."""
    if not (before := reel_writer.asked_before(reel)) or (before[0] / RECORD).exists():
        return ""
    if reels.judged(_read(before[0] / reels.REVIEW)) >= reels.RUNS:
        return ""
    pushed = (state["reels"].get(before[0].name) or {}).get("pushed")
    if not pushed or now - datetime.fromisoformat(pushed) > JUDGE_WAIT:
        return ""
    return f"waiting for the judge of {before[0].name}, whose question the next Reel answers"


def next_reset(now: datetime) -> datetime:
    reset = now.astimezone(IST).replace(hour=RESET[0], minute=RESET[1], second=0, microsecond=0)
    return reset if reset > now else reset + timedelta(days=1)


def _spend(usage_log: Path) -> dict:
    """What a step's model calls came to (llm.record's lines): calls, tokens and US dollars at paid prices, the
    Cloudflare neurons its paintings cost when it made any, and reel_art.OUT_OF_NEURONS when Cloudflare refused it."""
    total = {"calls": 0, "tokens": 0, "usd": 0.0}
    for line in usage_log.read_text(encoding="utf-8").splitlines() if usage_log.exists() else []:
        try:
            call = json.loads(line)
        except ValueError:
            continue
        if call.get(reel_art.OUT_OF_NEURONS):
            total[reel_art.OUT_OF_NEURONS] = True
            continue
        total["calls"] += 1
        total["tokens"] += sum(call.get(k) or 0 for k in ("input_tokens", "output_tokens", "thinking_tokens"))
        total["usd"] += call.get("usd") or 0
        if call.get("neurons"):
            total["neurons"] = total.get("neurons", 0) + call["neurons"]
    return total


def _neurons_left(state: dict, now: datetime) -> float:
    """What is left today of Ankahi's share of the Cloudflare neurons (reel_art.DAILY_NEURONS), by Cloudflare's UTC
    day."""
    return max(0.0, reel_art.DAILY_NEURONS - state.get("neurons", {}).get(f"{now:%Y-%m-%d}", 0))


def _add_neurons(state: dict, now: datetime, neurons: float) -> None:
    days = state.setdefault("neurons", {})
    days[f"{now:%Y-%m-%d}"] = round(days.get(f"{now:%Y-%m-%d}", 0) + neurons, 2)
    for day in sorted(days)[:-7]:
        days.pop(day)


def _add_spend(into: dict, spent: dict) -> None:
    for key, value in spent.items():
        into[key] = round(into.get(key, 0) + value, 4)


def _run(command: list[str], log: Path, timeout: int) -> tuple[int, str]:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as out:
        child = subprocess.Popen(command, cwd=reels.ROOT / "pipeline", stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGTERM)
            time.sleep(10)
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            code = -1
    text = log.read_text(errors="replace")
    return code, (f"timed out after {timeout // 60} min; " if code == -1 else "") + text[-2500:]


def _steps(reel: str, rescuing: bool = False) -> list[tuple[str, object, list[str], int]]:
    folder = reels.REELS / reel
    video = folder / f"{reel}.mp4"
    py = [sys.executable, "-u", "-m"]
    render = ("render", lambda: video.exists() and video.stat().st_mtime >= (folder / "short.yaml").stat().st_mtime,
              [*py, "ytc.reel_tools", "render", str(folder / "short.yaml")], 90 * 60)
    if rescuing:
        return [
            ("rescue", lambda: (folder / reels.RESCUE).exists(), [*py, "ytc.reel_rescue", "prepare", str(folder)],
             15 * 60),
            render,
            ("seal", lambda: bool(reels.rescued(folder)), [*py, "ytc.reel_rescue", "seal", str(folder)], 15 * 60),
        ]
    return [
        ("write", lambda: (folder / reel_writer.SCRIPT).exists(), [*py, "ytc.reel_writer", "write", "--id", reel],
         30 * 60),
        ("pictures", lambda: (folder / "short.yaml").exists(), [*py, "ytc.reel_pictures", str(folder)], 60 * 60),
        render,
        ("qa", lambda: (_read(folder / "work" / "qa.json") or {}).get("finished", False),
         [*py, "ytc.reel_qa", str(folder)], 120 * 60),
    ]


def make(reel: str, state: dict, logs: Path, done: dict, until: datetime | None = None) -> str:
    """Take one Reel as far as it goes; "made", "quota", "credits", "failed", "set aside", or "paused" when the next
    step can't finish by `until`."""
    folder = reels.REELS / reel
    mine = state["reels"].setdefault(reel, {"started": datetime.now(IST).isoformat(timespec="seconds"), "fails": {}})
    if done.get("museum"):
        mine["museum"] = True
    mine.pop("waits", None)
    if (script := _read(folder / reel_writer.SCRIPT)) and script.get("row"):
        move_row(reel, script["row"])  # a resumed Reel's row, moved on a clone that was never pushed
        if not (folder / "short.yaml").exists() and reel_writer.end_on_story(folder):
            print(f"{reel}: its script, written while the quiz was on, now ends on its story", flush=True)
    for step, finished, command, timeout in _steps(reel, bool(mine.get("rescue"))):
        if finished():
            continue
        cut = False
        if until:
            left = int((until - datetime.now(timezone.utc)).total_seconds())
            if left < NEEDS[step] * 60:
                done["why"] = f"{step} needs about {NEEDS[step]} min and this run has {max(left, 0) // 60} left"
                return "paused"
            cut, timeout = left < timeout, min(timeout, left)
        done["steps"].append(step)
        started = time.time()
        usage_log = logs / f"{reel}_{step}_usage.jsonl"
        usage_log.unlink(missing_ok=True)
        os.environ["YTC_USAGE_LOG"] = str(usage_log)
        if step == "pictures" and mine["fails"].get(step, 0) >= GIVE_UP - 1:
            os.environ["YTC_MAX_REUSED"] = str(LAST_TRY_REUSED)
        if step in ("pictures", "rescue"):
            os.environ["YTC_PAINT_NEURONS"] = str(_neurons_left(state, datetime.now(timezone.utc)))
        # Its script asks for museum pictures, so each step of a story made the older way is run the older way.
        if mine.get("museum"):
            os.environ["YTC_CINEMATIC"] = "0"
        try:
            code, tail = _run(command, logs / f"{reel}_{step}.log", timeout)
        finally:
            for name in ("YTC_USAGE_LOG", "YTC_MAX_REUSED", "YTC_PAINT_NEURONS", "YTC_CINEMATIC"):
                os.environ.pop(name, None)
        # Every try counts, failed ones too: they cost the same.
        spent = _spend(usage_log)
        refused = spent.pop(reel_art.OUT_OF_NEURONS, False)
        _add_spend(mine.setdefault("usage", {}).setdefault(step, {}), spent)
        _add_spend(state.setdefault("spend", {}).setdefault(f"{datetime.now(IST):%Y-%m-%d}", {}), spent)
        if spent.get("neurons"):
            _add_neurons(state, datetime.now(timezone.utc), spent["neurons"])
        if refused:
            _add_neurons(state, datetime.now(timezone.utc), _neurons_left(state, datetime.now(timezone.utc)))
        print(f"{reel} {step}: exit {code} after {time.time() - started:.0f} s, {spent['calls']} model calls, "
              f"${spent['usd']:.3f} at paid prices" + (f", {spent['neurons']:.0f} neurons" if spent.get("neurons") else ""),
              flush=True)
        if code == -1 and step == "render":
            # The encode writes the Reel's video in place, so a render stopped midway leaves a newer, broken one.
            (folder / f"{reel}.mp4").unlink(missing_ok=True)
        if not code and finished():
            if step == "write":
                script = _read(folder / reel_writer.SCRIPT) or {}
                if script.get("row") and move_row(reel, script["row"]):
                    done["moved"] = script["row"]["story"]
                mine["story"] = (script.get("row") or {}).get("story", "")
            continue
        # Only the error the step died of counts: its log also holds the overloads and refusals it got past.
        last = tail.strip().splitlines()[-1][:300] if tail.strip() else str(code)
        mine["last_error"] = f"{step}: {last}"
        # The next try writes the step's log again, so a failed try's log is kept under its own name.
        failed_log = logs / f"{reel}_{step}_failed_{datetime.now(timezone.utc):%m%d%H%M}.log"
        shutil.copyfile(logs / f"{reel}_{step}.log", failed_log)
        if CREDITS.search(last):
            state["cooldown_until"] = (datetime.now(timezone.utc) + CREDITS_WAIT).isoformat(timespec="seconds")
            done["alerts"].append(("credits", "the Gemini project's prepaid credits ran out, so no Reel can be made "
                                   "or judged: add credits in AI Studio > Billing (the factory looks again every hour)"))
            done["why"] = f"{step}: the Gemini credits ran out; paused until {state['cooldown_until']}"
            return "credits"
        if QUOTA.search(last):
            state["cooldown_until"] = next_reset(datetime.now(timezone.utc)).isoformat()
            done["why"] = f"{step} hit the Gemini quota; paused until {state['cooldown_until']}"
            return "quota"
        transient = mine.setdefault("transient", {})
        if code == -1 and cut and transient.get(step, 0) < TRANSIENT_GIVE_UP:
            transient[step] = transient.get(step, 0) + 1
            done["why"] = f"{step} ran out of this run's time ({transient[step]} of {TRANSIENT_GIVE_UP})"
            done["transient"] = True
            return "failed"
        if code != -1 and TRANSIENT.search(last) and transient.get(step, 0) < TRANSIENT_GIVE_UP:
            transient[step] = transient.get(step, 0) + 1
            done["why"] = f"{step} hit an outage or network error ({transient[step]} of {TRANSIENT_GIVE_UP}): {last}"
            done["transient"] = True
            return "failed"
        if step == "rescue" and refused:
            # It failed for want of the paintings Cloudflare refused, so it goes back among the failed Reels (supply
            # counts one without "pushed" as half-made), and decide offers it again once the day's neurons are back.
            rescue = mine.pop("rescue", None) or {}
            mine["pushed"] = rescue.get("pushed") or datetime.now(timezone.utc).isoformat(timespec="seconds")
            done["why"] = f"rescue needs paintings Cloudflare refuses until the neurons are back: {last}"
            return "failed"
        # Painted line by line, a story can't be pictured at all until the neurons are back, and decide starts none
        # meanwhile, so it always waits: counted as failures, a new story was set aside every few minutes (24 Oct).
        if (step == "pictures" and SHORT_OF_PICTURES.search(last) and reel_art.ready()
                and _neurons_left(state, datetime.now(timezone.utc)) < reel_art.NEURONS_EACH
                and (reel_art.CINEMATIC or (transient.get(step, 0) < TRANSIENT_GIVE_UP
                     and sum(bool(other.get("waits")) for other in state["reels"].values()) < WAITING_MAX))):
            # Another try today finds the same museum pictures (ak077's tries on 21 Oct each came up 8 lines short),
            # and a story set aside comes back only by hand: it waits for the paintings (decide).
            transient[step] = transient.get(step, 0) + 1
            mine["waits"] = "paintings"
            done["why"] = f"pictures wait for the day's neurons, for paintings of the lines no museum picture shows: {last}"
            return "failed"
        # A rescue that can't be done (too few pictures left, a render that fails its checks) won't be done by trying
        # again, and the new stories would wait behind it.
        mine["fails"][step] = GIVE_UP if step in ("rescue", "seal") else mine["fails"].get(step, 0) + 1
        done["why"] = f"{step} failed ({mine['fails'][step]} of {GIVE_UP}): {last}"
        if mine["fails"][step] < GIVE_UP:
            return "failed"
        mine["set_aside"] = f"{step} failed {GIVE_UP} times: {last}"
        if step == "write" and (found := reel_writer.next_story()):
            move_row("", found[0], f"{reel}: the writer failed {GIVE_UP} times")
            shutil.rmtree(folder, ignore_errors=True)
            state["reels"].pop(reel)
        # No alert: the next run starts the next story, and the owner hears only if the calendar runs dry.
        return "set aside"
    return "made"


def _stalled(now: datetime) -> bool:
    """Whether a thin calendar should reach the owner, as the reels run's alerts do (reel_cloud._tell_owner): no Reel
    booked for STALLED_AFTER, or for STALLED_COVERED while older Reels can go out again (reel_repost)."""
    from . import reel_cloud, reel_repost

    try:
        covered = reel_repost.ready(now)
    except Exception as err:
        print(f"couldn't check the older Reels that can go out again: {err}", file=sys.stderr)
        covered = False
    last = reel_repost.last_booked()
    return not last or now - last > (reel_cloud.STALLED_COVERED if covered else reel_cloud.STALLED_AFTER)


def decide(state: dict, now: datetime, force: bool, media: Path | None = None,
           stored: set[str] | None = None) -> tuple[str | None, dict]:
    """The Reel this run works on (None: nothing to do) and what the run found."""
    done: dict = {"steps": [], "alerts": []}
    if PAUSE.exists():
        return None, done | {"decision": f"{PAUSE.name} is set"}
    if (until := state.get("cooldown_until")) and datetime.fromisoformat(until) > now:
        return None, done | {"decision": f"waiting for the Gemini quota until {until}"}
    found = supply(state, now, media, stored)
    done["supply"] = {k: v for k, v in found.items() if v}
    days = {_due(_read(reels.REELS / r / RECORD)).astimezone(IST).date() for r in found["booked"]}
    soon = [(now.astimezone(IST) + timedelta(days=d)).date() for d in range(1, 4)]
    idle = [d for d in soon if d not in days]
    if not found["pending"] and not found["unfinished"] and idle and _stalled(now):
        done["alerts"].append(("dry", f"no Reel is booked for {', '.join(f'{d:%a %d %b}' for d in idle)} and none "
                               "is waiting"))
    if found["stuck"]:
        done["alerts"].append(("stuck", f"{', '.join(found['stuck'])} made over {STUCK.days} days ago and still not "
                               "booked"))
    if (left := readable_left()) <= 1:
        done["alerts"].append(("backlog", f"only {left} stories the writer can read are left in the backlog, and "
                               f"refilling it didn't work ({state.get('refill_error') or 'no new story checked out'})"))
    # A story the picture step was short of while the day's neurons were out goes on once they're back.
    paintable = [reel for reel in found.get("waiting", [])
                 if reel_art.ready() and _neurons_left(state, now) >= reel_art.NEURONS_EACH]
    # One made the older way first: it paints at most 8 lines a try (reel_art.MAX_PAINTED), a painted one up to 15.
    paintable.sort(key=lambda reel: not state["reels"].get(reel, {}).get("museum"))
    short = len(found["pending"]) < PENDING_MIN or len(found["booked"]) + len(found["pending"]) < AHEAD_MIN
    if not (short or force or found["unfinished"] or paintable):
        return None, done | {"decision": "supply is enough"}
    capped = state["made"].get(f"{now.astimezone(IST):%Y-%m-%d}", 0) >= DAILY_CAP and not force
    # The cap holds only what goes to the judge: a Reel already started is finished, and a rescue goes out unjudged.
    if found["unfinished"]:
        return found["unfinished"][0], done | {"decision": "resuming"}
    if paintable:
        return paintable[0], done | {"decision": "resuming with paintings"}
    # A rescue costs no model call (paintings one framing check) and goes out; new pictures cost less than a new
    # story, and the rest passed.
    if rescues := reel_rescue.to_render(found["failed"], stored):
        return rescues[0], done | {"decision": "rescuing"}
    # Every line is painted (reel_art.CINEMATIC), so with the day's neurons spent no story can be pictured.
    starved = reel_art.CINEMATIC and reel_art.ready() and _neurons_left(state, now) < reel_art.NEURONS_EACH
    # A day ahead with nothing for it gets a story made the older way (MUSEUM_WHEN_IDLE), even with the day's
    # neurons spent: the museums may show every line.
    museum = (MUSEUM_WHEN_IDLE and reel_art.CINEMATIC and len(found["pending"]) < len(idle)
              and not any(state["reels"].get(reel, {}).get("museum") for reel in found.get("waiting", [])))
    again = [reel for reel in found["failed"] if _pictures_failed(reels.REELS / reel, state["reels"].get(reel, {}))]
    if again and not capped and not starved:
        return again[0], done | {"decision": "picturing again"}
    # Too few of its pictures are left and it can't go back to the picture step: paintings stand in for them.
    if reel_art.ready() and (paintings := int(_neurons_left(state, now) // reel_art.NEURONS_EACH)) and (
            rescues := reel_rescue.to_render([r for r in found["failed"] if r not in again], stored, paintings)):
        return rescues[0], done | {"decision": "rescuing"}
    if capped:
        return None, done | {"decision": f"already made {DAILY_CAP} today"}
    if starved and not museum:
        return None, done | {"decision": "today's neurons are spent: no story can be painted until Cloudflare's "
                                         "reset at 05:30 IST"}
    if not reel_writer.next_story():
        done["alerts"].append(("backlog", "the backlog has no story the writer can read; no Reel made"))
        return None, done | {"decision": "no readable story"}
    if why := waits_for_judge(reel := reel_writer.next_id(), state, now):
        return None, done | {"decision": why}
    return reel, done | {"decision": "starting"} | ({"museum": True} if museum else {})


def refill(state: dict, now: datetime) -> list[str]:
    """When BACKLOG_LOW or fewer readable stories are left, add new ones (reel_backlog), once a day unless the Gemini
    quota stopped it; the stories added."""
    today = f"{now.astimezone(IST):%Y-%m-%d}"
    if PAUSE.exists() or state.get("refilled") == today or readable_left() > BACKLOG_LOW:
        return []
    if (until := state.get("cooldown_until")) and datetime.fromisoformat(until) > now:
        return []
    from . import llm, reel_backlog

    before = len(llm.calls)
    try:
        found = reel_backlog.refill()
    except llm.OutOfQuota:
        return []
    except Exception as err:
        state["refilled"], state["refill_error"] = today, f"{type(err).__name__}: {err}"[:300]
        return []
    finally:
        new = llm.calls[before:]
        _add_spend(state.setdefault("spend", {}).setdefault(today, {}), {
            "calls": len(new), "usd": sum(llm.cost(c) for c in new),
            "tokens": sum(c.get(k) or 0 for c in new for k in ("input_tokens", "output_tokens", "thinking_tokens"))})
    state["refilled"] = today
    state["refill_error"] = "" if found["added"] else f"all {len(found['turned_down'])} proposals were turned down"
    print(json.dumps({"refill": found}, ensure_ascii=False), flush=True)
    return found["added"]


def work(state_path: Path, logs: Path, force: bool = False, media: Path | None = None, stored: set[str] | None = None,
         until: datetime | None = None) -> dict:
    state = _read(state_path) or {}
    for key in ("reels", "made", "alerts"):
        state.setdefault(key, {})
    now = datetime.now(timezone.utc)
    refilled = refill(state, now)
    requeued = requeue_parts(state) if not PAUSE.exists() else []
    reel, done = decide(state, now, force, media, stored)
    if refilled:
        done["refilled"] = refilled
    if requeued:
        done["requeued"] = requeued
    if reel:
        done["reel"] = reel
        if done["decision"] == "starting" and state["reels"].get(reel, {}).get("set_aside"):
            # A Reel set aside after its story was written leaves no folder in git, so the next story takes its number.
            # Under the old record that story would get no tries of its own, and a pause of it would never resume.
            state.setdefault("aside", []).append({"reel": reel, **state["reels"].pop(reel)})
        if done["decision"] == "rescuing":
            mine = state["reels"].setdefault(reel, {})
            mine.update(fails={}, transient={}, rescue={"at": datetime.now(IST).isoformat(timespec="seconds"),
                                                        "pushed": mine.pop("pushed", None)})
            mine.setdefault("story", ((_read(reels.REELS / reel / reel_writer.SCRIPT) or {}).get("row") or {}).get(
                "story", ""))
        if done["decision"] == "picturing again":
            try:
                done["sent_back"] = {"left_out": send_back(reel, state)}
            except Exception as err:
                # Never sent back again, or every run would stop at the same Reel; the next run starts a story.
                state["reels"].setdefault(reel, {})["redo"] = {"failed": f"{type(err).__name__}: {err}"[:300]}
                done["result"], done["why"] = "not sent back", state["reels"][reel]["redo"]["failed"]
                state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
                return done
        done["result"] = make(reel, state, logs, done, until)
        done["story"] = state["reels"].get(reel, {}).get("story", "")
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return done


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ytc.reel_factory", description="keep the Reels ahead of the calendar")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("work", "status"):
        command = commands.add_parser(name)
        command.add_argument("--state", type=Path, required=True, help="the factory's state, as JSON")
        command.add_argument("--logs", type=Path, default=Path("/tmp/factory-logs"), help="each step's log goes here")
        command.add_argument("--force", action="store_true", help="make a Reel even when the supply is enough")
        command.add_argument("--media", type=Path, help="the reels volume, where a made Reel's render waits")
        command.add_argument("--stored", type=Path, help="JSON list of the Reels whose render is in the releases")
        command.add_argument("--until", type=datetime.fromisoformat, help="start no step that can't finish by then")
    args = parser.parse_args()

    from .cli import PIPELINE_ROOT, _load_env

    _load_env(PIPELINE_ROOT / ".env")
    stored = set(json.loads(args.stored.read_text(encoding="utf-8"))) if args.stored else None
    if args.command == "status":
        state = _read(args.state) or {}
        for key in ("reels", "made", "alerts"):
            state.setdefault(key, {})
        reel, done = decide(state, datetime.now(timezone.utc), args.force, args.media, stored)
        print(json.dumps(done | {"would_make": reel, "readable_left": readable_left()}, ensure_ascii=False))
        return
    print(json.dumps(work(args.state, args.logs, args.force, args.media, stored, args.until), ensure_ascii=False))


if __name__ == "__main__":
    main()
