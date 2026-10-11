"""@ankahi.kathayein's Reel factory: makes the next backlog story into a Reel whenever the Instagram calendar runs
short, so every slot (07:30, 13:00 and 20:00 IST) has one and the YouTube and Facebook mirrors have them too.

A factory run (reel_host.factory, on CircleCI; reel_cloud.factory_run on Modal) takes a fresh clone and calls `work`
on it. `work` makes at most
one Reel, at most DAILY_CAP a day, and only while the supply is short: fewer than PENDING_MIN made Reels waiting for
the judge or a slot, or fewer than AHEAD_MIN booked on Instagram and waiting together. It finishes a Reel an earlier
run left half-made, or writes the next backlog story (moving its row to Made), then picks the pictures, renders,
lets QA fix the frames, and reports the Reel as made; the run then puts the render in the reels volume and pushes the
folder, and the hourly reel_run judges and books it. These steps and the judge share the brand's daily Gemini quota
(reset 12:30 IST), so a quota error pauses the factory until then instead of counting as a failure, and so does a
Gemini outage or a network drop, until it has happened TRANSIENT_GIVE_UP times to the same step. A step that fails
GIVE_UP times for any other reason sets the Reel aside, and so does one pictures failure that leaves most lines
without art although the searches found plenty (_no_art); a story the writer can't write moves to "## Set aside".
With --until (CircleCI stops a job at one hour), a step that can't finish by then waits for the next run. When
BACKLOG_LOW or fewer stories the writer can read are left, reel_backlog adds new ones that check out.

The state (each Reel's progress and failures, Reels made per day, the quota pause, alerts sent) is kept by the run
(content/instagram/automation/factory.json on CircleCI); `work` reads and writes it as the JSON file the run hands
over. A FACTORY_PAUSE file next to the backlog stops the factory.

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

from . import reel_writer, reels

IST = ZoneInfo("Asia/Kolkata")
PENDING_MIN = 3
AHEAD_MIN = 10
# Instagram has 3 slots a day; one more a day catches up on empty ones.
DAILY_CAP = 4
GIVE_UP = 3
TRANSIENT_GIVE_UP = 8
BACKLOG_LOW = 4
STUCK = timedelta(days=5)
RESET = (12, 40)
QUOTA = re.compile(r"OutOfQuota|free quota left today|RESOURCE_EXHAUSTED", re.I)
TRANSIENT = re.compile(r"Overloaded|\b503\b|UNAVAILABLE|high demand|Read timed out|TimeoutError|NameResolution|"
                       r"ClientConnector|ConnectionError|Connection reset|RemoteDisconnected|"
                       r"Temporary failure in name resolution|SSLError", re.I)
NO_PICTURES = re.compile(r"no usable picture for lines \[([\d, ]+)\]")
SEARCHED = re.compile(r"(\d+) candidates for (\d+) lines")
PAUSE = reels.REELS / "FACTORY_PAUSE"
BACKLOG = reels.REELS / "backlog.md"
RECORD = "instagram.json"
# Minutes a step needs on CircleCI's 4-CPU machine; with --until, a step isn't started with less time left.
NEEDS = {"write": 10, "pictures": 20, "render": 30, "qa": 40}


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
    review = _read(folder / reels.REVIEW)
    if not review or sum(r.get("judge") == reels.JUDGE for r in review.get("runs") or []) < reels.RUNS:
        return False
    try:
        return not reels.passes(review, folder.name, with_bar=False)
    except Exception:
        return False


def supply(state: dict, now: datetime, media: Path | None = None, stored: set[str] | None = None) -> dict:
    """Reels booked on Instagram from now on, Reels made and waiting for the judge or a slot, those judged and
    failed or waiting longer than STUCK, and the factory's half-made ones. With `media` (the reels volume) or
    `stored` (the Reels whose render is in the releases), a Reel waits only once its render is there, as the hourly
    run needs."""
    found = {"booked": [], "pending": [], "failed": [], "stuck": [], "unfinished": []}
    for folder in sorted(p for p in reels.REELS.glob("ak*") if p.is_dir()):
        mine = state["reels"].get(folder.name, {})
        if mine and not mine.get("pushed") and not mine.get("set_aside"):
            found["unfinished"].append(folder.name)
            continue
        if not (folder / "short.yaml").exists() or mine.get("set_aside"):
            continue
        if record := _read(folder / RECORD):
            if not record.get("withdrawn") and (when := _due(record)) and when > now:
                found["booked"].append(folder.name)
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
    return sum(bool(reel_writer.source_urls(row["source"])) for row in reel_writer.backlog())


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


def next_reset(now: datetime) -> datetime:
    reset = now.astimezone(IST).replace(hour=RESET[0], minute=RESET[1], second=0, microsecond=0)
    return reset if reset > now else reset + timedelta(days=1)


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


def _steps(reel: str) -> list[tuple[str, object, list[str], int]]:
    folder = reels.REELS / reel
    video = folder / f"{reel}.mp4"
    py = [sys.executable, "-u", "-m"]
    return [
        ("write", lambda: (folder / reel_writer.SCRIPT).exists(), [*py, "ytc.reel_writer", "write", "--id", reel],
         30 * 60),
        ("pictures", lambda: (folder / "short.yaml").exists(), [*py, "ytc.reel_pictures", str(folder)], 60 * 60),
        ("render", lambda: video.exists() and video.stat().st_mtime >= (folder / "short.yaml").stat().st_mtime,
         [*py, "ytc.reel_tools", "render", str(folder / "short.yaml")], 90 * 60),
        ("qa", lambda: (_read(folder / "work" / "qa.json") or {}).get("finished", False),
         [*py, "ytc.reel_qa", str(folder)], 120 * 60),
    ]


def make(reel: str, state: dict, logs: Path, done: dict, until: datetime | None = None) -> str:
    """Take one Reel as far as it goes; "made", "quota", "failed", "set aside", or "paused" when the next step
    can't finish by `until`."""
    folder = reels.REELS / reel
    mine = state["reels"].setdefault(reel, {"started": datetime.now(IST).isoformat(timespec="seconds"), "fails": {}})
    if (script := _read(folder / reel_writer.SCRIPT)) and script.get("row"):
        move_row(reel, script["row"])  # a resumed Reel's row, moved on a clone that was never pushed
    for step, finished, command, timeout in _steps(reel):
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
        code, tail = _run(command, logs / f"{reel}_{step}.log", timeout)
        print(f"{reel} {step}: exit {code} after {time.time() - started:.0f} s", flush=True)
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
        if QUOTA.search(last):
            state["cooldown_until"] = next_reset(datetime.now(timezone.utc)).isoformat()
            done["why"] = f"{step} hit the Gemini quota; paused until {state['cooldown_until']}"
            return "quota"
        transient = mine.setdefault("transient", {})
        if code == -1 and cut and transient.get(step, 0) < TRANSIENT_GIVE_UP:
            transient[step] = transient.get(step, 0) + 1
            done["why"] = f"{step} ran out of this run's time ({transient[step]} of {TRANSIENT_GIVE_UP})"
            return "failed"
        if code != -1 and TRANSIENT.search(last) and transient.get(step, 0) < TRANSIENT_GIVE_UP:
            transient[step] = transient.get(step, 0) + 1
            done["why"] = f"{step} hit an outage or network error ({transient[step]} of {TRANSIENT_GIVE_UP}): {last}"
            return "failed"
        mine["fails"][step] = mine["fails"].get(step, 0) + 1
        if step == "pictures" and _no_art(failed_log.read_text(encoding="utf-8", errors="replace"), last):
            mine["fails"][step] = GIVE_UP
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


