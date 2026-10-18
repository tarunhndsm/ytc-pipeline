"""@ankahi.kathayein's Reels on Modal, so they keep going out while the Mac is closed.

Every hour `reel_run` takes a fresh clone of the repo, reviews each Reel the Mac rendered that is still missing
judgments (reels.review, on the brand's own Gemini project), schedules the ones that pass through Buffer
(instagram.schedule) so that every day has a Reel before any day has two and the queue stays full (free_slots,
fill_queue), gives each Reel Instagram has to the brand's YouTube channel as a Short at YouTube's own evening times
(instagram.mirror, lineup, spread), as the Reel's shorter YouTube cut once one is rendered (cuts), and mirrors each
new Reel to the brand's Facebook Page (facebook_mirror). Each run also checks Buffer's channels and sends a failed
post again once (buffer_health), and takes the videos of Reels all three channels had off Cloudinary (unhost_sent).
It pushes review.json, instagram.json, youtube.json, facebook.json and the Facebook mirror's start time. Once a day it
saves the YouTube channel's numbers and keeps its playlists (channel_upkeep). The work runs from the clone's code, so
a pushed fix reaches the next run without a deploy.

`factory_run` makes the Reels themselves (reel_factory: write, pictures, render, QA) whenever the Instagram calendar
runs short, puts each render in the reels volume and pushes its folder, so nothing waits for the Mac. It is scheduled
only by a deploy with AK_FACTORY_CRON set (the brand's own Modal workspace); `factory --ephemeral` runs it once
anywhere.

git leaves a Reel's video and narration out, so after each render the Mac puts them in the reels volume, then pushes
the Reel's folder:

    python -m ytc.reel_cloud upload content/instagram/ak011
    python -m ytc.reel_cloud deploy
    python -m ytc.reel_cloud run [--dry]     # one run now; --dry says what it would do and changes nothing
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import modal

from . import cloud, llm

log = logging.getLogger(__name__)

APP_NAME = "ankahi-kathayein"
app = modal.App(APP_NAME, include_source=False)
# The brand's Gemini, Buffer and Instagram keys and its YouTube channel's sign-in, with the studio's
# Cloudinary account, ntfy topic and deploy key; made from the Mac with `python3 pipeline/modal_secret.py brand`.
secret = modal.Secret.from_name("ankahi-kathayein")
media = modal.Volume.from_name("ankahi-kathayein-reels", create_if_missing=True)
# The day of the last "running dry" alert, so the phone hears it once a day.
state = modal.Dict.from_name("ankahi-kathayein-state", create_if_missing=True)
MEDIA_ROOT = Path("/reels")
# What git leaves out of a render, in the order a run copies it: check.check trusts the saved speech check only if it
# is newer than the narration.
MEDIA = ("{id}.mp4", "work/narration.wav", "work/speech.json")
# Uploaded with them, so a run can tell whether the folder in git is this render's.
RENDER = "work/manifest.json"
# At 15 past every hour (45 past in IST), so a post that goes out is replaced within the hour: 12:45 IST comes a
# quarter hour after Gemini's daily reset gives the judge its quota back, and no run meets the studio's (35 past).
SCHEDULE = "15 * * * *"
RUN_MINUTES = 45
# A published Reel's render stays in the volume this long after its slot, for a post Buffer has to send again.
KEEP = timedelta(days=2)

IST = ZoneInfo("Asia/Kolkata")
SLOTS = ((7, 30), (13, 0), (20, 0))
# YouTube's own times: India watches Shorts most from 19:00 to 22:00 IST. On 1 Oct the channel's Short at 20:00 had
# 1,213 views against 42 at 07:30 and 1 at 13:00, while Instagram's 07:30 slot drew 65.5k plays for ak002.
YOUTUBE_SLOTS = ((18, 30), (20, 0), (21, 30))
# A day's first Reel goes out in the evening, then at noon, then in the morning; a day's first Short at 20:00.
EVENING_FIRST = sorted(SLOTS, reverse=True)
YOUTUBE_FIRST = ((20, 0), (18, 30), (21, 30))
# Every day up to COVER_DAYS ahead (today is the first) gets a post before any day gets a second, and only days up
# to FULL_DAYS ahead get a second or third: a day with nothing costs a channel more than a day with one fewer, and
# posts spread ahead keep the days covered while the studio stalls. Up to 3 + 3 + 2 + 2 Reels wait, QUEUE of them.
COVER_DAYS, FULL_DAYS = 4, 4
# The owner hears of a day without a post coming only from this hour (IST), not at night.
ALERT_FROM = 12
# Buffer's free plan holds 10 scheduled posts a channel, and the owner wants all 10 kept full (1 Oct).
QUEUE = 10
# A slot sooner than this is left alone: hosting the video and handing it to Buffer takes minutes.
LEAD = timedelta(minutes=30)
# Reels Instagram had before the Facebook mirror began go to the Page as well (owner, 7 Oct: the same Reels on both).
# The catch-up books them before the mirror runs, at these times on days under the mirror's Reels-a-day cap, and
# leaves queue room for the Instagram Reels due within CATCH_UP_RESERVE, which the mirror places at Instagram's time.
CATCH_UP_SLOTS = ((10, 0), (16, 0), (19, 0), (21, 0), (22, 30))
CATCH_UP_PER_DAY = len(SLOTS)
CATCH_UP_DAYS = 4
CATCH_UP_RESERVE = timedelta(hours=48)
# Fewer Reels than this left to go out, and the owner hears the queue is running dry.
LOW = 3
# He hears of a thin queue or a day without a post only once no Reel has been booked for this long: while Reels are
# booked every few hours the queue refills by itself, and the alert only worried him (17 Oct).
STALLED_AFTER = timedelta(hours=8)
# While older Reels can go out again (reel_repost), no day goes without a post, so he hears only of a longer stall.
STALLED_COVERED = timedelta(hours=48)
PROBLEM_HOURS = 3
# A post Buffer failed goes out again once, this long after the run that finds it; one due longer ago than
# RESEND_WITHIN is left to the owner.
RESEND_AFTER, RESEND_WITHIN = timedelta(minutes=10), timedelta(days=1)
# Buffer fails a post only at its time, so its check (buffer_health) runs after a post was due in the last
# HEALTH_SINCE, and every HEALTH_HOURS for the channels themselves: the key has 3,000 requests in 30 days.
HEALTH_SINCE, HEALTH_HOURS = timedelta(minutes=90), 6
# The brand YouTube channel's daily numbers (youtube.stats), under the Reels' folder: subscribers and 90-day views
# toward the Partner Program, and each Short's retention.
STATS = Path("stats") / "youtube"
# Shorts pinned to a YouTube slot, under the Reels' folder, as {"ak002": "2026-10-01T20:00:00+05:30"}: a Reel that
# did well on Instagram (ak002 drew 66.5k plays) goes out at YouTube's evening peak. A pin in the past does nothing.
PINS = "youtube_pins.json"


# --- the work, on a checkout -------------------------------------------------------------------------------------

def _ist(stamp: str) -> str:
    return f"{datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(IST):%a %d %b %H:%M} IST"


def _due(folder: Path, youtube: bool = False) -> datetime | None:
    """When the Reel goes out on Instagram, or on YouTube; None for a Short taken off the channel."""
    from . import instagram

    record = folder / (instagram.YOUTUBE_RECORD if youtube else instagram.RECORD)
    if not record.exists():
        return None
    saved = json.loads(record.read_text(encoding="utf-8"))
    if saved.get("withdrawn"):
        return None
    return datetime.fromisoformat(saved["dueAt"].replace("Z", "+00:00"))


def _wants_short(folder: Path) -> bool:
    """Whether the Reel still needs a Short: it has none, or the one taken off the channel has its cut now."""
    from . import instagram

    record = instagram.youtube_record(folder)
    return record is None or bool(record.get("withdrawn") and instagram.cut(folder))


def _spec_of(folder: Path) -> dict:
    import yaml

    path = folder / "short.yaml"
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def _answers(folder: Path) -> str:
    """The Reel whose question this one answers (reel_writer's quiz from Reel to Reel), or ""."""
    return (_spec_of(folder).get("answers") or {}).get("reel") or ""


def _chained(folder: Path) -> bool:
    """Whether a Reel is in the quiz chain, whose posts keep their order on every channel."""
    spec = _spec_of(folder)
    return bool(spec.get("quiz") or spec.get("answers"))


def unanswered() -> set[str]:
    """Reels whose question no Reel answers, though a later Reel answers an earlier one (reel_writer.asked_before
    passed them over while the judge had failed them): sent now, their question would never get its answer."""
    from . import reel_writer, reels

    made = {path.parent.name: _spec_of(path.parent) for path in reels.REELS.glob("ak*/short.yaml")}
    answered = {(spec.get("answers") or {}).get("reel") for spec in made.values()}
    skips = [(spec["answers"]["reel"], reel) for reel, spec in made.items()
             if (spec.get("answers") or {}).get("reel") and not reel_writer.turned_down(reels.REELS / reel)]
    return {reel for reel, spec in made.items() if spec.get("quiz") and reel not in answered
            and any(before < reel < later for before, later in skips)}


def _after(folder: Path, booked: dict[str, datetime], gone: set[str]) -> tuple[datetime | None, str]:
    """When a Reel that answers another's question may go out on Instagram: after that one. (None, "") when it
    answers none, or one no viewer will see; (None, why) while that one waits."""
    from . import instagram, reel_writer, reels

    if not (before := _answers(folder)):
        return None, ""
    if before in booked:
        return booked[before], ""
    record = reels._read(reels.REELS / before / instagram.RECORD)
    if record and not record.get("withdrawn"):
        return datetime.fromisoformat(record["dueAt"].replace("Z", "+00:00")), ""
    if record or before in gone or reel_writer.turned_down(reels.REELS / before):
        return None, ""
    return None, f"waiting for {before}, whose question it answers"


def _youtube_after(folder: Path, mirrored: dict[str, datetime]) -> tuple[datetime | None, str]:
    """When a Reel that answers another's question may go out on YouTube: after that one's Short, as _after."""
    from . import reels

    if not (before := _answers(folder)):
        return None, ""
    if before in mirrored:
        return mirrored[before], ""
    earlier = reels.REELS / before
    if due := _due(earlier, youtube=True):
        return due, ""
    if _due(earlier) and _wants_short(earlier):
        return None, f"waiting for {before}'s Short, whose question it answers"
    return None, ""


def _facebook_after(folder: Path, sent: dict[str, datetime], start: datetime) -> tuple[datetime | None, str]:
    """When a Reel that answers another's question may go out on Facebook: after that one, as _after."""
    from . import facebook, reels

    if not (before := _answers(folder)):
        return None, ""
    if before in sent:
        return sent[before], ""
    earlier = reels.REELS / before
    if (record := facebook.record(earlier)) and record.get("dueAt"):
        return datetime.fromisoformat(record["dueAt"].replace("Z", "+00:00")), ""
    if facebook.auto_candidate(earlier, start):
        return None, f"waiting for {before} on Facebook, whose question it answers"
    return None, ""


def _dues(youtube: bool = False) -> list[datetime]:
    from . import reel_repost, reels

    found = [due for folder in reels.REELS.iterdir() if folder.is_dir() and (due := _due(folder, youtube))]
    return sorted(found if youtube else found + reel_repost.dues())


def _minute(when: datetime) -> datetime:
    return when.astimezone(IST).replace(second=0, microsecond=0)


def _open(day: date, order: list[tuple[int, int]], taken: set[datetime], now: datetime) -> datetime | None:
    """The first of a day's slots, in `order`, that nothing has and that is still LEAD away."""
    for hour, mins in order:
        slot = datetime(day.year, day.month, day.day, hour, mins, tzinfo=IST)
        if slot > now + LEAD and slot not in taken:
            return slot
    return None


def _ahead(now: datetime, days: int) -> list[date]:
    """The next `days` days, from today while it still has a slot LEAD away."""
    first = now.astimezone(IST).date()
    if (now + LEAD).astimezone(IST) >= datetime(first.year, first.month, first.day, *max(SLOTS + YOUTUBE_SLOTS),
                                                tzinfo=IST):
        first += timedelta(days=1)
    return [first + timedelta(days=n) for n in range(days)]


def youtube_slot(due: datetime, now: datetime, planned: list[datetime], after: datetime | None = None) -> datetime:
    """A free YouTube slot for a Reel due on Instagram at `due`: on that day while one is still LEAD away, else on the
    soonest day after. A Reel already out on Instagram catches up on the day up to COVER_DAYS ahead with the fewest
    Shorts, the soonest of those. A Reel that answers another's question takes the first open slot after `after`,
    that one's Short."""
    taken = {_minute(when) for when in _dues(youtube=True) + planned}
    if after is not None:
        first = max(due, now + LEAD, after).astimezone(IST).date()
        for day in (first + timedelta(days=n) for n in range(14)):
            for hour, mins in YOUTUBE_SLOTS:
                slot = datetime(day.year, day.month, day.day, hour, mins, tzinfo=IST)
                if slot > after and slot > now + LEAD and slot not in taken:
                    return slot
        raise RuntimeError(f"YouTube has no free slot in the two weeks from {first:%d %b}")
    if due > now + LEAD:
        first = due.astimezone(IST).date()
        days = [first + timedelta(days=n) for n in range(14)]
    else:
        per_day = Counter(when.date() for when in taken)
        soon = _ahead(now, COVER_DAYS)
        days = sorted(soon, key=lambda d: per_day[d]) + [soon[-1] + timedelta(days=n) for n in range(1, 14)]
    for day in days:
        if slot := _open(day, YOUTUBE_FIRST, taken, now):
            return slot
    raise RuntimeError(f"YouTube has no free slot in the two weeks from {days[0]:%d %b}")


def mirror(now: datetime, done: dict, dry: bool = False) -> list[datetime]:
    """Schedule each Reel Instagram has on the brand's YouTube channel too; the slots it took."""
    from . import instagram, publish, reels

    planned: list[datetime] = []
    waiting = [(due, folder) for folder in reels.REELS.iterdir()
               if folder.is_dir() and _wants_short(folder) and (due := _due(folder))]
    if not waiting:
        return planned
    try:
        buffered = instagram.posts_by_reel(instagram._env("IG_BUFFER_YOUTUBE_CHANNEL_ID"))
    except Exception as err:
        done["errors"].append(f"couldn't read Buffer's YouTube posts, so none were made: {err}"[:300])
        return planned
    pins = _pins(now)
    # Pinned Shorts first, since the queue frees one place per Short sent; then the Reels still to go out on
    # Instagram, so each keeps its slot; the ones already out catch up after.
    waiting.sort(key=lambda item: (item[1].name not in pins, item[0] <= now + LEAD, item[0]))
    mirrored: dict[str, datetime] = {}
    for due, folder in waiting:
        reel = folder.name
        withdrawn = (folder / instagram.YOUTUBE_RECORD).exists()
        # Buffer keeps a withdrawn Short's post as sent: only its cut's post is that Reel's Short now.
        if (post := buffered.get(reel)) and (not withdrawn or instagram.HOSTED_REEL.search(post["media_url"]).group(2)):
            if not dry:
                instagram.adopt(folder, post, youtube=True)
            done["mirrored"][reel] = f"{_ist(post['dueAt'])} (already in Buffer)"
            continue
        if len({d for d in _dues(youtube=True) if d > now} | set(planned)) >= QUEUE:
            done["held"][f"{reel} (YouTube)"] = "waiting for room in the YouTube queue"
            continue
        after, why = _youtube_after(folder, mirrored)
        if why:
            done["held"][f"{reel} (YouTube)"] = why
            continue
        taken = {_minute(when) for when in _dues(youtube=True) + planned}
        slot = pins[reel] if reel in pins and pins[reel] not in taken and not after else \
            youtube_slot(due, now, planned, after)
        planned.append(slot)
        mirrored[reel] = slot
        if dry:
            done["mirrored"][reel] = f"would go out on YouTube {slot.astimezone(IST):%a %d %b %H:%M} IST"
            continue
        try:
            instagram.mirror(folder / "short.yaml", slot)
            done["mirrored"][reel] = f"{slot.astimezone(IST):%a %d %b %H:%M} IST"
        except publish.BufferBusy as err:
            done["held"][f"{reel} (YouTube)"] = f"Buffer is busy ({err})"
            return planned
        except Exception as err:
            log.exception("mirroring %s to YouTube failed", reel)
            done["errors"].append(f"mirroring {reel} to YouTube failed: {err}"[:300])
            mirrored.pop(reel, None)
    return planned


def facebook_slot(due: datetime, now: datetime, taken: set[datetime], per_day: Counter,
                  after: datetime | None = None) -> datetime | None:
    """Keep Instagram's slot on Facebook when free; otherwise use the next balanced Reel slot on or after it. A Reel
    that answers another's question goes out after `after`, that one's post."""
    exact = _minute(due)
    if exact > now + LEAD and exact not in taken and per_day[exact.date()] < len(SLOTS) and \
            (after is None or exact > after):
        return exact
    first = max(due, now + LEAD, after or due).astimezone(IST).date()
    for have, days in ((0, COVER_DAYS), (1, FULL_DAYS), (2, FULL_DAYS)):
        for offset in range(days):
            day = first + timedelta(days=offset)
            if per_day[day] > have:
                continue
            for hour, minute in (SLOTS if after else EVENING_FIRST):
                slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)
                if slot > now + LEAD and slot >= due and slot not in taken and (after is None or slot > after):
                    return slot
    return None


