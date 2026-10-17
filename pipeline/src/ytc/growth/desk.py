"""The desk: runs a brand's agents in turn, keeps what they found and did in the repo, and tells the owner.

The collector and analyst run every time (every few hours on CircleCI); the listener twice a day; the scout and
the strategist once a day, the strategist's run ending with the day's digest on the owner's phone. One agent failing
doesn't stop the others: the report lists what failed.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import logging
import os
import tempfile
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from .. import reels
from . import analyze, brain, collect, scout
from .brands import Brand

log = logging.getLogger(__name__)

THINK_EVERY = timedelta(hours=20)
# The strategist waits for the morning in India, so the day's digest reaches the owner then, unless a day was missed.
THINK_HOURS = range(6, 12)
THINK_LATE = timedelta(hours=30)
SCOUT_EVERY = timedelta(hours=20)
LISTEN_EVERY = timedelta(hours=11)


def _json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data, compact: bool = False) -> None:
    """Write JSON; compact for the big files rewritten every run, which would otherwise swell the repo's history."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact else \
        json.dumps(data, ensure_ascii=False, indent=1)
    path.write_text(text + "\n", encoding="utf-8")


def _due(state: dict, name: str, every: timedelta, now: datetime) -> bool:
    last = collect.when(state.get(name))
    return last is None or now - last >= every


def _youtube_token(brand: Brand) -> Path | None:
    """The YouTube sign-in on disk, written from the environment on a fresh checkout."""
    path = brand.youtube_token_file
    if not path:
        return None
    if not path.exists() and brand.youtube_token and (token := os.environ.get(brand.youtube_token)):
        path.parent.mkdir(exist_ok=True)
        path.write_text(token, encoding="utf-8")
        path.chmod(0o600)
    return path if path.exists() else None


def notify(brand: Brand, message: str, high: bool = False) -> bool:
    """Push to the owner's phone through the studio's ntfy topic."""
    if not (topic := os.environ.get("YTC_NTFY_TOPIC")):
        return False
    request = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"), method="POST", headers={
        "Title": brand.title, "Priority": "high" if high else "default",
        "Tags": "warning" if high else "chart_with_upwards_trend"})
    try:
        urllib.request.urlopen(request, timeout=30).read()
        return True
    except Exception as err:
        log.warning("ntfy push failed: %s", err)
        return False


def _ist(moment: datetime) -> str:
    return moment.astimezone(reels.IST).strftime("%a %d %b %H:%M IST")


def _cell(value) -> str:
    return str(value if value is not None else "–").replace("|", "/").replace("\n", " ")


def _num(value) -> str:
    return f"{value:,.0f}" if isinstance(value, (int, float)) else "–"


