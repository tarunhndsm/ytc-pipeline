"""One unattended run of the cloud studio. GitHub Actions starts it several times a day.

A run syncs Buffer, files live Shorts into playlists, does the daily routine once a day (analytics, learnings,
report, new topics), makes Shorts when fewer than a week's worth are waiting, schedules finished Shorts into
Buffer's free slots, and writes `status/status.json` for the monitor on the owner's Mac.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
import secrets
import shutil
import statistics
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import date, datetime, time as clock, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

from . import llm
# The format new Shorts are made in (strategy/SCRIPT-RULES.md); only Shorts of this format are scheduled.
from .writer import FORMAT

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
EPISODES = ROOT / "content" / "episodes"
REJECTED = ROOT / "content" / "rejected"
STATUS = ROOT / "status"
CALENDAR = ROOT / "strategy" / "CALENDAR.md"
LEARNINGS = ROOT / "strategy" / "LEARNINGS.md"
REPORTS = ROOT / "reports"
ANALYTICS = ROOT / "analytics"
IST = ZoneInfo("Asia/Kolkata")
ET = ZoneInfo("America/New_York")
DAY_ZERO = date(2026, 9, 26)
# A week at 2 a day. Shorts made further ahead would miss what the newest analytics teach, which matters most
# while format 2 (writer.FORMAT) is new.
INVENTORY_TARGET = 14
# A backlog topic is chosen first when its Wikipedia article had at least this many readers in 30 days: a subject
# people already know or search for (format 1's median topic had about 6,000).
MIN_READERS = 15_000
BATCH = 4
# After Gemini's free quotas reset (00:05 Pacific: 12:35 IST in summer time, 13:35 in winter), so the routine's
# learnings and new topics come before production uses the day's quota; the first run after is 17:05 IST.
DAILY_FROM_HOUR = 14
# Modal's Starter plan includes $1 of compute a month, or $30 once a card is on file (set the YTC_MODAL_CREDIT
# Actions variable to match). A Short made there costs about $0.10: a worker waiting on Gemini, and up to three renders.
MODAL_CREDIT = float(os.environ.get("YTC_MODAL_CREDIT") or 1)
MODAL_PER_SHORT = 0.12
# With a card on file the studio's own runs are on Modal too (cloud.studio_run, about $1 a month), and with the
# spend limit at $0 Modal stops everything, those runs included, once the credit is gone.
MODAL_RESERVE = 2.0 if MODAL_CREDIT >= 10 else 0.0
# Set in Modal's containers: this run is cloud.studio_run, which starts workers but makes no Short itself.
ON_MODAL = bool(os.environ.get("YTC_ON_MODAL"))
# GitHub's free plan gives private repositories 2,000 Actions minutes a month. The reserve keeps the runs that
# only sync, collect and publish (about 2 minutes each) going to the end of the month.
MONTH_MINUTES = 2000
MINUTES_RESERVE = 250
# About what one Short made on the runner takes: research, image picks, and reviews waiting on the language model
# (3 to 5 minutes a prompt through Cursor), and three renders of about 3 minutes each (39 and 49 on 2026-09-26).
RUNNER_MINUTES_PER_SHORT = 45
# Shorts made at once on the runner. Most of a Short's time is waiting on the language model, so the lanes overlap
# those waits, while rendering and checking take turns (studio.CPU_LOCK).
LANES = 3
# Once Gemini's Flash quota is spent, production goes on with the backup models (llm.BACKUPS), but only while fewer
# than this many are ready (where posting slows; see per_day) does a backup model or Cursor judge a finished Short;
# otherwise it waits for a Flash review after the reset (_jobs). Cursor's Shorts took about three times the runner's
# minutes and passed the gate 2 times in 17 (0 of 3 when also given the month's spare minutes on 2026-09-27).
CURSOR_BELOW = 9
# Below this many ready (about a day of posting), production goes on without Gemini's Flash models or Cursor, and
# Gemma 4 31B reviews the Shorts they can't (studio.LAST_RESORT): a Short judged by Gemma beats posting nothing.
LAST_RESORT_BELOW = 3
# A post Buffer couldn't publish goes back in the queue this many times; after that it's left for the owner.
RETRIES = 2
# How far ahead of its slot a post is first checked (preflight_posts); every run until it goes out checks it again.
PREFLIGHT_HOURS = 36
# Never start a Short on the runner this far into a run; the workflow stops the step at 170.
PRODUCE_MINUTES = 120
# A Modal worker has a 4-hour timeout, after which Modal reports it failed. One Modal still calls running
# this long after it started (runs are 6 hours apart) is stuck in a queue, and is stopped and retried.
STALE_HOURS = 8
MAX_SPAWNS = 3
# A topic whose Short is rejected for anything but its research (the pictures, the review, a worker that kept
# stopping) is parked and comes back once after this many days; a second rejection drops it.
PARK_DAYS = 14
SERIES = ["Wait, That Happened?", "History's Luckiest People", "History's Unluckiest People",
          "Hoaxes That Fooled Everyone", "Trials You Won't Believe", "Bad Ideas That Seemed Good", "History Got It Wrong"]
MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
STATUS_MARK = re.compile(r"\s+—\s+(making|done|dropped|parked)\s+\((ep\d{3})\)\s*$")
# Wikipedia readership per topic (choose_topics), read again after DEMAND_DAYS.
DEMAND = STATUS / "demand.json"
DEMAND_DAYS = 14


def _now() -> datetime:
    return datetime.now(IST)


def day_number(today: date | None = None) -> int:
    return ((today or _now().date()) - DAY_ZERO).days


def _json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


class Run:
    """What this run did, stage by stage, for status.json and the day's report."""

    def __init__(self, trigger: str):
        self.started = _now()
        self.clock = time.monotonic()
        self.trigger = trigger
        self.stages: list[dict] = []
        self.errors: list[str] = []
        self.owner_action: list[str] = []
        self.made: list[dict] = []
        self.spawned: list[dict] = []
        self.started_here = 0
        self.capacity: dict | None = None
        self.worker_calls: list[dict] = []
        self.quota_until: datetime | None = None
        self.published: list[dict] = []
        self.notes: list[str] = []
        self.renders: dict[str, int] = {"modal": 0, "runner": 0}
        self.not_live: list[dict] = []

    def minutes(self) -> float:
        return (time.monotonic() - self.clock) / 60

    @contextmanager
    def stage(self, name: str):
        started = time.monotonic()
        entry = {"name": name, "ok": True, "detail": ""}
        self.stages.append(entry)
        log.info("== %s", name)
        try:
            yield entry
        except Exception as err:
            from .publish import BufferBusy

            if isinstance(err, BufferBusy):
                entry["detail"] = f"skipped: {err}"
                log.warning("%s skipped: %s", name, err)
                return
            entry["ok"] = False
            entry["error"] = f"{type(err).__name__}: {err}"[:600]
            self.errors.append(f"{name}: {entry['error']}")
            log.error("%s failed: %s\n%s", name, err, traceback.format_exc())
            if isinstance(err, llm.OutOfQuota):
                entry["quota"] = True
                self.quota_until = _next_quota_reset()
            _owner_action_for(self, err)
        finally:
            entry["seconds"] = round(time.monotonic() - started, 1)


def _owner_action_for(run: Run, err: Exception) -> None:
    text = str(err)
    if "invalid_grant" in text or "Not signed in" in text:
        run.owner_action.append("YouTube sign-in expired: on the Mac run `uv run --no-sync ytc auth` in pipeline/, "
                                "then update the YTC_GOOGLE_TOKEN secret (see OWNER-CHECKLIST.md).")
    elif "API key not valid" in text or "API_KEY_INVALID" in text:
        run.owner_action.append("The Gemini API key was rejected: make a new key in AI Studio (days-of-odd project) "
                                "and update the YTC_GEMINI_API_KEY secret.")
    elif "Unauthorized" in text and "buffer" in text.lower():
        run.owner_action.append("Buffer rejected the API key: make a new one and update the BUFFER_API_KEY secret.")


# --- inventory ---------------------------------------------------------------------------------------------


def episodes() -> list[dict]:
    """Every episode folder with what stage it's at."""
    out = []
    for folder in sorted(EPISODES.glob("ep[0-9][0-9][0-9]")):
        record, held, topic = _json(folder / "publish.json"), _json(folder / "hold.json"), _json(folder / "topic.json")
        remote = _json(folder / "remote.json")
        spec = yaml.safe_load((folder / "short.yaml").read_text(encoding="utf-8")) if (folder / "short.yaml").exists() else {}
        if record:
            state = "live" if record["status"] == "sent" else "error" if record["status"] == "error" else "scheduled"
        elif held:
            state = "waiting"
        elif remote:
            state = "remote"
        elif topic:
            state = "making"
        else:
            state = "unfinished"
        out.append({"id": folder.name, "folder": folder, "state": state, "title": spec.get("title"),
                    "series": spec.get("series") or (topic or {}).get("series"), "record": record, "held": held, "topic": topic,
                    "remote": remote, "format": (topic or {}).get("format", 1)})
    return out


def _postable(e: dict) -> bool:
    """A finished Short that may still be scheduled: format 1 ones stay parked (decision 2026-10-07)."""
    return e["state"] == "waiting" and e["format"] >= FORMAT