def facebook_mirror(now: datetime, done: dict, dry: bool = False) -> None:
    """Follow Instagram Reels from the first enabled run, including ones held while Facebook had no room."""
    from . import facebook, instagram, publish, reels

    start = facebook.live_start(reels.REELS, now, dry)
    waiting = [(due, folder) for folder in reels.REELS.glob("ak*") if folder.is_dir()
               and (due := facebook.auto_candidate(folder, start))]
    if not waiting:
        return
    try:
        posts = facebook.channel_posts(instagram._env("IG_BUFFER_FACEBOOK_CHANNEL_ID"), now)
    except publish.BufferBusy as err:
        for _, folder in waiting:
            done["held"][f"{folder.name} (Facebook)"] = f"Buffer is busy ({err})"
        return
    except Exception as err:
        done["errors"].append(f"couldn't read Buffer's Facebook posts, so none were made: {err}"[:300])
        return
    buffered = facebook.reel_posts(posts)
    queued, taken, per_day = facebook.queued(posts, reels.REELS, now)
    waiting.sort(key=lambda item: item[0])
    sent: dict[str, datetime] = {}
    for due, folder in waiting:
        reel = folder.name
        if post := buffered.get(reel):
            if not dry:
                facebook.adopt(folder, post)
            done["facebook"][reel] = f"{_ist(post['dueAt'])} (already in Buffer)"
            continue
        if len(queued) >= facebook.QUEUE:
            done["held"][f"{reel} (Facebook)"] = "waiting for room in the Facebook queue"
            continue
        after, why = _facebook_after(folder, sent, start)
        if why:
            done["held"][f"{reel} (Facebook)"] = why
            continue
        if not (slot := facebook_slot(due, now, taken, per_day, after)):
            done["held"][f"{reel} (Facebook)"] = f"no free Facebook Reel slot in the next {FULL_DAYS} days"
            continue
        if dry:
            queued.add(f"planned:{reel}")
            taken.add(slot)
            per_day[slot.date()] += 1
            sent[reel] = slot
            done["facebook"][reel] = f"would go out on Facebook {slot:%a %d %b %H:%M} IST"
            continue
        try:
            saved = facebook.schedule(folder / "short.yaml", slot)
        except publish.BufferBusy as err:
            done["held"][f"{reel} (Facebook)"] = f"Buffer is busy ({err})"
            return
        except Exception as err:
            log.exception("mirroring %s to Facebook failed", reel)
            done["errors"].append(f"mirroring {reel} to Facebook failed: {err}"[:300])
            continue
        queued.add(saved["id"])
        actual = _minute(datetime.fromisoformat(saved["dueAt"].replace("Z", "+00:00")))
        taken.add(actual)
        per_day[actual.date()] += 1
        sent[reel] = actual
        done["facebook"][reel] = f"{actual:%a %d %b %H:%M} IST"