def render(brand: Brand, analysis: dict, trends: dict, comments: list[dict], thought: dict | None, errors: list[str],
           state: dict, now: datetime) -> str:
    """The desk's report, rewritten every run."""
    lines = [f"# Growth desk: {brand.name}", "",
             f"Updated {_ist(now)}. {analysis['posts']} posts tracked across {', '.join(brand.channels)}. "
             f"The strategist last ran {_ist(collect.when(state['think'])) if state.get('think') else 'never'}.", ""]
    decisions = (thought or {}).get("decisions") or (state.get("last_decisions") or {})
    if decisions.get("diagnosis"):
        lines += ["## Diagnosis", "", decisions["diagnosis"], ""]
    lines += ["## This week", "", "| Platform | Posts (7 days) | Median index, 7 days | Median index, 14 days before | "
              "Reach gained, 24 h | Reach gained, 7 days |", "|---|---|---|---|---|---|"]
    for platform, t in analysis["trend"].items():
        if platform != "youtube_channel":
            lines.append(f"| {platform} | {t['posts_7d']} | {_cell(t['median_index_7d'])} | "
                         f"{_cell(t['median_index_prev_14d'])} | {_num(t['reach_gained_24h'])} | "
                         f"{_num(t['reach_gained_7d'])} |")
    if channel := analysis["trend"].get("youtube_channel"):
        lines += ["", f"YouTube channel: {channel['subscribers']} subscribers (a week ago "
                      f"{_cell(channel['subscribers_week_ago'])}), {channel['total_views']:,} views in all; "
                      f"{channel['views_last_7_days_reported']:,} views in the last 7 reported days (to "
                      f"{_cell(channel['analytics_through'])}) against {channel['views_7_days_before']:,} the week "
                      f"before; new Shorts are watched {_cell(channel['avg_view_pct_last_14d_shorts'])}% through on "
                      f"average. Traffic: " + ", ".join(f"{k.lower()} {v:.0%}" for k, v in channel["traffic_share"].items())
                      + "."]
    for title, rows in (("Best Reels", analysis["best"]), ("Weakest Reels", analysis["worst"])):
        if rows:
            lines += ["", f"## {title}", "", "| Reel | Score | Title | Instagram | Facebook | YouTube |",
                      "|---|---|---|---|---|---|"]
            for row in rows:
                cells = []
                for platform in ("instagram", "facebook", "youtube"):
                    p = row["platforms"].get(platform)
                    cells.append(f"{p['reach']:,.0f} (×{p['index']})" if p else "–")
                lines.append(f"| {row['reel']} | {row['score']} | {_cell(row['title'])} | " + " | ".join(cells) + " |")
    for field, title in (("what_works", "What works"), ("what_doesnt", "What doesn't"), ("audience", "What viewers say"),
                         ("writer_notes", "Notes the writer reads before every Reel"), ("youtube", "YouTube notes"),
                         ("owner_actions", "For the owner")):
        if decisions.get(field):
            lines += ["", f"## {title}", ""] + [f"- {item}" for item in decisions[field]]
    if decisions.get("experiments"):
        lines += ["", "## Experiments", ""] + [f"- {e['idea']} (how we'll know: {e['measure']})"
                                               for e in decisions["experiments"]]
    if thought:
        applied = thought.get("applied") or {}
        lines += ["", "## What the desk changed this run", ""]
        if applied.get("moved_up"):
            lines.append("- Moved to the front of the backlog: " + "; ".join(applied["moved_up"]))
        if applied.get("new_stories"):
            lines.append("- New stories added to the backlog: " + "; ".join(applied["new_stories"]))
        if applied.get("held"):
            lines.append("- Festival stories held, then first in line: " + "; ".join(applied["held"]))
        if applied.get("turned_down"):
            lines.append(f"- Story proposals the backlog's checks turned down: {applied['turned_down']}")
        if applied.get("writer_notes"):
            lines.append(f"- Writer notes replaced ({applied['writer_notes']})")
        if applied.get("refill_error"):
            lines.append(f"- Adding stories failed: {applied['refill_error']}")
        if not applied:
            lines.append("- Nothing")
        for item in thought.get("review") or []:
            lines.append(f"- Critic: {item}")
        for item in thought.get("dropped") or []:
            lines.append(f"- Dropped by the rules: {item}")
    if trends.get("festivals"):
        lines += ["", "## Festivals ahead", ""] + [f"- {f['date']} {f['festival']} ({f['days_left']} days): {f['stories']}"
                                                   for f in trends["festivals"][:8]]
    if trends.get("niche"):
        lines += ["", "## Most-viewed Shorts in the niche this week", ""] + [
            f"- {v['views']:,} views: {v['title']} ({v['channel']}, {v['published']})" for v in trends["niche"][:10]]
    lines += ["", "## Problems", ""] + ([f"- {p}" for p in analysis["problems"]] or ["- None found"])
    if comments:
        lines += ["", f"YouTube comments read: {len(comments)} (newest {comments[0]['at'][:10]})."]
    if errors:
        lines += ["", "## Agents that failed this run", ""] + [f"- {e}" for e in errors]
    return "\n".join(lines) + "\n"


def _digest(brand: Brand, analysis: dict, thought: dict) -> str:
    parts = []
    for platform, t in analysis["trend"].items():
        if platform != "youtube_channel":
            parts.append(f"{platform}: {t['posts_7d']} posts this week, +{_num(t['reach_gained_7d'])} reach")
    if channel := analysis["trend"].get("youtube_channel"):
        parts.append(f"YouTube: {channel['subscribers']} subscribers")
    if analysis["best"]:
        parts.append(f"Best lately: {analysis['best'][0]['title']}")
    applied = thought.get("applied") or {}
    changed = [f"{len(applied['moved_up'])} stories moved up" if applied.get("moved_up") else "",
               f"{len(applied['new_stories'])} stories added" if applied.get("new_stories") else "",
               f"{applied['writer_notes']} writer notes" if applied.get("writer_notes") else ""]
    if changed := [c for c in changed if c]:
        parts.append("Changed: " + ", ".join(changed))
    if diagnosis := thought["decisions"].get("diagnosis"):
        parts.append(diagnosis[:300])
    if actions := thought["decisions"].get("owner_actions"):
        parts.append("For you: " + "; ".join(actions)[:300])
    return "\n".join(parts)