def inventory(eps: list[dict] | None = None) -> dict:
    eps = eps if eps is not None else episodes()
    scheduled = [e for e in eps if e["state"] == "scheduled"]
    waiting = [e for e in eps if _postable(e)]
    return {"total": len(scheduled) + len(waiting), "in_buffer": len(scheduled), "waiting": len(waiting),
            "parked": sum(1 for e in eps if e["state"] == "waiting" and not _postable(e)),
            "target": INVENTORY_TARGET, "making": [e["id"] for e in eps if e["state"] == "making"],
            "remote": [e["id"] for e in eps if e["state"] == "remote"]}


# --- calendar ----------------------------------------------------------------------------------------------


def _next_date(label: str, today: date) -> date | None:
    match = re.match(r"([A-Z][a-z]{2})\w*\s+(\d{1,2})", label)
    if not match or match.group(1) not in MONTHS:
        return None
    for year in (today.year, today.year + 1):
        try:
            when = date(year, MONTHS[match.group(1)], int(match.group(2)))
        except ValueError:
            return None
        if when >= today:
            return when
    return None


def calendar_topics(today: date | None = None) -> list[dict]:
    today = today or _now().date()
    topics, section, series = [], None, None
    for number, line in enumerate(CALENDAR.read_text(encoding="utf-8").splitlines()):
        if line.startswith("## "):
            section, series = line[3:].strip(), None
        elif line.startswith("### "):
            series = line[4:].strip()
        elif section and section.startswith("Anniversaries") and line.startswith("| ") and not line.startswith(("| Date", "|---")):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 3:
                mark = STATUS_MARK.search(cells[1])
                topics.append({"kind": "anniversary", "line": number, "label": cells[0], "date": _next_date(cells[0], today),
                               "topic": STATUS_MARK.sub("", cells[1]), "series": cells[2], "status": mark.group(1) if mark else None,
                               "episode": mark.group(2) if mark else None})
        elif section == "Backlog" and series and line.startswith("- "):
            mark = STATUS_MARK.search(line)
            topics.append({"kind": "backlog", "line": number, "topic": STATUS_MARK.sub("", line[2:]).strip(),
                           "series": series, "status": mark.group(1) if mark else None, "episode": mark.group(2) if mark else None})
    return topics


def mark_topic(topic: dict, status: str, episode_id: str) -> None:
    lines = CALENDAR.read_text(encoding="utf-8").splitlines()
    line = lines[topic["line"]]
    if topic["kind"] == "anniversary":
        cells = line.strip().strip("|").split("|")
        cells[1] = f" {STATUS_MARK.sub('', cells[1].strip())} — {status} ({episode_id}) "
        lines[topic["line"]] = "|" + "|".join(cells) + "|"
    else:
        lines[topic["line"]] = STATUS_MARK.sub("", line) + f" — {status} ({episode_id})"
    CALENDAR.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _available(topic: dict, today: date) -> bool:
    """Not started yet, or parked for PARK_DAYS since its Short was rejected."""
    if topic["status"] != "parked":
        return not topic["status"]
    rejected_on = str((_json(REJECTED / topic["episode"] / "review.json") or {}).get("rejected_at", ""))[:10]
    return not rejected_on or (today - date.fromisoformat(rejected_on)).days >= PARK_DAYS


def demand(topics: list[dict]) -> dict[str, int]:
    """How many people read the English Wikipedia article that best matches each topic in the last 30 days: how
    widely known its subject is. Cached in status/demand.json for DEMAND_DAYS; 0 when Wikipedia doesn't answer."""
    from urllib.parse import quote

    from .visuals import _user_agent

    cache = _json(DEMAND, {})
    today = _now().date()
    end = today - timedelta(days=1)
    start = end - timedelta(days=29)
    headers = {"User-Agent": _user_agent()}

    def stems(text: str) -> set[str]:
        return {w.rstrip("s")[:5] for w in _key_words(text)}

    def article_for(text: str) -> str:
        # The search hit whose title (or the redirect it matched) shares the most words with the topic, at least a
        # third of its own: full-text search alone matched "Tulip mania wasn't as manic as we think" to a list of
        # people, the first hit a third right matched the Salem witch trials to "Scarlet Witch", and the closest
        # fraction matched Santa Anna to the city of Santa Ana.
        words = stems(text)

        def fit(title: str) -> tuple[int, float]:
            own = stems(title)
            return (len(own & words), len(own & words) / len(own)) if own else (0, 0.0)

        query = re.sub(r"\([^)]*\)", "", text).split(":")[0].strip()
        # Names of two or more words first ("Julius Caesar", "Leaning Tower of Pisa"): the whole sentence matched
        # "Julius Caesar was kidnapped by pirates" to piracy off Somalia, and Santa Anna's leg to a list of deaths.
        runs = (re.sub(r"^(?:The|A|An)\s+|['’]s\b", "", run)
                for run in re.findall(r"[A-Z][\w.'’-]*(?:\s+(?:of\s+|de\s+|the\s+)?[A-Z][\w.'’-]*)+", query))
        names = [run for run in runs if len(run.split()) >= 2]
        for attempt in dict.fromkeys((*names, query, re.split(r",| and | was | were | is ", query)[0])):
            time.sleep(0.5)
            response = requests.get("https://en.wikipedia.org/w/api.php", headers=headers, timeout=15, params={
                "action": "query", "list": "search", "srsearch": attempt, "srlimit": 5, "srprop": "redirecttitle",
                "format": "json"})
            response.raise_for_status()
            found = response.json()["query"]["search"]
            scored = []
            for rank, hit in enumerate(found):
                fits = [f for f in (fit(hit["title"]), fit(hit.get("redirecttitle", ""))) if f[1] >= 1 / 3]
                if fits:
                    scored.append((max(fits), -rank, hit["title"]))
            if scored:
                return max(scored)[2]
        return ""

    for topic in topics:
        text = topic["topic"]
        if (kept := cache.get(text)) and (today - date.fromisoformat(kept["at"])).days < DEMAND_DAYS:
            continue
        try:
            title = article_for(text)
            views = 0
            if title:
                article = quote(title.replace(" ", "_"), safe="")
                response = requests.get(
                    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/"
                    f"{article}/daily/{start:%Y%m%d}/{end:%Y%m%d}", headers=headers, timeout=15)
                # 404 is an article nobody read; any other failure is Wikipedia being busy, so ask again next run.
                if response.status_code != 404:
                    response.raise_for_status()
                    views = sum(item["views"] for item in response.json().get("items", []))
            cache[text] = {"article": title, "views": views, "at": today.isoformat()}
        except (requests.RequestException, ValueError, KeyError) as err:
            log.info("no Wikipedia readership for %r: %s", text, err)
            if getattr(getattr(err, "response", None), "status_code", None) == 429:
                break  # Wikipedia's limit for anonymous readers: the rest are measured next run
    _write_json(DEMAND, cache)
    return {text: entry["views"] for text, entry in cache.items()}


def choose_topics(count: int, eps: list[dict]) -> list[dict]:
    """Anniversaries within a week first, then the backlog's most widely known topics (MIN_READERS), never two of a
    series in a row; the rest of the backlog only when no widely known topic is left."""
    today = _now().date()
    open_topics = [t for t in calendar_topics(today) if _available(t, today)]
    chosen = sorted((t for t in open_topics if t["kind"] == "anniversary" and t["date"] and (t["date"] - today).days <= 7),
                    key=lambda t: t["date"])[:count]
    recent = [e["series"] for e in sorted(eps, key=lambda e: e["id"], reverse=True) if e["series"]]
    backlog = [t for t in open_topics if t["kind"] == "backlog"]
    readers = demand(backlog) if len(chosen) < count and backlog else {}
    while len(chosen) < count and backlog:
        last = ([t["series"] for t in reversed(chosen)] + recent or [None])[0]
        best = max(backlog, key=lambda t: (readers.get(t["topic"], 0) >= MIN_READERS, t["series"] != last,
                                           readers.get(t["topic"], 0)))
        chosen.append(best)
        backlog.remove(best)
    return chosen


TOPICS_SCHEMA = {
    "type": "object",
    "properties": {
        "backlog": {"type": "array", "items": {"type": "object", "properties": {
            "topic": {"type": "string"}, "series": {"type": "string", "enum": SERIES}, "wikipedia": {"type": "string"}},
            "required": ["topic", "series", "wikipedia"]}},
        "anniversaries": {"type": "array", "items": {"type": "object", "properties": {
            "date": {"type": "string"}, "topic": {"type": "string"}, "series": {"type": "string", "enum": SERIES},
            "wikipedia": {"type": "string"}}, "required": ["date", "topic", "series", "wikipedia"]}},
    },
    "required": ["backlog", "anniversaries"],
}


def _key_words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text)} - {"that", "with", "from", "were", "their", "which", "into", "after"}


def _name_words(text: str) -> set[str]:
    """The words of a name or article title, short ones and years included ("Emu War", "War of 1812")."""
    return {w.lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]{2,}", text)} - {"the", "and", "for"}