def facebook_catch_up(now: datetime, done: dict, dry: bool = False) -> None:
    """Put the pre-mirror Instagram Reels on Facebook too, leaving the mirror room for the Reels due soon."""
    from . import facebook, reels

    if not facebook.past_reels(reels.REELS, now):
        return
    result = facebook.backfill(reels.REELS, now, per_day=CATCH_UP_PER_DAY, days=CATCH_UP_DAYS, dry=dry,
                               slots=CATCH_UP_SLOTS, reserve_within=CATCH_UP_RESERVE)
    for reel in result["adopted"]:
        done["facebook"][reel] = "already in Buffer"
    for item in result["planned"]:
        done["facebook"][item["reel"]] = _ist(item["facebook_at"]) + " (catch-up)"
    waiting = [f"{len(result['held'])} wait: {result['held'][0]['reason']}"] if result["held"] else []
    if result["unverified"]:
        waiting.append(f"not seen sent on Instagram in Buffer: {', '.join(result['unverified'])}")
    if waiting:
        done["held"]["Facebook catch-up"] = "; ".join(waiting)


def cuts(now: datetime, done: dict, dry: bool = False) -> None:
    """Make each Short still to go out its Reel's YouTube cut, once the cut is rendered, at the same time."""
    from . import instagram, publish, reels

    for folder in sorted(reels.REELS.glob("ak*")):
        record = instagram.youtube_record(folder) if folder.is_dir() else None
        due = _due(folder, youtube=True) if record else None
        if not due or due <= now + LEAD or record.get("cut") or not instagram.cut(folder):
            continue
        if not dry:
            try:
                instagram.send_cut(folder)
            except publish.BufferBusy as err:
                done["held"][f"{folder.name} (YouTube)"] = f"its cut waits; Buffer is busy ({err})"
                return
            except Exception as err:
                log.exception("sending %s's YouTube cut failed", folder.name)
                done["errors"].append(f"sending {folder.name}'s YouTube cut failed: {err}"[:300])
                continue
        done["moved"][folder.name] = f"{'would go' if dry else 'goes'} out as its YouTube cut, {_ist(record['dueAt'])}"


def _pins(now: datetime) -> dict[str, datetime]:
    """The Shorts pinned to a slot still LEAD away, and their slots."""
    from . import reels

    path = reels.REELS / PINS
    pins = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {reel: _minute(when) for reel, stamp in pins.items() if (when := datetime.fromisoformat(stamp)) > now + LEAD}


def _evening_slot(now: datetime, taken: set[datetime], first: date | None = None) -> datetime | None:
    """An open slot at YouTube's times: on `first` if it has one, else on the day up to COVER_DAYS ahead with the
    fewest Shorts, the soonest of those, else on the days after."""
    per_day = Counter(when.date() for when in taken)
    soon = _ahead(now, COVER_DAYS)
    days = [first] * bool(first) + sorted(soon, key=lambda d: per_day[d]) + \
        [soon[-1] + timedelta(days=n) for n in range(1, 14)]
    return next((slot for day in days if (slot := _open(day, YOUTUBE_FIRST, taken, now))), None)


def _move(folder: Path, slot: datetime, why: str, done: dict, dry: bool) -> bool:
    """Move a Reel's Short on YouTube; False when it couldn't, so the run moves nothing more: a pinned Short would
    otherwise join the one that failed to make way."""
    from . import instagram, publish

    when = _minute(_due(folder, youtube=True))
    if not dry:
        try:
            instagram.move_short(folder, slot)
        except publish.BufferBusy as err:
            done["held"][f"{folder.name} (YouTube)"] = f"not moved; Buffer is busy ({err})"
            return False
        except Exception as err:
            log.exception("moving %s on YouTube failed", folder.name)
            done["errors"].append(f"moving {folder.name} on YouTube failed: {err}"[:300])
            return False
    done["moved"][folder.name] = (f"{'would move' if dry else 'moved'} from {when:%a %d %b %H:%M} to "
                                  f"{slot.astimezone(IST):%a %d %b %H:%M} IST, {why}")
    return True


def lineup(now: datetime, done: dict, planned: list[datetime], dry: bool = False) -> None:
    """Move each Short still to go out to the slot it is pinned to (the Short there makes way, to the day with the
    fewest), then each one outside YOUTUBE_SLOTS to one of them, that day while it has room; the slots they move to
    join `planned`."""
    from . import reels

    queued = {folder: _minute(yt) for folder in reels.REELS.iterdir() if folder.is_dir()
              and (yt := _due(folder, youtube=True)) and yt > now + LEAD}
    taken = {_minute(when) for when in _dues(youtube=True) + planned}
    pins = _pins(now)
    moves: list[tuple[Path, datetime, str]] = []
    for reel, pin in sorted(pins.items(), key=lambda p: p[1]):
        folder = reels.REELS / reel
        if folder not in queued or queued[folder] == pin or _chained(folder):
            continue
        there = next((f for f, when in queued.items() if when == pin), None)
        # A Short of the quiz chain keeps its place: moved, it could come before the one whose question it answers.
        if (there is None and pin in taken) or (there is not None and (there.name in pins or _chained(there))):
            done["held"][f"{reel} (YouTube)"] = f"pinned to {pin:%a %d %b %H:%M} IST, which another Short keeps"
            continue
        taken.discard(queued[folder])
        if there is not None:
            if not (slot := _evening_slot(now, taken)):
                taken.add(queued[folder])
                continue
            moves.append((there, slot, f"to make way for {reel}"))
            queued[there] = slot
            taken.add(slot)
        moves.append((folder, pin, "where it is pinned"))
        queued[folder] = pin
        taken.add(pin)
    for folder, when in sorted(queued.items(), key=lambda q: q[1]):
        if (when.hour, when.minute) in YOUTUBE_SLOTS or folder.name in pins or _chained(folder):
            continue
        taken.discard(when)
        if not (slot := _evening_slot(now, taken, when.date())):
            taken.add(when)
            continue
        moves.append((folder, slot, "into YouTube's evening hours"))
        queued[folder] = slot
        taken.add(slot)
    for folder, slot, why in moves:
        if not _move(folder, slot, why, done, dry):
            return
        planned.append(slot)