def run(brand: Brand, dry: bool = False, think: bool | None = None, scout_now: bool = False) -> dict:
    """One run of the brand's desk; a summary with counts only (CircleCI's logs are public)."""
    levers = importlib.import_module(f"{__package__}.{brand.levers}")
    now = collect.utcnow()
    today = now.astimezone(reels.IST).date()
    out = brand.out
    out.mkdir(parents=True, exist_ok=True)
    state = _json(out / "state.json", {})
    store = _json(out / "posts.json", {})
    errors: list[str] = []
    channels = {p: os.environ[e] for p, e in brand.channels.items() if os.environ.get(e)}

    if (key := os.environ.get(brand.buffer_key)) and channels:
        try:
            store = collect.collect(dataclasses.replace(brand, channels=channels), store, key, levers.made_posts(), now)
            errors += [f"collector: {e}" for e in store.get("collect_errors", [])]
        except Exception as err:
            log.exception("collecting from Buffer failed")
            errors.append(f"collector: {err}"[:300])
    else:
        errors.append(f"collector: {brand.buffer_key} or the Buffer channel ids aren't set")

    days = collect.youtube_days(brand.youtube_stats)
    token = _youtube_token(brand)
    comments = _json(out / "comments.json", [])
    if token and days and _due(state, "listened", LISTEN_EVERY, now):
        try:
            titles = {v["id"]: v["title"] for v in days[-1].get("videos", [])}
            comments = collect.youtube_comments(token, days[-1]["channel"]["id"], titles, now - timedelta(days=60))
            state["listened"] = now.isoformat(timespec="minutes")
        except Exception as err:
            log.exception("reading YouTube comments failed")
            errors.append(f"listener: {err}"[:300])

    analysis = analyze.analyse(store, levers.features(), days, now, list(channels), brand.weights)

    trends = _json(out / "trends.json", {})
    if scout_now or _due(state, "scouted", SCOUT_EVERY, now):
        trends = {"at": now.isoformat(timespec="minutes"), "niche": []}
        if token and days and brand.niche:
            try:
                trends["niche"] = scout.niche(token, brand.niche, days[-1]["channel"]["id"], now)
            except Exception as err:
                log.exception("the niche search failed")
                errors.append(f"scout: {err}"[:300])
        trends["niche_figures"] = scout.figures_in([v["title"] for v in trends["niche"]])
        state["scouted"] = now.isoformat(timespec="minutes")
    trends["festivals"] = scout.festivals(today)

    thought = None
    morning = now.astimezone(reels.IST).hour in THINK_HOURS
    if think or (think is None and (_due(state, "think", THINK_EVERY, now) and morning
                                    or _due(state, "think", THINK_LATE, now))):
        try:
            book = levers.playbook()
            thought = brain.decide(brand, analysis, comments, trends, levers.backlog_view(),
                                   book.get("writer_notes", []), today)
            thought["applied"] = levers.apply(thought["decisions"], now, dry, state)
            if not dry:
                state["think"] = now.isoformat(timespec="minutes")
                state["last_decisions"] = thought["decisions"]
        except Exception as err:
            log.exception("the strategist failed")
            errors.append(f"strategist: {err}"[:300])

    report = render(brand, analysis, trends, comments, thought, errors, state, now)
    pushed = []
    if dry:
        target = Path(tempfile.gettempdir()) / f"growth-{brand.key}"
        target.mkdir(exist_ok=True)
        (target / "REPORT.md").write_text(report, encoding="utf-8")
        _write(target / "thought.json", thought or {})
        _write(target / "analysis.json", {k: v for k, v in analysis.items() if k != "rows"})
    else:
        _write(out / "posts.json", store, compact=True)
        _write(out / "comments.json", comments)
        _write(out / "trends.json", trends)
        _write(out / "analysis.json", {k: v for k, v in analysis.items() if k != "rows"}, compact=True)
        (out / "REPORT.md").write_text(report, encoding="utf-8")
        if thought:
            with (out / "decisions.jsonl").open("a", encoding="utf-8") as log_file:
                log_file.write(json.dumps({"at": now.isoformat(timespec="minutes"), **thought}, ensure_ascii=False) + "\n")
            if (alert := thought["decisions"].get("alert")) and state.get("alerted") != f"{today} {alert}":
                if notify(brand, alert, high=True):
                    state["alerted"] = f"{today} {alert}"
                    pushed.append("alert")
            if state.get("digest") != str(today) and notify(brand, _digest(brand, analysis, thought)):
                state["digest"] = str(today)
                pushed.append("digest")
        _write(out / "state.json", state)
    applied = (thought or {}).get("applied") or {}
    return {
        "brand": brand.key, "dry": dry, "posts": analysis["posts"], "problems": len(analysis["problems"]),
        "comments": len(comments), "niche": len(trends.get("niche", [])), "thought": bool(thought),
        "moved_up": len(applied.get("moved_up") or []), "new_stories": len(applied.get("new_stories") or []),
        "writer_notes": applied.get("writer_notes", 0), "dropped": len((thought or {}).get("dropped") or []),
        "pushed": pushed, "errors": len(errors),
    }