def _winners(count: int = 5) -> str:
    """The channel's Shorts at least two days old that drew the most and the fewest views, from the latest analytics,
    so new topics aim at subjects with the winners' pull (the growth desk's lever on Days of Odd)."""
    files = sorted(ANALYTICS.glob("*.json"))
    now = _now()
    videos = sorted((v for v in (_json(files[-1], {}) if files else {}).get("videos", [])
                     if now - datetime.fromisoformat(v["published"].replace("Z", "+00:00")) >= timedelta(hours=48)),
                    key=lambda v: -v["views"])
    if len(videos) < count + 3:
        return ""

    def line(v: dict) -> str:
        watched = (v.get("analytics") or {}).get("averageViewPercentage")
        return f"- {v['title']}: {v['views']:,} views" + (f", watched {watched:.0f}% through" if watched else "")

    return ("Our Shorts viewers watched most; favour subjects with the same pull:\n"
            + "\n".join(line(v) for v in videos[:count])
            + "\nAnd least; avoid subjects like these:\n" + "\n".join(line(v) for v in videos[-3:]) + "\n\n")


def add_topics(run: Run, backlog_count: int = 4) -> list[str]:
    """Fresh, well-documented topics for the backlog, plus an anniversary 3 to 8 weeks out."""
    from .research import _existing

    topics = calendar_topics()
    launch = [line for line in CALENDAR.read_text(encoding="utf-8").splitlines() if re.match(r"\| \d+ \| ep\d+ ", line)]
    known = [t["topic"] for t in topics] + [e["title"] or "" for e in episodes()] + launch
    today = _now().date()
    series_counts = {s: sum(1 for t in topics if t["kind"] == "backlog" and t["series"] == s and not t["status"]) for s in SERIES}
    prompt = (
        "You find topics for Days of Odd, a YouTube Shorts channel of strange-but-true history for American and "
        "British viewers of every age (about 30 seconds each, told with archive pictures). Series: "
        + "; ".join(SERIES) + ".\n\n"
        f"Suggest {backlog_count + 6} new backlog topics and 2 anniversaries dated between "
        f"{(today + timedelta(days=21)).strftime('%b %d')} and {(today + timedelta(days=56)).strftime('%b %d')}. Each must be a "
        "true story with a genuinely surprising fact, well documented by reputable sources, with an English "
        "Wikipedia article (give its exact title), and with period pictures likely on Wikimedia Commons. The "
        "subject must be one most Americans or Britons already know or instantly recognize: a famous person "
        "(a president, a royal, an inventor, an artist, an outlaw), a landmark, a brand, a famous ship, war, "
        "disaster, crime, or heist. \"Teddy Roosevelt was shot and gave his speech anyway\" is the kind of story; "
        "an obscure official, town, or court case is not. No gore-centered stories, no living private people, no "
        "topics already listed below or close variants. Favor series with few open topics. Write each topic as a "
        "short line naming the story, the year, and the twist, like: \"The Great Molasses Flood (1919)\". Dates "
        "look like \"Oct 7\".\n\n"
        + _winners()
        + f"Open topics per series: {json.dumps(series_counts)}\n\nAlready listed or made:\n" + "\n".join(f"- {k}" for k in known)
    )
    # Without new topics the calendar runs dry in weeks, so this falls back to the light models' separate quota.
    answer = llm.generate(prompt, schema=TOPICS_SCHEMA, models=llm.BROAD, purpose="new topics")
    added = []
    known_words = [_key_words(k) for k in known]
    known_names = [_name_words(k) for k in known]

    def fresh(item) -> bool:
        words = _key_words(item["topic"])
        if not words or any(len(words & k) >= max(2, len(words) // 2) for k in known_words):
            return False
        # A long topic line shares few words with a short one about the same person ("Timothy Dexter (1806): got
        # rich by accident selling mittens..." against ep008's "Lord" Timothy Dexter), but names the same article.
        article = _name_words(item["wikipedia"])
        if article and any(article <= k for k in known_names):
            return False
        return bool(_existing([item["wikipedia"]]))

    suggested = [i for i in answer.get("backlog", []) if fresh(i)]
    readers = demand([{"topic": i["wikipedia"]} for i in suggested])
    suggested = sorted((i for i in suggested if readers.get(i["wikipedia"], 0) >= MIN_READERS),
                       key=lambda i: -readers[i["wikipedia"]])
    lines = CALENDAR.read_text(encoding="utf-8").splitlines()
    for item in suggested[:backlog_count]:
        heading = f"### {item['series']}"
        if heading not in lines:
            continue
        start = lines.index(heading)
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("#")), len(lines))
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, f"- {item['topic']}")
        known_words.append(_key_words(item["topic"]))
        known_names.append(_name_words(item["topic"]))
        added.append(f"{item['topic']} ({item['series']})")
    for item in [i for i in answer.get("anniversaries", []) if fresh(i) and _next_date(i["date"], today)][:1]:
        header = next(i for i, l in enumerate(lines) if l.startswith("## Anniversaries"))
        end = next(i for i in range(header + 1, len(lines)) if lines[i].startswith("## "))
        while not lines[end - 1].startswith("|"):
            end -= 1
        when = _next_date(item["date"], today)
        rows = list(range(header + 1, end))
        position = end
        for i in rows:
            cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            other = _next_date(cells[0], today) if cells and cells[0] not in ("Date",) and not cells[0].startswith("---") else None
            if other and other > when:
                position = i
                break
        lines.insert(position, f"| {item['date']} | {item['topic']} | {item['series']} |")
        added.append(f"{item['date']}: {item['topic']} (anniversary)")
    CALENDAR.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return added


# --- producing and publishing ----------------------------------------------------------------------------