def spread(now: datetime, done: dict, planned: list[datetime], dry: bool = False) -> None:
    """Move Shorts still to go out from a day with two or more to a day up to COVER_DAYS ahead that has none; the
    slots they move to join `planned`. A pinned Short stays."""
    from . import instagram, publish, reels

    taken = {_minute(when) for when in _dues(youtube=True) + planned}
    pinned = _pins(now)
    movable = {folder: _minute(yt) for folder in reels.REELS.iterdir() if folder.is_dir()
               and (yt := _due(folder, youtube=True)) and yt > now + LEAD and folder.name not in pinned
               and not _chained(folder)}
    for day in _ahead(now, COVER_DAYS):
        per_day = Counter(when.date() for when in taken)
        if per_day[day] or not (slot := _open(day, YOUTUBE_FIRST, taken, now)):
            continue
        crowded = [(per_day[when.date()], when, folder) for folder, when in movable.items() if per_day[when.date()] > 1]
        if not crowded:
            return
        _, when, folder = max(crowded, key=lambda c: (c[0], c[1]))
        if not dry:
            try:
                instagram.move_short(folder, slot)
            except publish.BufferBusy as err:
                done["held"][f"{folder.name} (YouTube)"] = f"not moved; Buffer is busy ({err})"
                return
            except Exception as err:
                log.exception("moving %s on YouTube failed", folder.name)
                done["errors"].append(f"moving {folder.name} on YouTube failed: {err}"[:300])
                return
        done["moved"][folder.name] = (f"{'would move' if dry else 'moved'} from {when:%a %d %b %H:%M} to "
                                      f"{slot:%a %d %b %H:%M} IST, so {day:%a %d %b} has a Short")
        taken.discard(when)
        taken.add(slot)
        movable[folder] = slot
        planned.append(slot)


def free_slots(now: datetime, count: int, planned: list[datetime] | None = None,
               after: datetime | None = None) -> list[datetime]:
    """The Instagram slots the next `count` Reels take, from LEAD after `now`: the evening of the soonest day up to
    COVER_DAYS ahead that has no Reel, else another slot on the soonest day up to FULL_DAYS ahead with the fewest.
    A Reel that answers another's question takes the first open slot after `after`, that one's. Fewer than `count`
    when those days are full."""
    taken = {_minute(due) for due in _dues() + (planned or [])}
    free: list[datetime] = []
    while len(free) < count and (slot := _reel_slot(now, taken, after)):
        free.append(slot)
        taken.add(slot)
    return free


def _reel_slot(now: datetime, taken: set[datetime], after: datetime | None = None) -> datetime | None:
    if after is not None:
        first = max(after, now).astimezone(IST).date()
        for day in (first + timedelta(days=n) for n in range(FULL_DAYS)):
            for hour, mins in SLOTS:
                slot = datetime(day.year, day.month, day.day, hour, mins, tzinfo=IST)
                if slot > after and slot > now + LEAD and slot not in taken:
                    return slot
        return None
    per_day = Counter(when.date() for when in taken)
    for have, days in ((0, COVER_DAYS), (1, FULL_DAYS), (2, FULL_DAYS)):
        for day in _ahead(now, days):
            if per_day[day] <= have and (slot := _open(day, EVENING_FIRST, taken, now)):
                return slot
    return None


def fill_queue(now: datetime, done: dict, planned: list[datetime], dry: bool = False,
               chain: dict | None = None) -> None:
    """Room the Reels that reach the bar leave in the queue goes to the ones the bar alone holds (every score at
    least reels.PASS_SCORE), best first: the owner wants the queue kept full, and a slot without a Reel costs the page
    more than a Reel under the recent ones' mean. Their records say so, and they don't count toward the bar. After
    them come the Reels rescued without the pictures the judge faulted (reel_rescue), best first. Reels of the quiz
    chain keep its order (`chain`: work's booked, gone and unsent)."""
    from . import instagram, publish, reel_rescue, reels

    def rank(folder: Path) -> tuple[int, float] | None:
        if (folder / instagram.RECORD).exists() or folder.name in done["scheduled"] or \
                folder.name in chain["unsent"] or not (folder / f"{folder.name}.mp4").exists():
            return None
        rescued = bool(reels.rescued(folder))
        if reel_rescue.unshippable(folder) if rescued else reels.unshippable(folder, with_bar=False):
            return None
        return int(rescued), -reels._read(folder / reels.REVIEW)["total"]

    chain = chain or {"booked": {}, "gone": set(), "unsent": set()}
    held = sorted((key, folder) for folder in sorted(p.parent for p in reels.REELS.glob("ak*/short.yaml"))
                  if (key := rank(folder)))
    for (rescued, minus_total), folder in held:
        total = -minus_total
        if len({d for d in _dues() if d > now} | set(planned)) >= QUEUE:
            return
        after, why = _after(folder, chain["booked"], chain["gone"])
        if why:
            continue
        if not (slots := free_slots(now, 1, planned, after)):
            if after is None:
                return
            continue
        slot, bar = slots[0], reels.bar(folder.name)
        note = {"below_bar": bar} | ({"rescue": reel_rescue.note(folder)} if rescued else {})
        if not dry:
            try:
                instagram.schedule(folder / "short.yaml", slot, note)
            except publish.BufferBusy as err:
                done["held"][folder.name] = f"would fill the queue; Buffer is busy ({err})"
                return
            except Exception as err:
                log.exception("scheduling %s to fill the queue failed", folder.name)
                done["errors"].append(f"scheduling {folder.name} to fill the queue failed: {err}"[:300])
                continue
        planned.append(slot)
        chain["booked"][folder.name] = slot
        done["held"].pop(folder.name, None)
        done["scheduled"][folder.name] = (f"{'would go out ' if dry else ''}{slot:%a %d %b %H:%M} IST, below the bar "
                                          f"({total} < {bar}){f', {reel_rescue.told(folder)},' if rescued else ''}"
                                          " to keep the queue full")


def idle_days(now: datetime, planned: list[datetime], youtube_planned: list[datetime],
              reposts: bool = False) -> list[str]:
    """The next two days (from today while it still has a slot), on each channel, when nothing is due there. With
    `reposts` (reel_repost.ready), Instagram's days are covered: an older Reel goes out again a few hours ahead."""
    idle = []
    channels = [("YouTube", _dues(youtube=True) + youtube_planned)]
    if not reposts:
        channels.insert(0, ("Instagram", _dues() + planned))
    for name, dues in channels:
        days = {_minute(when).date() for when in dues}
        idle += [f"nothing on {name} {day:%a %d %b}" for day in _ahead(now, 2) if day not in days]
    return idle


def channel_upkeep(now: datetime, done: dict) -> None:
    """Once a day, with the brand YouTube channel's sign-in (IG_YOUTUBE_TOKEN): save its numbers, check each Short due
    on it in the last day went out, and file its Shorts into their scriptures' playlists. A day whose numbers fail is
    saved as its error, so a broken sign-in reaches the owner once a day rather than every run."""
    from . import instagram, publish, reels, youtube

    out = reels.REELS / STATS
    day = now.date().isoformat()
    if (out / f"{day}.json").exists():
        return
    youtube.BRAND_TOKEN_FILE.parent.mkdir(exist_ok=True)
    youtube.BRAND_TOKEN_FILE.write_text(os.environ["IG_YOUTUBE_TOKEN"], encoding="utf-8")
    try:
        saved = json.loads(youtube.save_stats(out, token=youtube.BRAND_TOKEN_FILE).read_text(encoding="utf-8"))
    except Exception as err:
        log.exception("reading the YouTube channel's numbers failed")
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{day}.json").write_text(json.dumps({"date": day, "error": str(err)[:300]}), encoding="utf-8")
        done["errors"].append(f"reading the YouTube channel's numbers failed: {err}"[:300])
        return
    done["youtube"] = f"{saved['channel']['subscribers']} subscribers, {saved['channel']['views']} views"
    shorts = {video["title"]: video["id"] for video in saved["videos"]}
    for folder in sorted(reels.REELS.glob("ak*")):
        due = _due(folder, youtube=True)
        if due and now - timedelta(days=1) < due < now - LEAD and \
                publish.youtube_title(instagram.youtube_spec(folder)) not in shorts:
            done["errors"].append(f"{folder.name} was due on YouTube {due.astimezone(IST):%a %H:%M} IST, "
                                  "but the channel doesn't have it")
    try:
        if changes := youtube.file_reels(reels.REELS, shorts, youtube.BRAND_TOKEN_FILE):
            done["playlists"] = changes
    except Exception as err:
        log.exception("filing the Shorts into playlists failed")
        done["errors"].append(f"filing the YouTube Shorts into playlists failed: {err}"[:300])