def _no_art(log_text: str, last: str) -> bool:
    """Whether a pictures failure left most lines without a picture although the searches found plenty: the
    collections have no art for the story, and another try finds the same (Savitri: 8 of 13 lines, four tries)."""
    missing, searched = NO_PICTURES.search(last), SEARCHED.search(log_text)
    if not missing or not searched:
        return False
    candidates, lines = int(searched[1]), int(searched[2])
    return candidates >= 5 * lines and len(missing[1].split(",")) > lines / 2


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
    if not found["pending"] and not found["unfinished"] and (empty := [d for d in soon if d not in days]):
        done["alerts"].append(("dry", f"no Reel is booked for {', '.join(f'{d:%a %d %b}' for d in empty)} and none "
                               "is waiting"))
    if found["stuck"]:
        done["alerts"].append(("stuck", f"{', '.join(found['stuck'])} made over {STUCK.days} days ago and still not "
                               "booked"))
    if (left := readable_left()) <= 1:
        done["alerts"].append(("backlog", f"only {left} stories the writer can read are left in the backlog, and "
                               f"refilling it didn't work ({state.get('refill_error') or 'no new story checked out'})"))
    short = len(found["pending"]) < PENDING_MIN or len(found["booked"]) + len(found["pending"]) < AHEAD_MIN
    if not (short or force or found["unfinished"]):
        return None, done | {"decision": "supply is enough"}
    today = f"{now.astimezone(IST):%Y-%m-%d}"
    if state["made"].get(today, 0) >= DAILY_CAP and not force:
        return None, done | {"decision": f"already made {DAILY_CAP} today"}
    if found["unfinished"]:
        return found["unfinished"][0], done | {"decision": "resuming"}
    if not reel_writer.next_story():
        done["alerts"].append(("backlog", "the backlog has no story the writer can read; no Reel made"))
        return None, done | {"decision": "no readable story"}
    return reel_writer.next_id(), done | {"decision": "starting"}


def refill(state: dict, now: datetime) -> list[str]:
    """When BACKLOG_LOW or fewer readable stories are left, add new ones (reel_backlog), once a day unless the Gemini
    quota stopped it; the stories added."""
    today = f"{now.astimezone(IST):%Y-%m-%d}"
    if PAUSE.exists() or state.get("refilled") == today or readable_left() > BACKLOG_LOW:
        return []
    if (until := state.get("cooldown_until")) and datetime.fromisoformat(until) > now:
        return []
    from . import llm, reel_backlog

    try:
        found = reel_backlog.refill()
    except llm.OutOfQuota:
        return []
    except Exception as err:
        state["refilled"], state["refill_error"] = today, f"{type(err).__name__}: {err}"[:300]
        return []
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
    reel, done = decide(state, now, force, media, stored)
    if refilled:
        done["refilled"] = refilled
    if reel:
        done["reel"] = reel
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