def month_minutes() -> float:
    """Actions minutes this calendar month (UTC), from the run history; each job is billed in whole minutes."""
    month = _now().astimezone(timezone.utc).strftime("%Y-%m")
    total = 0.0
    for line in (STATUS / "history.jsonl").read_text(encoding="utf-8").splitlines() if (STATUS / "history.jsonl").exists() else []:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("host") == "github-actions" and str(entry.get("started_at_utc", "")).startswith(month):
            total += int(entry.get("seconds", 0) // 60) + 2
    return total


def runner_minutes_allowed() -> float:
    """Minutes this run may spend making Shorts on the runner: the month's use follows a straight line to the
    budget, so making Shorts here can't use up the minutes in the first week and stop the channel for the rest."""
    now = _now().astimezone(timezone.utc)
    elapsed = (now.day - 1 + (now.hour + now.minute / 60) / 24) / calendar.monthrange(now.year, now.month)[1]
    line = (MONTH_MINUTES - MINUTES_RESERVE) * elapsed + RUNNER_MINUTES_PER_SHORT
    return max(0.0, min(PRODUCE_MINUTES, line - month_minutes()))


def capacity() -> dict:
    """How many Shorts this run can start: on Modal while this month's credit lasts, then on this runner."""
    cloud, credit_left = 0, None
    if (os.environ.get("MODAL_TOKEN_ID") or ON_MODAL) and os.environ.get("YTC_STUDIO_HERE") != "1":
        if (cost := _modal_cost()) is not None:
            # Workers still going haven't been billed in full.
            credit_left = MODAL_CREDIT - MODAL_RESERVE - cost["metered"] - len(inventory()["remote"]) * MODAL_PER_SHORT
            cloud = max(0, int(credit_left // MODAL_PER_SHORT))
    minutes = runner_minutes_allowed() if os.environ.get("GITHUB_ACTIONS") else 0 if ON_MODAL else PRODUCE_MINUTES
    return {"cloud": cloud, "runner": int(minutes // RUNNER_MINUTES_PER_SHORT) * LANES, "runner_minutes": round(minutes),
            "modal_credit_left": None if credit_left is None else round(credit_left, 2)}


def _next_quota_reset(after: datetime | None = None) -> datetime:
    """The first reset of Gemini's free daily quotas (midnight Pacific time) after `after`, or from now."""
    pacific = after.astimezone(ZoneInfo("America/Los_Angeles")) if after else datetime.now(ZoneInfo("America/Los_Angeles"))
    return datetime.combine(pacific.date() + timedelta(days=1), clock(0, 5), pacific.tzinfo).astimezone(IST)


def _refund_attempt(folder: Path, spawned: bool, quota: bool = False, stopped: datetime | None = None) -> None:
    """An episode stopped by Gemini's quota or an overload keeps the attempt it didn't get to use. One stopped by
    the quota (which with the backup models only the review runs out of) waits for the first reset after it
    stopped (_jobs): a worker that ran out before a reset and is collected after it can go on at once."""
    meta = _json(folder / "topic.json")
    if meta:
        meta["attempts"] = max(0, meta.get("attempts", 1) - 1)
        if spawned:
            meta["spawns"] = max(0, meta.get("spawns", 0) - 1)
        if quota:
            meta["waits_until"] = _next_quota_reset(stopped).isoformat(timespec="minutes")
        _write_json(folder / "topic.json", meta)


def _waiting(meta: dict) -> bool:
    """The episode stopped for a review Gemini's Flash models had no quota left for, and they haven't reset yet."""
    return bool(meta.get("waits_until")) and _now() < datetime.fromisoformat(meta["waits_until"])


def _finish_topic(episode_id: str, outcome: str) -> None:
    if outcome not in ("ready", "rejected"):
        return
    if topic := next((t for t in calendar_topics() if t["episode"] == episode_id), None):
        mark_topic(topic, "done" if outcome == "ready" else _rejected_mark(episode_id), episode_id)


def _rejected_mark(episode_id: str) -> str:
    """Parked (tried again after PARK_DAYS) unless the research found too little to go on or this was the retry."""
    folder = REJECTED / episode_id
    reason = str((_json(folder / "review.json") or {}).get("reason", ""))
    retry = (_json(folder / "topic.json") or {}).get("retry_of")
    return "dropped" if reason.startswith("research:") or retry else "parked"


def _resumable(eps: list[dict]) -> list[dict]:
    """Half-made episodes that can go on now. One waiting for Flash's review is left until the reset, unless so few
    are ready that a backup model judges it (studio.review)."""
    few_ready = inventory(eps)["total"] < CURSOR_BELOW
    return [e for e in eps if e["state"] == "making" and (few_ready or not _waiting(e["topic"]))]


def _jobs(count: int) -> list[dict]:
    """Half-made episodes first (_resumable), then new topics from the calendar, each with its folder and topic.json."""
    from . import studio

    eps = episodes()
    resumable = {e["id"] for e in _resumable(eps)}
    half_made = []
    for e in [e for e in eps if e["state"] == "making"]:
        if e["format"] < FORMAT:
            studio.reject(e["folder"], f"half-made in format {e['format']}, which format {FORMAT} replaced on 2026-10-07")
            _finish_topic(e["id"], "rejected")
        elif e["topic"].get("spawns", 0) >= MAX_SPAWNS:
            studio.reject(e["folder"], f"its cloud worker stopped {MAX_SPAWNS} times without finishing")
            _finish_topic(e["id"], "rejected")
        elif e["id"] in resumable:
            half_made.append(e)
    jobs = [{"id": e["id"], "topic": e["topic"]["topic"], "series": e["topic"]["series"], "at": e["topic"].get("at")}
            for e in half_made[:count]]
    for topic in choose_topics(count - len(jobs), eps):
        episode_id = studio.next_id()
        at = topic["date"].isoformat() if topic["kind"] == "anniversary" and topic.get("date") else None
        meta = {"topic": topic["topic"], "series": topic["series"], "at": at,
                "started_at": _now().isoformat(timespec="seconds"), "attempts": 0, "format": FORMAT}
        if topic["status"] == "parked":
            meta["retry_of"] = topic["episode"]
        _write_json(EPISODES / episode_id / "topic.json", meta)
        mark_topic(topic, "making", episode_id)
        jobs.append({"id": episode_id, "topic": topic["topic"], "series": topic["series"], "at": at})
    return jobs


def _spawn(run: Run, jobs: list[dict]) -> list[dict]:
    """Start a Modal worker per episode. Returns the jobs this runner has to make itself."""
    from . import cloud

    if not any(s["name"] == "deploy" for s in run.stages):
        with run.stage("deploy"):
            cloud.deploy()
    # A run on Modal is inside the deployed app, so its workers can still start on the version already there.
    if not any(s["name"] == "deploy" and s["ok"] for s in run.stages) and not ON_MODAL:
        return jobs
    ready = inventory()["total"]
    cursor, last_resort = ready < CURSOR_BELOW, ready < LAST_RESORT_BELOW
    for job in jobs:
        folder = EPISODES / job["id"]
        with run.stage(f"start {job['id']}") as entry:
            entry["detail"] = job["topic"]
            meta = _json(folder / "topic.json", {})
            meta["spawns"] = meta.get("spawns", 0) + 1
            _write_json(folder / "topic.json", meta)
            key = f"{job['id']}-{secrets.token_hex(4)}"
            call_id = cloud.spawn(ROOT, {**job, "key": key, "cursor": cursor, "last_resort": last_resort})
            _write_json(folder / "remote.json", {"call_id": call_id, "key": key, "spawned_at": _now().isoformat(timespec="seconds"),
                                                 "topic": job["topic"]})
            run.spawned.append({"id": job["id"], "topic": job["topic"], "call_id": call_id})
        # A failed start may still have started a worker, so the episode waits for the next run rather than
        # being made here as well.
    return []


def _produce_here(run: Run, jobs: list[dict], minutes: float) -> None:
    """Make Shorts on this machine, LANES at a time, starting each only while `minutes` more can pay for it."""
    from . import llm, studio

    if not jobs:
        return
    # Modal's credit is for its workers; and minutes spent waiting out an overloaded Gemini are billed here.
    os.environ["YTC_RENDER_HERE"] = "1"
    llm.OVERLOAD_WAIT = 300
    # As with workers (_spawn): Cursor finishes a Short only while few are ready, the only time it starts one.
    llm.CURSOR_FALLBACK = inventory()["total"] < CURSOR_BELOW
    studio.LAST_RESORT = inventory()["total"] < LAST_RESORT_BELOW
    deadline = run.minutes() + minutes
    waiting = list(jobs)
    # Also guards CALENDAR.md, which finishing a topic rewrites.
    lock = threading.Lock()
    stop, resting = threading.Event(), threading.Event()

    def next_job() -> dict | None:
        with lock:
            if stop.is_set() or not waiting or run.minutes() + RUNNER_MINUTES_PER_SHORT > deadline + 1:
                return None
            if llm.gemini_spent() and inventory()["total"] >= CURSOR_BELOW:
                resting.set()
                stop.set()
                return None
            run.started_here += 1
            return waiting.pop(0)

    def lane() -> None:
        while job := next_job():
            with run.stage(f"make {job['id']}") as entry:
                entry["detail"] = job["topic"]
                result = studio.produce(job["topic"], job["series"], job["id"], at=job["at"])
                run.made.append(result)
                entry["detail"] = f"{job['topic']}: {result['outcome']}" + (f" ({result.get('reason')})" if result.get("reason") else "")
                with lock:
                    _finish_topic(job["id"], result["outcome"])
            if entry.get("quota") or "Overloaded" in entry.get("error", ""):
                with lock:
                    _refund_attempt(EPISODES / job["id"], spawned=False, quota=bool(entry.get("quota")))
                if entry.get("quota"):
                    run.quota_until = _next_quota_reset()
                stop.set()

    lanes = [threading.Thread(target=lane, name=f"lane-{n + 1}") for n in range(min(LANES, len(jobs)))]
    for thread in lanes:
        thread.start()
    for thread in lanes:
        thread.join()
    if llm.gemini_spent():
        run.quota_until = _next_quota_reset()
    if resting.is_set():
        run.notes.append(f"Gemini's free quota is spent and {inventory()['total']} Shorts are ready; "
                         "the rest waits for its reset instead of going to Cursor")
    elif stop.is_set():
        run.notes.append("Gemini is out of free quota or overloaded; the rest waits for the next run")
    elif waiting:
        run.notes.append(f"{len(waiting)} Shorts wait for a later run's share of the Actions minutes")
    run.renders["modal"] += studio.renders.count("modal")
    run.renders["runner"] += studio.renders.count("runner")
    studio.renders.clear()


def produce(run: Run, count: int) -> None:
    """Start up to `count` Shorts: Modal workers while this month's credit lasts, then on this runner."""
    room = run.capacity = capacity()
    jobs = _jobs(min(count, room["cloud"] + room["runner"]))
    if len(jobs) < count:
        run.notes.append(f"room for {len(jobs)} of the {count} Shorts wanted: Modal's credit covers {room['cloud']}, "
                         f"this run's share of the Actions minutes {room['runner']}")
    cloud, here = jobs[:room["cloud"]], jobs[room["cloud"]:]
    if cloud:
        not_started = _spawn(run, cloud)
        if not_started:
            run.notes.append("Modal couldn't be deployed; making the Shorts on this runner")
            here = not_started + here
    _produce_here(run, here, room["runner_minutes"])


def collect(run: Run) -> str:
    """Bring in what finished Modal workers made. A worker that outlives its timeout is stopped and retried."""
    from . import cloud

    done = []
    for e in [e for e in episodes() if e["state"] == "remote"]:
        remote = e["remote"]
        outcome = cloud.collect(ROOT, e["id"], remote["call_id"], remote.get("key"))
        if outcome is None:
            hours = (_now() - datetime.fromisoformat(remote["spawned_at"])).total_seconds() / 3600
            if hours < STALE_HOURS:
                done.append(f"{e['id']} still working ({hours:.1f} h)")
                continue
            cloud.cancel(remote["call_id"])
            outcome = {"id": e["id"], "outcome": "unfinished", "reason": f"the worker ran over {STALE_HOURS} hours and was stopped"}
        (e["folder"] / "remote.json").unlink(missing_ok=True)
        if outcome["outcome"] == "rejected" and (ROOT / "content" / "rejected" / e["id"]).exists():
            shutil.rmtree(e["folder"])
        outcome.setdefault("topic", remote.get("topic"))
        renders = outcome.pop("renders", [])
        run.renders["modal"] += renders.count("modal")
        run.renders["runner"] += renders.count("runner")
        for call in outcome.pop("llm_calls", []):
            run.worker_calls.append(call)
        stopped = datetime.fromisoformat(outcome.pop("stopped_at", None) or remote["spawned_at"])
        run.made.append(outcome)
        _finish_topic(e["id"], outcome["outcome"])
        gemini = outcome.get("quota") or outcome.get("overloaded")
        if gemini or (outcome["outcome"] == "unfinished" and _modal_credit_spent()):
            _refund_attempt(e["folder"], spawned=True, quota=bool(outcome.get("quota")), stopped=stopped)
            why = ("Gemini's free quota" if outcome.get("quota") else "Gemini's free tier being overloaded") if gemini \
                else "Modal's monthly credit running out"
            run.notes.append(f"{e['id']} stopped on {why}; it resumes in a later run")
        if outcome.get("quota") and (reset := _next_quota_reset(stopped)) > _now():
            run.quota_until = reset
        done.append(f"{e['id']} {outcome['outcome']}" + (f" ({outcome.get('reason')})" if outcome.get("reason") else ""))
    waiting = {e["remote"].get("key") or e["id"] for e in episodes() if e["state"] == "remote"}
    try:
        if removed := cloud.sweep(waiting):
            done.append(f"cleared {removed} files no episode was waiting for")
    except Exception as err:
        log.warning("couldn't clear old worker files: %s", err)
    return "; ".join(done)[:600] or "nothing in the cloud"


def _scores(e: dict) -> int:
    held = e.get("held") or {}
    if held.get("scores"):
        return sum(held["scores"].values())
    text = (e["folder"] / "research.md").read_text(encoding="utf-8") if (e["folder"] / "research.md").exists() else ""
    match = re.search(r"\| Hook \| Clarity \| Payoff \| Visuals \| Loop \|\n\|[-| ]+\|\n\|([^\n]+)\|", text)
    return sum(int(x) for x in re.findall(r"\d", match.group(1))) if match else 0


def check_channel(run: Run) -> bool:
    """Whether Buffer can post to the YouTube channel. Disconnected, locked, or paused, it fails every queued Short,
    and only the owner can fix it."""
    from .publish import channel

    with run.stage("buffer channel") as entry:
        found = channel()
        wrong = [name for flag, name in (("isDisconnected", "disconnected"), ("isLocked", "locked"),
                                          ("isQueuePaused", "paused")) if found.get(flag)]
        entry["detail"] = " and ".join(wrong) or "connected"
        if wrong:
            run.owner_action.append(
                f"Buffer's YouTube channel is {entry['detail']}, so no Short goes out until it's fixed: in the Chrome "
                "profile signed in to the studio's Buffer account open https://publish.buffer.com, go to Settings > "
                "Channels, and reconnect or unpause Days of Odd.")
            return False
    return True


def _resending(record: dict) -> bool:
    """Whether cloud.slot_watch may still send a failed post again: its due time, which each try moves, is within
    publish.RESEND_WINDOW."""
    from .publish import RESEND_WINDOW

    return bool(record.get("due_at")) and timedelta(0) <= _now() - datetime.fromisoformat(record["due_at"]) <= RESEND_WINDOW


def repair_posts(run: Run, requeue: bool = True) -> None:
    """Keep every Short's hosted video within render.MAX_UPLOAD_MB (Buffer fetches it when the post goes out), and
    send a post Buffer couldn't publish back to waiting, up to RETRIES times (not while `requeue` is off: a post
    that failed on a disconnected channel would only fail again), once cloud.slot_watch has stopped sending it
    again."""
    from .publish import delete_post, edit_post, hosted_bytes, shrink_hosted, unhost_video
    from .render import MAX_UPLOAD_MB
    from .spec import ShortSpec

    for e in episodes():
        name = "publish.json" if e["record"] else "hold.json"
        record = e["record"] or e["held"]
        path = e["folder"] / name
        if e["state"] in ("scheduled", "waiting") and record.get("media_url") and "media_bytes" not in record:
            try:
                record["media_bytes"] = hosted_bytes(record["media_url"])
            except requests.RequestException as err:
                log.warning("couldn't size %s's video: %s", e["id"], err)
                continue
            if record["media_bytes"] > MAX_UPLOAD_MB * 1_000_000:
                with run.stage(f"shrink {e['id']}'s video") as entry:
                    old = record["media_public_id"]
                    record["media_public_id"], record["media_url"] = shrink_hosted(old, record["media_url"])
                    if e["state"] == "scheduled":
                        edit_post(record["buffer_post_id"], ShortSpec.load(e["folder"] / "short.yaml"), record["media_url"])
                    entry["detail"] = f"{record['media_bytes'] / 1e6:.0f} MB -> "
                    record["media_bytes"] = hosted_bytes(record["media_url"])
                    entry["detail"] += f"{record['media_bytes'] / 1e6:.0f} MB"
                    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
                    unhost_video(old)
                continue
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        elif e["state"] == "error" and requeue and record.get("retries", 0) < RETRIES and not _resending(record):
            with run.stage(f"requeue {e['id']}") as entry:
                record["retries"] = record.get("retries", 0) + 1
                path.write_text(json.dumps(record, indent=2), encoding="utf-8")
                old = record["media_public_id"]
                smaller = shrink_hosted(old, record["media_url"])
                public_id, url = smaller or (old, record["media_url"])
                held = {"id": e["id"], "title": record["title"], "series": e["series"], "media_public_id": public_id,
                        "media_url": url, "media_bytes": hosted_bytes(url), "hosted_at": _now().isoformat(timespec="seconds"),
                        "retries": record["retries"], "failed": record.get("error")}
                (e["folder"] / "hold.json").write_text(json.dumps(held, indent=2), encoding="utf-8")
                delete_post(record["buffer_post_id"])
                path.unlink()
                if smaller:
                    unhost_video(old)
                entry["detail"] = f"back to waiting after Buffer failed it ({record.get('error') or 'no reason given'})"[:300]


def preflight_posts(run: Run) -> None:
    """Check each post due within PREFLIGHT_HOURS as it will go out: Buffer still has it, scheduled and public, with
    a title and description YouTube takes and the video its record hosts, and that video downloads whole. What a
    run can fix it fixes; anything else fails the stage, which the monitor raises as an alert."""
    from .publish import buffer_busy, description, edit_post, fetch_video, hosted_bytes, post, youtube_problems
    from .spec import ShortSpec

    soon = _now() + timedelta(hours=PREFLIGHT_HOURS)
    for e in episodes():
        if buffer_busy():
            return
        record = e["record"]
        if e["state"] != "scheduled" or not record.get("due_at") or datetime.fromisoformat(record["due_at"]) > soon:
            continue
        with run.stage(f"preflight {e['id']}") as entry:
            path = e["folder"] / "publish.json"
            found = post(record["buffer_post_id"])
            if found is None:
                keep = ("media_public_id", "media_url", "media_bytes", "hosted_at", "retries")
                held = {"id": e["id"], "title": record["title"], "series": e["series"],
                        **{k: record[k] for k in keep if k in record}}
                (e["folder"] / "hold.json").write_text(json.dumps(held, indent=2), encoding="utf-8")
                path.unlink()
                entry["detail"] = "Buffer no longer has its post, so it's back to waiting"
                continue
            if found["status"] in ("sending", "sent", "error"):
                entry["detail"] = f"Buffer has it as {found['status']}"
                continue
            if found["status"] != "scheduled":
                raise RuntimeError(f"Buffer has the post due {record['due_at']} as {found['status']}, so it won't go out")
            url = record.get("media_url") or found["video"]
            fixed = youtube_problems(found["title"], found["text"] or "")
            if found["privacy"] != "public":
                fixed.append(f"privacy {found['privacy']}")
            if found["video"] != url:
                fixed.append("video link")
            if fixed:
                spec = ShortSpec.load(e["folder"] / "short.yaml")
                manifest = json.loads((e["folder"] / "work" / "manifest.json").read_text(encoding="utf-8"))
                edit_post(record["buffer_post_id"], spec, url, description(spec, manifest))
            if record.get("fetched_url") == url:
                hosted_bytes(url)
                checked = "video still hosted"
            else:
                fetched = fetch_video(url)
                record.update(media_bytes=fetched["bytes"], fetched_url=url)
                path.write_text(json.dumps(record, indent=2), encoding="utf-8")
                checked = f"video downloads whole ({fetched['bytes'] / 1e6:.0f} MB in {fetched['seconds']:.0f} s)"
            entry["detail"] = (f"fixed {', '.join(fixed)}; " if fixed else "") + checked


def per_day(stock: int, slots: int) -> int:
    """Shorts a day to schedule: as many as leaves three days of posting in the made-but-unposted stock, but at least
    one, so a slow stretch of making thins the posting out instead of leaving days with nothing."""
    return max(1, min(slots, stock // 3))


# Shorts a day (decision 2026-10-07): 2 while format 2 proves itself. Three a day of near-identical Shorts is the
# template mass production YouTube's Shorts ranking began holding back on 1 Oct 2026.
BASE_PER_DAY = 2
# Days from which a day gets one more of publish.SLOTS (decision 2026-09-26), paused with format 2.
PACE_STEPS: tuple[int, ...] = ()


def slots_per_day(today: date | None = None) -> int:
    """How many of publish.SLOTS a day uses: BASE_PER_DAY, one more from each of PACE_STEPS' days, for as long as the
    last 10 Shorts keep at least 90% of the engaged share of the Shorts before them."""
    from .publish import SLOTS

    steps = sum(day_number(today) >= day for day in PACE_STEPS)
    if steps:
        # Scheduling runs through here, so a stats file it can't read costs the extra slots, not the posting.
        try:
            latest = max(ANALYTICS.glob("*.json"), default=None)
            rows = [r for r in _video_rows(_json(latest, {}), episodes()) if r["hours"] >= 48
                    and isinstance(r["engaged_share"], (int, float))] if latest else []
            recent, before = _median(r["engaged_share"] for r in rows[-10:]), _median(r["engaged_share"] for r in rows[:-10])
        except Exception as err:
            log.warning("couldn't read engagement for the posting pace (%s); posting %d a day", err, BASE_PER_DAY)
            recent = before = None
        if not (recent and before and recent >= 0.9 * before):
            steps = 0
    return min(len(SLOTS), BASE_PER_DAY + steps)


def publish_waiting(run: Run) -> None:
    from .publish import AUDIENCE_TZ, BUFFER_QUEUE_LIMIT, SLOTS, next_slots, posts, schedule

    eps = episodes()
    waiting = [e for e in eps if _postable(e)]
    if not waiting:
        return
    # Three days back covers every day a new post can land on; all of Buffer's history would cost a request per 50 posts.
    everything = [p for p in posts(since=datetime.now(AUDIENCE_TZ) - timedelta(days=3)) if p["status"] not in ("error", "draft")]
    queued = [p for p in everything if p["status"] != "sent"]
    free = BUFFER_QUEUE_LIMIT - len(queued)
    if free <= 0:
        run.notes.append(f"Buffer is full ({len(queued)} queued); {len(waiting)} waiting")
        return
    slots = slots_per_day()
    pace = per_day(len(queued) + len(waiting), slots)
    if pace < slots:
        run.notes.append(f"only {len(queued) + len(waiting)} Shorts made and unposted; scheduling {pace} a day")
    # Sent posts count toward their day's number.
    taken = {datetime.fromisoformat(p["dueAt"]).astimezone(AUDIENCE_TZ) for p in everything if p.get("dueAt")}
    by_post = {e["record"]["buffer_post_id"]: e for e in eps if e["record"]}
    last = max(queued, key=lambda p: p.get("dueAt") or "", default=None)
    last_series = by_post.get(last["id"], {}).get("series") if last else None
    today = datetime.now(AUDIENCE_TZ)

    def anniversary(e):
        at = (e.get("held") or {}).get("anniversary") or (e.get("topic") or {}).get("at")
        return date.fromisoformat(at) if at else None

    # A Short whose post Buffer failed already lost one slot, so it takes the next one after anniversaries.
    ordered = sorted(waiting, key=lambda e: (anniversary(e) is None, anniversary(e) or date.max,
                                             not (e.get("held") or {}).get("retries"), -_scores(e), e["id"]))
    for _ in range(min(free, len(ordered))):
        pick = next((e for e in ordered if anniversary(e) or e["series"] != last_series), ordered[0])
        ordered.remove(pick)
        when = None
        if day := anniversary(pick):
            for slot in SLOTS[:3]:
                candidate = datetime.combine(day, slot, AUDIENCE_TZ)
                if candidate > today + timedelta(minutes=30) and candidate not in taken:
                    when = candidate
                    break
        when = when or next_slots(1, taken, per_day=pace)[0]
        with run.stage(f"schedule {pick['id']}") as entry:
            record = schedule(pick["folder"] / "short.yaml", when)
            taken.add(datetime.fromisoformat(record["due_at"]).astimezone(AUDIENCE_TZ))
            run.published.append({"id": pick["id"], "title": pick["title"], "due_at": record["due_at"]})
            entry["detail"] = record["due_at"]
            last_series = pick["series"]
        if not run.stages[-1]["ok"]:
            break


# --- daily routine ---------------------------------------------------------------------------------------


def _video_rows(analytics: dict, eps: list[dict]) -> list[dict]:
    by_video = {}
    for e in eps:
        url = (e["record"] or {}).get("youtube_url") or ""
        if match := re.search(r"(?:shorts/|v=|youtu\.be/)([\w-]{11})", url):
            by_video[match.group(1)] = e
    rows = []
    now = datetime.now(IST)
    for video in analytics.get("videos", []):
        e = by_video.get(video["id"])
        published = datetime.fromisoformat(video["published"].replace("Z", "+00:00"))
        a = video.get("analytics") or {}
        at10 = min(video.get("retention") or [], key=lambda p: abs(p["at"] - 0.1), default=None)
        script = _json(e["folder"] / "script.json", {}) if e else {}
        rows.append({
            "short": e["id"] if e else video["id"], "series": (e or {}).get("series") or "?", "format": (e or {}).get("format", 1),
            "hook_style": script.get("hook_style", "–"), "title": video["title"], "views": video["views"],
            "likes": video["likes"], "comments": video["comments"], "hours": round((now - published).total_seconds() / 3600),
            "engaged_share": a.get("engaged_share"), "avg_viewed": a.get("averageViewPercentage"),
            "at10": at10["watching"] if at10 else None, "subs": a.get("subscribersGained"),
            "hook": (yaml.safe_load((e["folder"] / "short.yaml").read_text(encoding="utf-8"))["beats"][0]["text"] if e else ""),
        })
    return sorted(rows, key=lambda r: r["short"])


def _median(values) -> float | None:
    values = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(values), 3) if values else None


LEARN_SCHEMA = {
    "type": "object",
    "properties": {
        "reasons": {"type": "array", "items": {"type": "object", "properties": {
            "short": {"type": "string"}, "reason": {"type": "string"}}, "required": ["short", "reason"]}},
        "rules": {"type": "array", "items": {"type": "string"}},
        "hypotheses": {"type": "array", "items": {"type": "string"}},
        "log": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["reasons", "rules", "hypotheses", "log", "summary"],
}


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start: end if end != -1 else len(text)]


def update_learnings(run: Run, analytics: dict) -> dict:
    eps = episodes()
    rows = [r for r in _video_rows(analytics, eps) if r["hours"] >= 48]
    text = LEARNINGS.read_text(encoding="utf-8")
    recent = rows[-10:]
    medians = {"engaged_share": _median(r["engaged_share"] for r in recent), "avg_viewed": _median(r["avg_viewed"] for r in recent),
               "at10": _median(r["at10"] for r in recent),
               "subs_per_1000": _median((r["subs"] or 0) * 1000 / r["views"] for r in recent if r["views"]),
               "likes_per_100": _median(r["likes"] * 100 / r["views"] for r in recent if r["views"])}
    answer = llm.generate(
        "You keep the learning log of Days of Odd, a Shorts channel of strange-but-true history. From the numbers "
        "below, explain each Short's result in a few words (hook, topic, pacing, visuals, loop), judged against the "
        "channel medians. Promote a hypothesis to a rule only when 2 or more Shorts show the same effect, and cite "
        "them; drop hypotheses the data contradicts; keep the rest. Rules and hypotheses must be concrete enough "
        "for a scriptwriter to apply. Format 2 (shorter Shorts about famous subjects, a new voice and music) replaced "
        "format 1 on 2026-10-07: judge each Short against Shorts of its own format, and never keep a rule or "
        "hypothesis that contradicts format 2. log: one line on what changed today and why. summary: 2 sentences on "
        "what the numbers say for tomorrow's scripts.\n\nCurrent file:\n" + text.split("## Scoreboard")[0]
        + "\n\nShorts with 48+ hours of data:\n" + json.dumps(rows, indent=1) + "\n\nMedians of the last 10: " + json.dumps(medians),
        schema=LEARN_SCHEMA, purpose="learnings",
    )
    reasons = {r["short"]: r["reason"] for r in answer["reasons"]}

    def pct(v):
        return "–" if v is None else f"{v * 100:.0f}%"

    def share(v):
        return "–" if v is None else f"{v:.1f}%"

    board = [
        "| Short | Series | Hook style | Views | Engaged share | Avg % viewed | Watching at 10% | Subs gained | Likely reason |",
        "|---|---|---|---|---|---|---|---|---|",
    ] + [
        f"| {r['short']} | {r['series']} | {r['hook_style']} | {r['views']} | {pct(r['engaged_share'])} | {share(r['avg_viewed'])} | "
        f"{pct(r['at10'])} | {r['subs'] if r['subs'] is not None else '–'} | {reasons.get(r['short'], '–')} |"
        for r in rows
    ]
    what = _section(text, "## What to beat")
    for label, key, fmt in (("Engaged share", "engaged_share", pct), ("Average percentage viewed", "avg_viewed", share),
                            ("Still watching at 10%", "at10", pct), ("Subscribers per 1,000 views", "subs_per_1000", lambda v: "–" if v is None else f"{v:.1f}"),
                            ("Likes per 100 views", "likes_per_100", lambda v: "–" if v is None else f"{v:.1f}")):
        value = medians[key]
        what = re.sub(rf"(\| {re.escape(label)}[^|]*\|[^|]*\| )([^|]*)( \|)", lambda m: m.group(1) + (fmt(value) if value is not None else "not enough data yet") + m.group(3), what, count=1)
    head = text.split("## What to beat")[0]
    rules_intro = _section(text, "## Rules we've proven").split("\n- ")[0].rstrip()
    hyp_intro = _section(text, "## Hypotheses to test").split("\n- ")[0].rstrip()
    board_intro = _section(text, "## Scoreboard").split("\n| Short")[0].rstrip()
    log_section = _section(text, "## Log").rstrip()
    new = "".join([
        head, what.rstrip(), "\n\n",
        rules_intro, "\n\n", "\n".join(f"- {r}" for r in answer["rules"]) or "- None yet.", "\n\n",
        hyp_intro, "\n\n", "\n".join(f"- {h}" for h in answer["hypotheses"]), "\n\n",
        board_intro, "\n\n", "\n".join(board), "\n\n",
        log_section, f"\n- {_now().date().isoformat()}: {answer['log']}\n",
    ])
    for heading in ("## What to beat", "## Rules we've proven", "## Hypotheses to test", "## Scoreboard", "## Log"):
        if heading not in new:
            raise RuntimeError(f"rebuilt LEARNINGS.md lost {heading}; left unchanged")
    LEARNINGS.write_text(new, encoding="utf-8")
    return {"rows": rows, "medians": medians, "summary": answer["summary"], "log": answer["log"]}


def _report_path(day: int) -> Path:
    return REPORTS / f"day-{day:03d}.md"


def _sessions_section(path: Path) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return text[text.index("## Production sessions"):].rstrip() + "\n" if "## Production sessions" in text else "## Production sessions\n"


def write_report(run: Run, analytics: dict, learned: dict | None, topics_added: list[str]) -> Path:
    today = _now().date()
    day = day_number(today)
    path = _report_path(day)
    eps = episodes()
    rows = {r["short"]: r for r in _video_rows(analytics, eps)} if analytics else {}
    since = datetime.now(IST) - timedelta(hours=24)
    went_live = [e for e in eps if e["state"] == "live" and e["record"].get("sent_at")
                 and datetime.fromisoformat(e["record"]["sent_at"]) >= since]
    queue = sorted((e for e in eps if e["state"] == "scheduled"), key=lambda e: e["record"]["due_at"])
    waiting = [e for e in eps if e["state"] == "waiting"]
    channel = analytics.get("channel", {}) if analytics else {}
    week_views = sum(d.get("views", 0) for d in (analytics.get("daily") or [])[-7:]) if analytics else None
    engaged90 = (analytics.get("last_90_days") or {}).get("engagedViews") if analytics else None
    made = [e for e in eps if e["held"] and datetime.fromisoformat(e["held"]["hosted_at"]) >= since]
    rejected = [p for p in (ROOT / "content" / "rejected").glob("ep*/review.json")
                if (_json(p) or {}).get("rejected_at", "") >= since.isoformat()]

    def when(iso: str) -> str:
        t = datetime.fromisoformat(iso)
        return f"{t.astimezone(ET):%a %b %d %H:%M} ET ({t.astimezone(IST):%a %H:%M} IST)"

    lines = [f"# Day {day}: {today.isoformat()}", "", "## Summary", ""]
    summary = [f"{len(went_live)} Short(s) went live in the last 24 hours; {len(queue)} are scheduled in Buffer and "
               f"{len(waiting)} wait for a slot (target {INVENTORY_TARGET})."]
    if channel:
        summary.append(f"The channel has {channel.get('subscribers', 0)} subscribers and {channel.get('views', 0)} views.")
    if learned:
        summary.append(learned["summary"])
    lines += [" ".join(summary), "", "## Published", "", "| Id | Title | Published | Link | Views | Likes | Comments |", "|---|---|---|---|---|---|---|"]
    for e in went_live:
        r = rows.get(e["id"], {})
        lines.append(f"| {e['id']} | {e['title']} | {when(e['record']['sent_at'])} | <{e['record'].get('youtube_url') or ''}> | "
                     f"{r.get('views', (e['record'].get('metrics') or {}).get('views', '–'))} | {r.get('likes', '–')} | {r.get('comments', '–')} |")
    if not went_live:
        lines.append("| – | nothing went live | | | | | |")
    lines += ["", "## Totals", ""]
    if channel:
        lines.append(f"{channel.get('subscribers', 0)} subscribers, {week_views if week_views is not None else '–'} views in the last 7 days, "
                     f"{engaged90 if engaged90 is not None else '–'} engaged views in the last 90 days (the Partner Program needs 10 million).")
    else:
        lines.append("Analytics weren't available today (see the stages below).")
    lines += ["", "## Queue", "", "| Id | Publishes | Title |", "|---|---|---|"]
    lines += [f"| {e['id']} | {when(e['record']['due_at'])} | {e['title']} |" for e in queue] or ["| – | empty | |"]
    if waiting:
        lines += ["", "Waiting for a Buffer slot: " + ", ".join(f"{e['id']} ({e['title']})" for e in waiting) + "."]
    lines += ["", "## Learned", "", (learned or {}).get("log", "Not enough data yet.") if learned else "Skipped today.", ""]
    lines += ["## Better today", ""]
    for e in made:
        md = (e["folder"] / "research.md").read_text(encoding="utf-8") if (e["folder"] / "research.md").exists() else ""
        better = re.search(r"^Better than the last: (.*)$", md, re.M)
        scores = (e["held"] or {}).get("scores") or {}
        lines.append(f"- {e['id']} \"{e['title']}\" ({e['series']}): scores " + ", ".join(f"{k} {v}" for k, v in scores.items())
                     + (f". {better.group(1)}" if better else ""))
    for p in rejected:
        lines.append(f"- {p.parent.name} was rejected: {(_json(p) or {}).get('reason')}")
    if not made and not rejected:
        lines.append("- No new Shorts in the last 24 hours.")
    lines += ["", "## Decisions", ""]
    lines += [f"- Added topic: {t}" for t in topics_added] or ["- None."]
    lines += ["", "## Owner action needed", ""]
    lines += [f"- {a}" for a in dict.fromkeys(run.owner_action)] or ["- None."]
    lines += ["", _sessions_section(path)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    if run.owner_action:
        blockers = REPORTS / "BLOCKERS.md"
        existing = blockers.read_text(encoding="utf-8") if blockers.exists() else "# Blockers\n"
        for action in dict.fromkeys(run.owner_action):
            if action not in existing:
                existing = existing.rstrip() + f"\n- {today.isoformat()}: {action}\n"
        blockers.write_text(existing, encoding="utf-8")
    return path


def weekly_review(run: Run, analytics: dict, learned: dict | None) -> Path:
    day = day_number()
    week = day // 7 + 1
    path = REPORTS / f"week-{week:02d}.md"
    rows = _video_rows(analytics, episodes()) if analytics else []
    text = llm.generate(
        f"Write the week {week} review for Days of Odd, a Shorts channel of strange-but-true history, in plain "
        "Markdown with these sections: Trend (views and subscribers), Top 3 and bottom 3 Shorts and why, What "
        "we learned, Next week's single experiment (one change, how we'll measure it). Be specific and brief. "
        "Use only these numbers:\n" + json.dumps({"rows": rows, "channel": analytics.get("channel"), "daily": analytics.get("daily"),
                                                  "learnings": learned}, indent=1, default=str),
        models=llm.STRONG, purpose="weekly review",
    )
    path.write_text(f"# Week {week} review ({_now().date().isoformat()})\n\n{text.strip()}\n", encoding="utf-8")
    return path


def daily(run: Run) -> None:
    from .youtube import save_stats

    analytics, learned, added = {}, None, []
    with run.stage("analytics") as entry:
        path = save_stats(ANALYTICS)
        analytics = _json(path, {})
        entry["detail"] = f"{analytics.get('channel', {}).get('subscribers')} subscribers"
    if analytics:
        with run.stage("learnings") as entry:
            learned = update_learnings(run, analytics)
            entry["detail"] = learned["log"][:200]
    with run.stage("new topics") as entry:
        added = add_topics(run)
        entry["detail"] = f"{len(added)} added"
    if _now().weekday() == 6 and analytics:
        with run.stage("weekly review") as entry:
            entry["detail"] = weekly_review(run, analytics, learned).name
    with run.stage("report") as entry:
        entry["detail"] = write_report(run, analytics, learned, added).name


# --- the run -------------------------------------------------------------------------------------------------


def _session_note(run: Run, inv: dict) -> None:
    path = _report_path(day_number())
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Day {day_number()}: {_now().date().isoformat()}\n\n## Production sessions\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    if "## Production sessions" not in text:
        text = text.rstrip() + "\n\n## Production sessions\n"
    made = ", ".join(f"{m['id']} {m['outcome']}" + (f" ({m.get('reason')})" if m.get("reason") else "") for m in run.made) or "nothing"
    if run.spawned:
        made += "; started in the cloud " + ", ".join(f"{s['id']} ({s['topic']})" for s in run.spawned)
    published = ", ".join(f"{p['id']} for {datetime.fromisoformat(p['due_at']).astimezone(ET):%b %d %H:%M} ET" for p in run.published) or "nothing"
    failed = [s["name"] for s in run.stages if not s["ok"]]
    where = f"run {os.environ['GITHUB_RUN_NUMBER']} on GitHub" if os.environ.get("GITHUB_RUN_NUMBER") else f"{_host()} run"
    note = (f"- {run.started:%H:%M} IST, {where} ({run.minutes():.0f} min): "
            f"made {made}; scheduled {published}; inventory {inv['total']} ({inv['in_buffer']} in Buffer, {inv['waiting']} waiting"
            + (f", {len(inv['remote'])} being made in the cloud" if inv["remote"] else "") + ")"
            + (f"; failed: {', '.join(failed)}" if failed else "") + (f"; {'; '.join(run.notes)}" if run.notes else "") + ".")
    path.write_text(text.rstrip() + "\n" + note + "\n", encoding="utf-8")


def _host() -> str:
    return "github-actions" if os.environ.get("GITHUB_ACTIONS") else "modal" if ON_MODAL else "local"


def _modal_cost() -> dict | None:
    if not (os.environ.get("MODAL_TOKEN_ID") or ON_MODAL):
        return None
    try:
        from .cloud import month_cost

        return month_cost()
    except Exception as err:
        log.warning("couldn't read Modal's billing: %s", err)
        return None


def _modal_credit_spent() -> bool:
    """Whether Modal has stopped running things because this month's credit is used up."""
    cost = _modal_cost()
    return bool(cost) and cost["metered"] >= MODAL_CREDIT - MODAL_PER_SHORT / 2


def write_status(run: Run, result: str) -> dict:
    eps = episodes()
    inv = inventory(eps)
    by_model: dict[str, int] = {}
    for call in llm.calls + run.worker_calls:
        by_model[call["model"]] = by_model.get(call["model"], 0) + 1
    state = _json(STATUS / "state.json", {})
    status = {
        "updated_at": _now().isoformat(timespec="seconds"),
        "day": day_number(),
        "result": result,
        "run": {
            "host": _host(),
            "id": os.environ.get("GITHUB_RUN_ID") or os.environ.get("MODAL_TASK_ID"), "number": os.environ.get("GITHUB_RUN_NUMBER"),
            "trigger": run.trigger,
            "started_at": run.started.isoformat(timespec="seconds"),
            "started_at_utc": run.started.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds"),
            "seconds": round(run.minutes() * 60),
        },
        "stages": run.stages,
        "errors": run.errors,
        "owner_action": list(dict.fromkeys(run.owner_action)),
        "notes": run.notes,
        "inventory": inv,
        "queue": [{"id": e["id"], "title": e["title"], "due_at": e["record"]["due_at"]}
                  for e in sorted((e for e in eps if e["state"] == "scheduled"), key=lambda e: e["record"]["due_at"])],
        "waiting": [{"id": e["id"], "title": e["title"], "series": e["series"], "score": _scores(e)} for e in eps if e["state"] == "waiting"],
        "made": run.made,
        "spawned": run.spawned,
        "in_cloud": [{"id": e["id"], "topic": e["remote"]["topic"], "spawned_at": e["remote"]["spawned_at"]}
                     for e in eps if e["state"] == "remote"],
        "published": run.published,
        "live": sum(1 for e in eps if e["state"] == "live"),
        "failed_posts": [{"id": e["id"], "error": e["record"].get("error")} for e in eps
                         if e["state"] == "error" and not _resending(e["record"])],
        "not_live": run.not_live,
        "llm": {"calls": len(llm.calls) + len(run.worker_calls), "by_model": by_model, "out_of_quota": sorted(llm._spent)},
        "renders": run.renders,
        "daily_done": state.get("daily_done"),
        "minutes_this_month": month_minutes() + (run.minutes() + 2 if os.environ.get("GITHUB_ACTIONS") else 0),
        "minutes_budget": MONTH_MINUTES,
        "modal_cost": _modal_cost(),
        "modal_credit": MODAL_CREDIT,
        "capacity": run.capacity,
        "made_here": run.started_here,
        "open_topics": sum(1 for t in calendar_topics() if not t["status"]),
        "parked_topics": sum(1 for t in calendar_topics() if t["status"] == "parked"),
    }
    _write_json(STATUS / "status.json", status)
    with (STATUS / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"started_at_utc": status["run"]["started_at_utc"], "host": status["run"]["host"],
                             "number": status["run"]["number"], "trigger": run.trigger, "seconds": status["run"]["seconds"],
                             "result": result, "made": [f"{m['id']}:{m['outcome']}" for m in run.made],
                             "spawned": [s["id"] for s in run.spawned], "made_here": run.started_here,
                             "published": [p["id"] for p in run.published], "inventory": inv["total"],
                             "errors": run.errors[:5], "llm_calls": len(llm.calls)}, default=str) + "\n")
    return status


def main(produce_count: int | None = None, publish: bool = True, daily_mode: str = "auto", trigger: str = "manual") -> int:
    from .publish import buffer_busy, sync

    run = Run(trigger)
    STATUS.mkdir(parents=True, exist_ok=True)
    state = _json(STATUS / "state.json", {})
    if ON_MODAL:
        # The app's scheduled checks (cloud.slot_watch) run the code last deployed, not this clone's.
        from . import cloud

        with run.stage("deploy"):
            cloud.deploy()
    with run.stage("sync") as entry:
        records = sync(EPISODES)
        entry["detail"] = f"{len(records)} posts"
    with run.stage("playlists") as entry:
        from .youtube import file_into_playlists

        entry["detail"] = "; ".join(file_into_playlists(EPISODES))[:300] or "nothing new"
    with run.stage("on youtube") as entry:
        from .youtube import check_live

        run.not_live = check_live(EPISODES)
        entry["detail"] = "; ".join(f"{p['id']}: {p['problem']}" for p in run.not_live)[:300] or "every Short sent is public"
    if inventory()["remote"] or os.environ.get("MODAL_TOKEN_ID") or ON_MODAL:
        with run.stage("collect") as entry:
            entry["detail"] = collect(run)
    today = _now().date().isoformat()
    due = state.get("daily_done") != today and _now().hour >= DAILY_FROM_HOUR
    if daily_mode == "yes" or (daily_mode == "auto" and due):
        daily(run)
        if all(s["ok"] for s in run.stages if s["name"] in ("report",)):
            state["daily_done"] = today
            _write_json(STATUS / "state.json", state)
    can_post = False
    if publish and not buffer_busy():
        can_post = check_channel(run)
    if publish and not buffer_busy():
        if not can_post:
            run.notes.append("scheduled nothing new: Buffer can't post to the YouTube channel")
        repair_posts(run, requeue=can_post)
        preflight_posts(run)
    if can_post and not buffer_busy():
        with run.stage("publish"):
            publish_waiting(run)
    inv = inventory()
    if produce_count is None:
        # Shorts still being made in the cloud count as made, or every run would start another batch.
        want = INVENTORY_TARGET - inv["total"] - len(inv["remote"])
        # Every half-made Short that can go on does, not just a batch: after Gemini's reset most only need the review
        # they waited for, and the day's Flash quota covers far more reviews than a batch.
        batch = max(BATCH, len(_resumable(episodes())))
        produce_count = min(batch, want) if want > 0 and (want >= BATCH or inv["making"]) else 0
        stored = datetime.fromisoformat(state["gemini_quota_until"]) if state.get("gemini_quota_until") else None
        quota_until = run.quota_until or (stored if stored and _now() < stored else None)
        if produce_count and quota_until and inv["total"] >= CURSOR_BELOW:
            run.notes.append(f"Gemini's Flash quota is spent until {quota_until:%H:%M} IST: new Shorts are drafted on the "
                             "backup models, and each waits for a Flash review after the reset")
    if produce_count > 0:
        produce(run, produce_count)
        if can_post and not buffer_busy():
            with run.stage("publish"):
                publish_waiting(run)
    if until := buffer_busy():
        run.notes.append(f"Buffer's API limit is used up until {until.astimezone(IST):%H:%M} IST, so this run left Buffer "
                         "alone; the Shorts it already has still go out, and a later run schedules the waiting ones")
    if run.quota_until:
        state["gemini_quota_until"] = run.quota_until.isoformat(timespec="minutes")
        _write_json(STATUS / "state.json", state)
    inv = inventory()
    _session_note(run, inv)
    failed = [s for s in run.stages if not s["ok"]]
    quota = any(s.get("quota") or "Overloaded" in s.get("error", "") for s in failed)
    # Rejected Shorts are the quality gate working; a run fails when the Shorts it started all broke.
    made_nothing = run.started_here > 0 and not run.spawned and not any(m["outcome"] in ("ready", "rejected") for m in run.made)
    if not failed:
        result = "ok"
    elif (made_nothing and not quota) or len(failed) >= 3:
        result = "failed"
    else:
        result = "degraded"
    write_status(run, result)
    log.info("run %s in %.0f min: made %s, scheduled %s, inventory %s", result, run.minutes(), run.made, run.published, inv)
    return 0 if result != "failed" else 1