def health_due(now: datetime) -> bool:
    """Whether this run checks Buffer's side: a post was due in the last HEALTH_SINCE, or it is a HEALTH_HOURS hour."""
    from . import facebook, instagram, reels

    if now.hour % HEALTH_HOURS == 0:
        return True
    for name in (instagram.RECORD, facebook.RECORD, instagram.YOUTUBE_RECORD):
        for path in reels.REELS.glob(f"ak*/{name}"):
            try:
                stamp = json.loads(path.read_text(encoding="utf-8"))["dueAt"]
                due = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
            if now - HEALTH_SINCE <= due <= now:
                return True
    return False


def buffer_health(now: datetime, done: dict, dry: bool = False) -> None:
    """Buffer's side, in one request: the brand's three channels connected with their queues running, and each post
    due in the last RESEND_WITHIN sent. A post Buffer failed goes out again once, RESEND_AFTER from now with the same
    video; one that fails again, and a channel only the owner can reconnect, are errors the owner hears of."""
    from . import facebook, instagram, publish, reels
    from .spec import ShortSpec

    names = {"IG_BUFFER_CHANNEL_ID": "Instagram", "IG_BUFFER_FACEBOOK_CHANNEL_ID": "Facebook",
             "IG_BUFFER_YOUTUBE_CHANNEL_ID": "YouTube"}
    ours = {os.environ[name]: label for name, label in names.items() if os.environ.get(name)}
    query = """
    query Health($channels: ChannelsInput!, $posts: PostsInput!) {
      channels(input: $channels) { id isDisconnected isLocked isQueuePaused }
      posts(input: $posts, first: 50) {
        edges { node { id status dueAt channelService assets { source } error { message } } }
      }
    }"""
    organization = instagram._organization()
    answer = instagram._buffer(query, {"channels": {"organizationId": organization}, "posts": {
        "organizationId": organization,
        "filter": {"channelIds": list(ours), "startDate": (now - RESEND_WITHIN).isoformat(), "endDate": now.isoformat()}}})
    found = {channel["id"]: channel for channel in answer["channels"]}
    for channel_id, label in ours.items():
        channel = found.get(channel_id)
        flags = [name for flag, name in (("isDisconnected", "disconnected"), ("isLocked", "locked"),
                                         ("isQueuePaused", "paused")) if channel and channel[flag]]
        if not channel or flags:
            done["errors"].append(f"Buffer's {label} channel is {' and '.join(flags) or 'gone'}, so nothing goes out "
                                  "there until the owner reconnects it in Buffer")
    services = {"instagram": ("Instagram", instagram.RECORD), "facebook": ("Facebook", facebook.RECORD),
                "youtube": ("YouTube", instagram.YOUTUBE_RECORD)}
    for post in (edge["node"] for edge in answer["posts"]["edges"]):
        if post["status"] != "error" or post["channelService"] not in services:
            continue
        label, record_name = services[post["channelService"]]
        source = next((asset["source"] for asset in post["assets"] or [] if asset.get("source")), "")
        hosted = instagram.HOSTED_REEL.search(source)
        reel = hosted.group(1) if hosted else post["id"]
        why = ((post.get("error") or {}).get("message") or "no reason given")[:160]
        failed = f"Buffer couldn't post {reel} on {label} at {_ist(post['dueAt'])} ({why})"
        path = reels.REELS / reel / record_name
        record = json.loads(path.read_text(encoding="utf-8")) if hosted and path.exists() else {}
        if record.get("id") != post["id"] or record.get("resent"):
            done["errors"].append(failed[:300])
            continue
        if dry:
            done["resent"][f"{reel} ({label})"] = "would send it again"
            continue
        when, folder = (now + RESEND_AFTER).isoformat(), path.parent
        try:
            if post["channelService"] == "youtube":
                spec = instagram.youtube_spec(folder)
                payload = instagram._short(spec, record["media_url"], when)
            else:
                spec = ShortSpec.load(folder / "short.yaml")
                payload = (instagram._post(folder, spec, record["media_url"], when) if label == "Instagram"
                           else facebook._post(spec, record["media_url"], when))
            instagram._still_hosted(spec, record["media_url"])
            result = instagram._mutate("editPost", {"id": post["id"], **payload})
            if result["__typename"] != "PostActionSuccess":
                raise RuntimeError(result.get("message"))
        except publish.BufferBusy:
            raise
        except Exception as err:
            done["errors"].append(f"{failed}, and sending it again failed: {err}"[:300])
            continue
        record |= {"dueAt": result["post"]["dueAt"], "resent": {"at": now.isoformat(timespec="seconds"), "why": why}}
        path.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        done["resent"][f"{reel} ({label})"] = f"{_ist(record['dueAt'])}, after: {why}"


def unhost_sent(now: datetime, done: dict, dry: bool = False) -> None:
    """Take a Reel's videos off Cloudinary once Instagram, Facebook and YouTube all had their posts KEEP ago: Buffer
    fetches a video only at its slot, and the free plan counts what stays hosted against the same 25 credits a month
    as what Buffer downloads. A Reel short of one of the three keeps its videos."""
    from . import facebook, instagram, publish, reels

    for folder in sorted(path.parent for path in reels.REELS.glob(f"ak*/{instagram.RECORD}")):
        paths = [folder / name for name in (instagram.RECORD, facebook.RECORD, instagram.YOUTUBE_RECORD)]
        if not all(path.exists() for path in paths):
            continue
        records = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
        if records[0].get("unhosted_at") or any(r.get("withdrawn") or not r.get("dueAt") for r in records):
            continue
        if max(datetime.fromisoformat(r["dueAt"].replace("Z", "+00:00")) for r in records) > now - KEEP:
            continue
        hosted = sorted({r["media_public_id"] for r in records if r.get("media_public_id")})
        if dry:
            done["unhosted"][folder.name] = f"would take off {', '.join(hosted)}"
            continue
        try:
            for public_id in hosted:
                publish.unhost_video(public_id)
        except Exception as err:
            log.warning("taking %s's videos off Cloudinary failed: %s", folder.name, err)
            continue
        records[0]["unhosted_at"] = now.isoformat(timespec="seconds")
        paths[0].write_text(json.dumps(records[0], indent=1) + "\n", encoding="utf-8")
        done["unhosted"][folder.name] = ", ".join(hosted)


def work(dry: bool = False) -> dict:
    """Review each rendered Reel still missing judgments and schedule the ones that pass, in id order."""
    from . import instagram, llm, publish, reel_repost, reel_rescue, reels

    now = datetime.now(timezone.utc)
    done: dict = {"reviewed": {}, "scheduled": {}, "mirrored": {}, "facebook": {}, "moved": {}, "held": {},
                  "resent": {}, "unhosted": {}, "rescued": {}, "reposted": {}, "errors": []}
    judging, scheduling, planned = True, True, []
    try:
        buffered = instagram.posts_by_reel(instagram._env("IG_BUFFER_CHANNEL_ID"))
    except Exception as err:
        buffered, scheduling = {}, False
        done["errors"].append(f"couldn't read Buffer's Instagram posts, so none were scheduled: {err}"[:300])
    if scheduling and health_due(now):
        try:
            buffer_health(now, done, dry)
        except publish.BufferBusy as err:
            scheduling = False
            done["held"]["Buffer's check"] = f"Buffer is busy ({err})"
        except Exception as err:
            log.exception("checking Buffer's channels and failed posts failed")
            done["errors"].append(f"checking Buffer's channels and failed posts failed: {err}"[:300])
    # The quiz chain: each Reel goes out after the one whose question it answers (booked: this run's slots), even
    # below the bar; a Reel whose question no Reel will answer stays (unsent), and one that can't go out is gone.
    needed = {before for path in reels.REELS.glob("ak*/short.yaml") if (before := _answers(path.parent))}
    chain: dict = {"booked": {}, "gone": set(), "unsent": unanswered()}
    for spec_path in sorted(reels.REELS.glob("ak*/short.yaml")):
        folder = spec_path.parent
        reel = folder.name
        if (folder / instagram.RECORD).exists():
            continue
        if post := buffered.get(reel):
            if not dry:
                instagram.adopt(folder, post)
            done["scheduled"][reel] = f"{_ist(post['dueAt'])} (already in Buffer)"
            continue
        if not (folder / f"{reel}.mp4").exists():
            done["held"][reel] = "its render isn't in the reels volume"
            continue
        # A rescued Reel isn't judged again: its rescue only took out pictures the judge found fault with.
        rescue = reels.rescued(folder)
        if not rescue and reels.judgments(folder) < reels.RUNS:
            if dry or not judging:
                done["held"][reel] = "would be reviewed" if dry else "waiting for the judge's quota"
                continue
            try:
                done["reviewed"][reel] = reels.review(folder)["total"]
            except reels.Unfit as err:
                chain["gone"].add(reel)
                done["held"][reel] = f"not sent to the judge: {err}"
                continue
            except (llm.OutOfQuota, llm.Overloaded) as err:
                judging = False
                done["held"][reel] = f"{reels.judgments(folder)} of {reels.RUNS} judgments; waiting for the judge ({err})"
                if isinstance(err, llm.OutOfCredits):
                    done["errors"].append("the Gemini project's prepaid credits ran out, so no Reel can be judged: "
                                          "add credits in AI Studio > Billing")
                continue
            except Exception as err:
                log.exception("reviewing %s failed", reel)
                done["errors"].append(f"reviewing {reel} failed: {err}"[:300])
                continue
        note = None
        if rescue:
            if why := reel_rescue.unshippable(folder):
                done["held"][reel] = why
                continue
            if reel not in needed:
                done["held"][reel] = "rescued; it fills the queue after the Reels whose every score passed"
                continue
            note = {"below_bar": reels.bar(reel), "rescue": reel_rescue.note(folder)}
        elif why := reels.unshippable(folder):
            # A Reel whose question a later one answers goes out with every score passing, under the bar or not.
            if reel not in needed or reels.unshippable(folder, with_bar=False):
                done["held"][reel] = why
                continue
            note = {"below_bar": reels.bar(reel)}
        if reel in chain["unsent"]:
            done["held"][reel] = "a later Reel answers an earlier question than its own, which would go unanswered"
            continue
        after, why = _after(folder, chain["booked"], chain["gone"])
        if why:
            done["held"][reel] = f"passed; {why}"
            continue
        if not scheduling or len({d for d in _dues() if d > now} | set(planned)) >= QUEUE:
            done["held"][reel] = "passed; waiting for room in the queue"
            continue
        if not (slots := free_slots(now, 1, planned, after)):
            done["held"][reel] = f"passed; the next {COVER_DAYS} days have their Reels"
            continue
        slot = slots[0]
        planned.append(slot)
        chain["booked"][reel] = slot
        below = ((f", {reel_rescue.told(folder)}," if "rescue" in note else f", below the bar ({note['below_bar']})")
                 + " since a later Reel answers its question") if note else ""
        if dry:
            done["scheduled"][reel] = f"would go out {slot:%a %d %b %H:%M} IST{below}"
            continue
        try:
            instagram.schedule(spec_path, slot, note)
            done["scheduled"][reel] = f"{slot:%a %d %b %H:%M} IST{below}"
        except publish.BufferBusy as err:
            scheduling = False
            chain["booked"].pop(reel)
            done["held"][reel] = f"passed; Buffer is busy ({err})"
        except Exception as err:
            log.exception("scheduling %s failed", reel)
            chain["booked"].pop(reel)
            done["errors"].append(f"scheduling {reel} failed: {err}"[:300])
    for folder in sorted(p.parent for p in reels.REELS.glob("ak*/short.yaml")):
        try:
            if dry and not reel_rescue.why_not(folder) and not reel_rescue.faulted(folder):
                done["rescued"][folder.name] = "would be rescued as it is"
            elif not dry and reel_rescue.mark(folder):
                done["rescued"][folder.name] = "as it is: the judge found no fault with its pictures, only repeats"
        except Exception as err:
            log.exception("rescuing %s failed", folder.name)
            done["errors"].append(f"rescuing {folder.name} failed: {err}"[:300])
    if scheduling:
        fill_queue(now, done, planned, dry, chain)
        try:
            reel_repost.fill(now, done, planned, dry)
        except publish.BufferBusy as err:
            done["held"]["older Reels"] = f"Buffer is busy ({err})"
        except Exception as err:
            log.exception("sending an older Reel out again failed")
            done["errors"].append(f"sending an older Reel out again failed: {err}"[:300])
    youtube_planned: list[datetime] = []
    if os.environ.get("IG_BUFFER_YOUTUBE_CHANNEL_ID") and scheduling:
        youtube_planned = mirror(now, done, dry)
        cuts(now, done, dry)
        lineup(now, done, youtube_planned, dry)
        spread(now, done, youtube_planned, dry)
    if os.environ.get("IG_YOUTUBE_TOKEN") and not dry:
        channel_upkeep(now, done)
    if not os.environ.get("IG_BUFFER_FACEBOOK_CHANNEL_ID"):
        log.info("IG_BUFFER_FACEBOOK_CHANNEL_ID is unset; skipping the Facebook mirror")
    elif scheduling:
        for name, step in (("catch-up", facebook_catch_up), ("mirror", facebook_mirror)):
            try:
                step(now, done, dry)
            except publish.BufferBusy as err:
                done["held"][f"Facebook {name}"] = f"Buffer is busy ({err})"
            except Exception as err:
                log.exception("the Facebook %s failed", name)
                done["errors"].append(f"the Facebook {name} failed: {err}"[:300])
    if os.environ.get("IG_YOUTUBE_TOKEN") and scheduling and not dry:
        try:
            from . import shoutouts, youtube

            youtube.BRAND_TOKEN_FILE.parent.mkdir(exist_ok=True)
            youtube.BRAND_TOKEN_FILE.write_text(os.environ["IG_YOUTUBE_TOKEN"], encoding="utf-8")
            shoutouts.youtube_round(now, done, youtube.BRAND_TOKEN_FILE)
        except Exception as err:
            log.exception("the YouTube shoutouts failed")
            done["errors"].append(f"the YouTube shoutouts failed: {err}"[:300])
    try:
        unhost_sent(now, done, dry)
        reel_repost.unhost(now, done, dry)
    except Exception:
        log.exception("taking sent Reels' videos off Cloudinary failed")
    try:
        done["reposts_ready"] = reel_repost.ready(now)
    except Exception as err:
        log.exception("checking the older Reels that can go out again failed")
        done["reposts_ready"] = False
        done["errors"].append(f"checking the older Reels that can go out again failed: {err}"[:300])
    done["idle"] = idle_days(now, planned, youtube_planned, done["reposts_ready"])
    upcoming = sorted({d for d in _dues() if d > now} | set(planned))
    done["queue"] = len(upcoming)
    done["last_due"] = f"{upcoming[-1].astimezone(IST):%a %d %b %H:%M} IST" if upcoming else ""
    last = reel_repost.last_booked()
    done["last_booked"] = last.isoformat(timespec="seconds") if last else ""
    return done


# --- the run, on Modal -------------------------------------------------------------------------------------------

def _alert(message: str) -> None:
    """Push to the owner's phone through the studio's ntfy topic, under the brand's name."""
    if topic := os.environ.get("YTC_NTFY_TOPIC"):
        request = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"), method="POST",
                                         headers={"Title": "Ankahi Kathayein needs attention", "Priority": "high",
                                                  "Tags": "warning"})
        try:
            urllib.request.urlopen(request, timeout=30).read()
        except Exception as err:
            log.warning("ntfy push failed: %s", err)


def _clone() -> None:
    ssh = Path.home() / ".ssh"
    ssh.mkdir(mode=0o700, exist_ok=True)
    (ssh / "id_ed25519").write_text(os.environ["YTC_DEPLOY_KEY"].strip() + "\n", encoding="utf-8")
    (ssh / "id_ed25519").chmod(0o600)
    (ssh / "known_hosts").write_text(cloud.GITHUB_HOST_KEY + "\n", encoding="utf-8")
    shutil.rmtree(cloud.CHECKOUT, ignore_errors=True)
    done = subprocess.run(["git", "clone", "-q", "--depth", "20", cloud.REPO_URL, str(cloud.CHECKOUT)],
                          capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"couldn't clone the repo: {done.stderr.strip()[:400]}")
    cloud._git("config", "user.name", "Ankahi Kathayein reels")
    cloud._git("config", "user.email", "reels@days-of-odd.invalid")
    # A run that couldn't save would schedule the same Reels again on the next run's clone. A push by another run
    # since the clone leaves it behind, which git refuses too (the reels and factory runs start in the same minute).
    for _ in range(3):
        if not (done := cloud._git("push", "--dry-run", "-q", "origin", "HEAD")).returncode:
            return
        cloud._git("pull", "-q", "--ff-only")
    raise RuntimeError(f"the deploy key can't push to the repo: {done.stderr.strip()[:400]}")


def _copy_render(stored: Path, folder: Path, reel: str, label: str) -> bool:
    """Copy a render from the volume into the clone when git has its folder as rendered."""
    if not (stored / RENDER).exists() or not (folder / RENDER).exists() or \
            (stored / RENDER).read_bytes() != (folder / RENDER).read_bytes():
        log.info("%s: the folder in git isn't the render in the volume yet", label)
        return False
    names = [name.format(id=reel) for name in MEDIA]
    if missing := [name for name in names if not (stored / name).exists()]:
        log.info("%s: the volume has no %s", label, ", ".join(missing))
        return False
    for name in names:
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(stored / name, folder / name)
    return True


def _cut_waits(stored: Path, folder: Path) -> bool:
    """Whether the volume has a Reel's YouTube cut that its Short isn't yet."""
    from . import instagram

    record = instagram.youtube_record(folder) or {}
    return (stored / instagram.CUT).is_dir() and not (record.get("cut") and not record.get("withdrawn"))


def _bring_media() -> list[str]:
    """Copy the render of each Reel still to be scheduled, and each YouTube cut still to go out, from the volume into
    the clone, when git has its folder."""
    from . import instagram

    brought = []
    for stored in sorted(p for p in MEDIA_ROOT.iterdir() if p.is_dir()):
        folder = cloud.CHECKOUT / "content" / "instagram" / stored.name
        if not (folder / instagram.RECORD).exists() and _copy_render(stored, folder, stored.name, stored.name):
            brought.append(stored.name)
        if _cut_waits(stored, folder) and _copy_render(stored / instagram.CUT, folder / instagram.CUT, stored.name,
                                                       f"{stored.name}'s cut"):
            brought.append(f"{stored.name} ({instagram.CUT})")
    return brought


def _prune(now: datetime) -> list[str]:
    """Drop the renders of Reels whose slot passed KEEP ago, unless their YouTube cut is still to go out."""
    gone = []
    for stored in sorted(p for p in MEDIA_ROOT.iterdir() if p.is_dir()):
        folder = cloud.CHECKOUT / "content" / "instagram" / stored.name
        due = _due(folder)
        if due and due < now - KEEP and not _cut_waits(stored, folder):
            shutil.rmtree(stored)
            gone.append(stored.name)
    if gone:
        media.commit()
    return gone


def _last_json(stdout: str) -> dict:
    for line in reversed(stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {}


def _message(started: datetime, done: dict, host: str = "Modal") -> str:
    parts = [f"{reel} reviewed ({total})" for reel, total in done.get("reviewed", {}).items()]
    parts += [f"{reel} scheduled for {when}" for reel, when in done.get("scheduled", {}).items()]
    parts += [f"{reel} on YouTube for {when}" for reel, when in done.get("mirrored", {}).items()]
    parts += [f"{reel} on Facebook for {when}" for reel, when in done.get("facebook", {}).items()]
    parts += [f"{reel} on YouTube {how}" for reel, how in done.get("moved", {}).items()]
    parts += [f"{reel} out again {when}" for reel, when in done.get("reposted", {}).items()]
    return f"reels: {host} run {started:%Y-%m-%d %H:%M} UTC" + (f": {'; '.join(parts)}" if parts else "")


def _tell_owner(code: int, done: dict, memory=None, host: str = "Modal",
                log_at: str = "modal.com > ankahi-kathayein > reel_run") -> None:
    """Alert the owner's phone; `memory` (the state Dict by default) keeps when each kind of alert last went out."""
    memory = state if memory is None else memory
    problems = list(done.get("errors", []))
    if code:
        problems.append(f"it ran over {RUN_MINUTES} minutes and was stopped" if code == 124 else f"it failed (exit {code})")
    # Runs come every hour; a problem that lasts reaches the phone every PROBLEM_HOURS.
    if problems and datetime.now(timezone.utc).timestamp() - memory.get("problem-at", 0) > PROBLEM_HOURS * 3600:
        _alert(f"The Reels' run on {host}: {'; '.join(problems)}"[:850] + f". Log: {log_at}.")
        memory["problem-at"] = datetime.now(timezone.utc).timestamp()
    local = datetime.now(IST)
    today = local.date().isoformat()
    held = "; ".join(f"{reel}: {why}" for reel, why in done.get("held", {}).items()) or "nothing is rendered"
    last = done.get("last_booked")
    quiet = STALLED_COVERED if done.get("reposts_ready") else STALLED_AFTER
    if last and datetime.now(timezone.utc) - datetime.fromisoformat(last) <= quiet:
        return
    since = f" Nothing new booked since {datetime.fromisoformat(last).astimezone(IST):%a %d %b %H:%M} IST." if last else ""
    if done.get("reposts_ready"):
        since += " Older Reels go out again on Instagram and Facebook meanwhile."
    if "queue" in done and done["queue"] < LOW and memory.get("low-queue") != today:
        _alert(f"Only {done['queue']} Reel(s) left to go out"
               + (f", the last on {done['last_due']}" if done["last_due"] else "") + f".{since} Waiting: {held}"[:700])
        memory["low-queue"] = today
    if done.get("idle") and local.hour >= ALERT_FROM and memory.get("idle-day") != today:
        _alert(f"A day without a post is coming: {'; '.join(done['idle'])}.{since} Waiting: {held}"[:900])
        memory["idle-day"] = today


@app.function(image=cloud.run_image, cpu=1.0, memory=2048, timeout=(RUN_MINUTES + 15) * 60,
              schedule=modal.Cron(SCHEDULE), secrets=[secret], volumes={str(MEDIA_ROOT): media}, max_containers=1)
def reel_run(dry: bool = False) -> dict:
    """One run on a fresh clone of the repo; what it changed is pushed back."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    started = datetime.now(timezone.utc)
    try:
        _clone()
        brought = _bring_media()
    except Exception as err:
        _alert(f"The Reels' run on Modal couldn't start: {err}"[:900])
        raise
    env = llm.use_brand_project({**os.environ, "PYTHONPATH": str(cloud.CHECKOUT / "pipeline" / "src"),
                                 "PYTHONUNBUFFERED": "1"})
    command = [sys.executable, "-c", "from ytc import reel_cloud; reel_cloud.main()", "work", *(["--dry"] if dry else [])]
    try:
        run = subprocess.run(command, cwd=cloud.CHECKOUT / "pipeline", env=env, stdout=subprocess.PIPE, text=True,
                             timeout=RUN_MINUTES * 60)
        code, done = run.returncode, _last_json(run.stdout)
    except subprocess.TimeoutExpired:
        code, done = 124, {}
    done |= {"brought": brought, "exit": code}
    if dry:
        return done
    try:
        done["saved"] = cloud._save(_message(started, done))
    except Exception as err:
        _alert(f"The Reels' run on Modal couldn't save what it did: {err}"[:900])
        raise
    done["pruned"] = _prune(started)
    _tell_owner(code, done)
    return done


# --- the Reel factory, on Modal ----------------------------------------------------------------------------------

# Half-made Reels (from a run whose step failed), each step's log, and the models the steps download.
factory_volume = modal.Volume.from_name("ankahi-kathayein-factory", create_if_missing=True)
FACTORY_ROOT = Path("/factory")
# Only a deploy with AK_FACTORY_CRON set (e.g. "40 * * * *": 10 past every hour in IST, between reel_run's runs)
# schedules the factory, so it runs on the brand's own Modal workspace and never on the Days of Odd one, whose free
# credit the studio needs.
FACTORY_CRON = os.environ.get("AK_FACTORY_CRON", "")
FACTORY_MINUTES = 225


def _factory_state() -> dict:
    saved = state.get("factory") or {}
    for key in ("reels", "made", "alerts"):
        saved.setdefault(key, {})
    return saved


def _bring_wip(saved: dict) -> list[str]:
    """Put each half-made Reel the volume keeps back into the clone."""
    brought = []
    for reel, mine in saved["reels"].items():
        wip = FACTORY_ROOT / "wip" / reel
        if not mine.get("pushed") and not mine.get("set_aside") and wip.is_dir():
            shutil.copytree(wip, cloud.CHECKOUT / "content" / "instagram" / reel, dirs_exist_ok=True)
            brought.append(reel)
    return brought


def _keep_wip(reel: str) -> None:
    folder = cloud.CHECKOUT / "content" / "instagram" / reel
    if folder.is_dir():
        wip = FACTORY_ROOT / "wip" / reel
        shutil.rmtree(wip, ignore_errors=True)
        shutil.copytree(folder, wip)


def _push(paths: list[str], message: str) -> str:
    """Commit these paths alone and push, rebasing onto whatever reel_run or a person pushed meanwhile."""
    for path in paths:
        cloud._git("add", "-A", "--", path)
    staged = [p for p in cloud._git("diff", "--cached", "--name-only").stdout.splitlines() if p]
    if not staged:
        return "nothing changed"
    if big := [p for p in staged if (cloud.CHECKOUT / p).is_file() and (cloud.CHECKOUT / p).stat().st_size > 5_000_000]:
        cloud._git("reset", "-q")
        raise RuntimeError(f"{', '.join(big)} is over 5 MB")
    cloud._git("commit", "-q", "-m", message)
    error = ""
    for attempt in range(5):
        if cloud._git("pull", "-q", "--rebase", "--autostash", "-X", "theirs").returncode:
            cloud._git("rebase", "--abort")
            error = "the rebase onto the repo's latest commit failed"
        elif (pushed := cloud._git("push", "-q")).returncode:
            error = pushed.stderr.strip()[:400]
        else:
            return "pushed"
        time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"couldn't push: {error}")


def _ship(reel: str, story: str) -> str:
    """Put a made Reel's render in the reels volume, then push its folder and the backlog row it moved."""
    folder = cloud.CHECKOUT / "content" / "instagram" / reel
    names = [name.format(id=reel) for name in (*MEDIA, RENDER)]
    if missing := [name for name in names if not (folder / name).exists()]:
        raise RuntimeError(f"{reel} has no {', '.join(missing)}")
    for name in names:
        (MEDIA_ROOT / reel / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(folder / name, MEDIA_ROOT / reel / name)
    media.commit()
    return _push([f"content/instagram/{reel}", "content/instagram/backlog.md"],
                 f"Reel factory: {reel}, {story}: written, pictured, rendered and checked by QA on Modal; the next "
                 "reel run judges it")


def _factory_alerts(saved: dict, done: dict) -> None:
    """Each kind of alert reaches the phone once a day."""
    today = f"{datetime.now(IST):%Y-%m-%d}"
    for kind, message in done.get("alerts", []) + [("error", e) for e in done.get("errors", [])]:
        if saved["alerts"].get(kind) != today:
            _alert(f"Reel factory: {message}"[:900])
            saved["alerts"][kind] = today


@app.function(image=cloud.run_image, cpu=(1.0, 8.0), memory=(2048, 8192), timeout=(FACTORY_MINUTES + 15) * 60,
              schedule=modal.Cron(FACTORY_CRON) if FACTORY_CRON else None, secrets=[secret],
              volumes={str(MEDIA_ROOT): media, str(FACTORY_ROOT): factory_volume}, max_containers=1)
def factory_run(force: bool = False) -> dict:
    """One factory run on a fresh clone: makes at most one Reel (reel_factory.work), then ships it."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    try:
        _clone()
    except Exception as err:
        _alert(f"The Reel factory on Modal couldn't start: {err}"[:900])
        raise
    saved = _factory_state()
    resumed = _bring_wip(saved)
    state_file = Path("/tmp/factory-state.json")
    state_file.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    # The pictures are ranked in this container: the studio's ranker (cloud.rank_pictures) is a Days of Odd function,
    # which the brand's workspace doesn't have.
    env = llm.use_brand_project({**os.environ, "PYTHONPATH": str(cloud.CHECKOUT / "pipeline" / "src"),
                                 "PYTHONUNBUFFERED": "1", "HF_HOME": str(FACTORY_ROOT / "hf"), "YTC_RANK_HERE": "1"})
    command = [sys.executable, "-c", "from ytc import reel_factory; reel_factory.main()", "work", "--state",
               str(state_file), "--logs", str(FACTORY_ROOT / "logs"), "--media", str(MEDIA_ROOT),
               *(["--force"] if force else [])]
    try:
        run = subprocess.run(command, cwd=cloud.CHECKOUT / "pipeline", env=env, stdout=subprocess.PIPE, text=True,
                             timeout=FACTORY_MINUTES * 60)
        code, done = run.returncode, _last_json(run.stdout)
    except subprocess.TimeoutExpired:
        code, done = 124, {}
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    done |= {"resumed": resumed, "exit": code, "errors": []}
    if code:
        done["errors"].append(f"the factory's step ran over {FACTORY_MINUTES} minutes" if code == 124
                              else f"the factory's step failed (exit {code})")
    reel = done.get("reel")
    if reel and done.get("result") == "made":
        try:
            done["shipped"] = _ship(reel, done.get("story", ""))
            saved["reels"][reel]["pushed"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            today = f"{datetime.now(IST):%Y-%m-%d}"
            saved["made"][today] = saved["made"].get(today, 0) + 1
            shutil.rmtree(FACTORY_ROOT / "wip" / reel, ignore_errors=True)
        except Exception as err:
            log.exception("shipping %s failed", reel)
            done["errors"].append(f"{reel} was made but not shipped: {err}"[:300])
            _keep_wip(reel)
    elif reel:
        _keep_wip(reel)
        if done.get("result") == "set aside":
            # The story's row moved on this clone: pushed, or every run would start the same story again.
            try:
                done["shipped"] = _push(["content/instagram/backlog.md"], f"Reel factory: {reel} set aside "
                                        f"({saved['reels'].get(reel, {}).get('set_aside', 'the writer failed')})"[:200])
            except Exception as err:
                done["errors"].append(f"couldn't push the backlog after setting {reel} aside: {err}"[:300])
    factory_volume.commit()
    _factory_alerts(saved, done)
    state["factory"] = saved
    return done


# --- from the Mac ------------------------------------------------------------------------------------------------

def upload(folder: Path) -> list[str]:
    """Put a rendered Reel's (or YouTube cut's) video and narration in the reels volume, where the runs take them
    from."""
    from . import instagram

    folder = folder.resolve()
    reel = folder.parent.name if folder.name == instagram.CUT else folder.name
    where = f"{reel}/{instagram.CUT}" if folder.name == instagram.CUT else reel
    names = [name.format(id=reel) for name in (*MEDIA, RENDER)]
    if missing := [name for name in names if not (folder / name).exists()]:
        raise SystemExit(f"{where} has no {', '.join(missing)}: render it first")
    with media.batch_upload(force=True) as batch:
        for name in names:
            batch.put_file(folder / name, f"/{where}/{name}")
    return names


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ytc.reel_cloud", description="@ankahi.kathayein's Reels on Modal")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("upload", help="put rendered Reels' videos in the reels volume").add_argument(
        "folders", type=Path, nargs="+")
    commands.add_parser("deploy", help="publish this code as the app, with its schedule")
    for name, text in (("run", "one run on Modal now"), ("work", "review and schedule on this checkout (a run's step)")):
        commands.add_parser(name, help=text).add_argument("--dry", action="store_true", help="change nothing")
    factory = commands.add_parser("factory", help="one Reel factory run on Modal now")
    factory.add_argument("--force", action="store_true", help="make a Reel even when the supply is enough")
    factory.add_argument("--ephemeral", action="store_true", help="in a one-off app, for a workspace without the deploy")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    from .cli import PIPELINE_ROOT, _load_env

    _load_env(PIPELINE_ROOT / ".env")
    if args.command == "upload":
        for folder in args.folders:
            print(f"{folder}: {', '.join(upload(folder))}")
    elif args.command == "deploy":
        with modal.enable_output():
            app.deploy(name=APP_NAME)
    elif args.command == "run":
        result = modal.Function.from_name(APP_NAME, "reel_run").remote(dry=args.dry)
        print(json.dumps(result, indent=1, ensure_ascii=False))
    elif args.command == "factory" and args.ephemeral:
        with modal.enable_output(), app.run(detach=True):
            print(json.dumps(factory_run.remote(force=args.force), indent=1, ensure_ascii=False))
    elif args.command == "factory":
        result = modal.Function.from_name(APP_NAME, "factory_run").remote(force=args.force)
        print(json.dumps(result, indent=1, ensure_ascii=False))
    else:
        print(json.dumps(work(args.dry), ensure_ascii=False))


if __name__ == "__main__":
    main()
