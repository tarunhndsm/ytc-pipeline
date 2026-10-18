"""The analyst: an age-fair score for every post, what the best and the worst stories share, how each platform is
trending, and what has gone wrong. Plain statistics, no model: the strategist reasons over these numbers.

A post's index is its reach over what a post of its age usually gets on its platform, from a line fitted through
log(reach) against log(age) over the brand's own posts: 1 is typical, 3 is three times that. Reels gather most of
their views in the first days, so raw views would favour old posts and hide a new one doing well. The age is the
post's age when its reach was last read, since the collector stops reading posts after two weeks.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from .. import reels
from .collect import REACH, when

# Younger posts are too young to judge.
MIN_AGE_H = 20
# Fewer posts than this on a platform and the index is reach over the platform's median, ignoring age.
FIT_MIN = 6
GROUP_MIN = 3


def _fit(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least squares y = a + b·x, with the slope kept in a sane range (reach never shrinks with age)."""
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / sxx if sxx else 0.0
    slope = max(0.0, min(slope, 1.5))
    return mean_y - slope * mean_x, slope


def _at(history: list[list], moment: datetime) -> float | None:
    """A post's reach at `moment`, read off its history by straight lines between points, or None before it."""
    points = [(when(t), v) for t, v in history if when(t)]
    if not points or moment < points[0][0]:
        return None
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= moment <= t1:
            span = (t1 - t0).total_seconds()
            return v0 + (v1 - v0) * ((moment - t0).total_seconds() / span if span else 1)
    return points[-1][1]


def _slot(sent: datetime) -> str:
    local = sent.astimezone(reels.IST)
    minutes = round((local.hour * 60 + local.minute) / 30) * 30
    return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"


def score(posts: dict, now: datetime) -> list[dict]:
    """Each sent post with its age, reach, engagement, reach gained in the last day, and index."""
    rows = []
    for post in posts.values():
        sent = when(post.get("sent"))
        reach = post.get("metrics", {}).get(REACH.get(post.get("platform"), ""))
        if not sent or reach is None:
            continue
        metrics = post["metrics"]
        engaged = sum(metrics.get(k, 0) for k in ("reactions", "comments", "shares", "saves"))
        day_ago = _at(post.get("history", []), now - timedelta(hours=24))
        read = min(when(post.get("seen")) or when((post.get("history") or [[None]])[-1][0]) or now, now)
        rows.append({
            "id": post["id"], "platform": post["platform"], "reel": post.get("reel"), "repost": post.get("repost"),
            "video": post.get("video"), "sent": post["sent"], "slot": _slot(sent),
            "weekday": sent.astimezone(reels.IST).strftime("%a"), "age_h": round((now - sent).total_seconds() / 3600, 1),
            "read_h": round((read - sent).total_seconds() / 3600, 1),
            "reach": reach, "engaged": engaged, "shares": metrics.get("shares", 0), "saves": metrics.get("saves", 0),
            "comments": metrics.get("comments", 0), "rate": round(engaged / reach, 4) if reach else 0.0,
            "gained_24h": round(reach - day_ago) if day_ago is not None else None,
        })
    groups = defaultdict(list)
    for row in rows:
        if row["read_h"] >= MIN_AGE_H and not row["repost"]:
            groups[row["platform"]].append(row)
    for group in groups.values():
        points = [(math.log1p(r["read_h"]), math.log1p(r["reach"])) for r in group]
        a, b = _fit(points) if len(points) >= FIT_MIN else (statistics.median(y for _, y in points), 0.0)
        for row, (x, y) in zip(group, points):
            row["index"] = round(math.exp(y - (a + b * x)), 2)
    return rows


def _youtube_extras(rows: list[dict], days: list[dict]) -> None:
    """Add the YouTube analytics of each YouTube post's video: average view percentage, engaged share and
    subscribers gained."""
    if not days:
        return
    videos = {v["id"]: v for v in days[-1].get("videos", [])}
    for row in rows:
        if row["platform"] == "youtube" and (video := videos.get(row.get("video") or "")):
            stats = video.get("analytics") or {}
            row["view_pct"] = round(stats["averageViewPercentage"], 1) if "averageViewPercentage" in stats else None
            row["engaged_share"] = stats.get("engaged_share")
            row["subs"] = stats.get("subscribersGained")


def _values(row: dict, feats: dict) -> dict[str, list[str]]:
    """The feature values a post is grouped by."""
    f = feats.get(row.get("reel") or "", {})
    values = {"slot": [row["slot"]], "weekday": [row["weekday"]]}
    if f:
        values |= {
            "scripture": [f["scripture"]],
            "figure": f["figures"][:3],
            "famous figure": ["yes" if f["famous"] else "no"],
            "question in title or hook": ["yes" if f["question"] else "no"],
            "ends on a quiz": ["yes" if f["quiz"] else "no"],
            "answers last quiz": ["yes" if f["answers"] else "no"],
            "part of a series": ["yes" if f["series"] else "no"],
            "hook length": ["up to 8 words" if len(f["hook"].split()) <= 8 else "over 8 words"],
        }
    return values


def compare(rows: list[dict], feats: dict) -> dict:
    """For each platform and feature, the median index of the posts with each value (groups of GROUP_MIN or more)."""
    table: dict = {}
    for platform in sorted({r["platform"] for r in rows}):
        groups: dict = defaultdict(lambda: defaultdict(list))
        for row in rows:
            if row["platform"] == platform and "index" in row:
                for feature, values in _values(row, feats).items():
                    for value in values:
                        groups[feature][value].append(row["index"])
        table[platform] = {
            feature: {value: {"n": len(v), "median": round(statistics.median(v), 2)}
                      for value, v in sorted(by.items(), key=lambda kv: -statistics.median(kv[1])) if len(v) >= GROUP_MIN}
            for feature, by in groups.items()
        }
    return table


def ranked(rows: list[dict], feats: dict, weights: dict[str, float]) -> list[dict]:
    """Every Reel with an index on at least one platform, best first: the geometric mean of its platforms' indexes,
    weighted by how much each platform matters to the brand."""
    by_reel = defaultdict(dict)
    for row in rows:
        if row.get("reel") and "index" in row and not row["repost"]:
            by_reel[row["reel"]][row["platform"]] = row
    out = []
    for reel, platforms in by_reel.items():
        weight = {p: weights.get(p, 1.0) for p in platforms}
        mean = sum(weight[p] * math.log(max(r["index"], 0.01)) for p, r in platforms.items()) / sum(weight.values())
        f = feats.get(reel, {})
        out.append({
            "reel": reel, "score": round(math.exp(mean), 2),
            "title": f.get("title", ""), "hook": f.get("hook", ""), "story": f.get("story", ""),
            "scripture": f.get("scripture", ""), "figures": f.get("figures", []), "quiz": f.get("quiz"),
            "series": f.get("series"), "judge": f.get("judge"),
            "platforms": {p: {k: r.get(k) for k in ("index", "reach", "age_h", "slot", "shares", "saves", "comments",
                                                    "view_pct", "subs") if r.get(k) is not None}
                          for p, r in platforms.items()},
        })
    return sorted(out, key=lambda r: -r["score"])


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def trend(rows: list[dict], posts: dict, days: list[dict], now: datetime) -> dict:
    """Per platform: posts and their median index this week against the two weeks before, reach gained over the last
    day and week; for YouTube also the channel's views, subscribers and watch share from its analytics."""
    out = {}
    for platform in sorted({r["platform"] for r in rows}):
        group = [r for r in rows if r["platform"] == platform and not r["repost"]]
        week = [r for r in group if r["age_h"] <= 168]
        before = [r for r in group if 168 < r["age_h"] <= 504]
        gained_week = 0.0
        for row in rows:
            if row["platform"] == platform:
                then = _at(posts[row["id"]].get("history", []), now - timedelta(days=7))
                if then is not None:
                    gained_week += row["reach"] - then
                elif row["age_h"] <= 168:
                    gained_week += row["reach"]
        out[platform] = {
            "posts_7d": len(week), "posts_prev_14d": len(before),
            "median_index_7d": _median([r["index"] for r in week if "index" in r]),
            "median_index_prev_14d": _median([r["index"] for r in before if "index" in r]),
            "reach_gained_24h": round(sum(g)) if (g := [r["gained_24h"] for r in rows if r["platform"] == platform
                                                        and r["gained_24h"] is not None]) else None,
            "reach_gained_7d": round(gained_week),
            "reposts_7d": sum(1 for r in rows if r["platform"] == platform and r["repost"] and r["age_h"] <= 168),
        }
    if days:
        latest = days[-1]
        daily = latest.get("daily", [])
        week, before = daily[-7:], daily[-14:-7]
        subs_then = next((d["channel"]["subscribers"] for d in reversed(days[:-1])
                          if when(d["date"] + "T00:00:00+00:00") <= now - timedelta(days=7)), None)
        recent = [v for v in latest.get("videos", []) if (when(v.get("published")) or now) > now - timedelta(days=14)]
        traffic = {t["insightTrafficSourceType"]: t["views"] for t in latest.get("traffic_sources", []) if t["views"]}
        total = sum(traffic.values()) or 1
        out["youtube_channel"] = {
            "subscribers": latest["channel"]["subscribers"], "total_views": latest["channel"]["views"],
            "subscribers_week_ago": subs_then,
            "views_last_7_days_reported": sum(d.get("views", 0) for d in week),
            "views_7_days_before": sum(d.get("views", 0) for d in before),
            "subscribers_gained_7d": sum(d.get("subscribersGained", 0) for d in week),
            "analytics_through": week[-1]["day"] if week else None,
            "avg_view_pct_last_14d_shorts": _median([v["analytics"]["averageViewPercentage"] for v in recent
                                                      if (v.get("analytics") or {}).get("averageViewPercentage")]),
            "traffic_share": {k: round(v / total, 3) for k, v in sorted(traffic.items(), key=lambda kv: -kv[1])[:6]},
        }
    return out


def problems(rows: list[dict], store: dict, now: datetime, channels: list[str]) -> list[str]:
    """What has gone wrong or is about to: days without a post, nothing booked ahead, failed posts, recent posts far
    below the usual, numbers Buffer stopped refreshing, and Buffer requests running out before the key's window
    resets. The pipeline's records ("booked": each post due from four days ago on) cover days the collector hasn't
    read yet."""
    found = []
    today = now.astimezone(reels.IST).date()
    for platform in channels:
        due_days = {when(p.get("due")).astimezone(reels.IST).date() for p in store.get("booked", [])
                    if p["platform"] == platform and when(p.get("due"))}
        sent_days = {when(r["sent"]).astimezone(reels.IST).date() for r in rows if r["platform"] == platform}
        sent_days |= {d for d in due_days if d < today}
        missing = [d for d in (today - timedelta(days=k) for k in (1, 2, 3)) if d not in sent_days]
        if missing:
            found.append(f"{platform}: no post on {', '.join(d.strftime('%a %d %b') for d in sorted(missing))}")
        covered = sent_days | due_days
        empty = [d for d in (today + timedelta(days=k) for k in (0, 1, 2)) if d not in covered]
        if empty:
            found.append(f"{platform}: nothing posted or booked for {', '.join(d.strftime('%a %d %b') for d in empty)}")
        recent = sorted((r for r in rows if r["platform"] == platform and "index" in r), key=lambda r: r["sent"])[-5:]
        if len(recent) == 5 and statistics.median(r["index"] for r in recent) < 0.5:
            found.append(f"{platform}: the last 5 posts reach under half of what posts their age usually do "
                         f"(median index {statistics.median(r['index'] for r in recent):.2f})")
    if failed := store.get("failed"):
        found.append(f"{len(failed)} post(s) recorded as failed to publish: "
                     + ", ".join(sorted({f"{p['platform']} {p.get('reel') or p['id'][:8]}" for p in failed})[:6]))
    read = when(store.get("collected")) or now
    stale = [p for p in store.get("posts", {}).values() if (when(p.get("sent")) or read) > read - timedelta(days=3)
             and (when(p.get("metrics_at")) or read - timedelta(days=9)) < read - timedelta(days=2)]
    if stale:
        found.append(f"Buffer hasn't refreshed the numbers of {len(stale)} recent post(s) for over two days")
    budget = store.get("buffer") or {}
    at, resets, pace = when(budget.get("at")), when(budget.get("resets")), budget.get("pace_per_day")
    if at and resets and pace and budget.get("left") is not None:
        runs_out = at + timedelta(days=budget["left"] / pace)
        if runs_out < resets:
            found.append(f"At about {pace} requests a day the Reel pipeline runs out of Buffer requests on "
                         f"{runs_out.astimezone(reels.IST):%a %d %b}, before the key's 30-day window resets on "
                         f"{resets.astimezone(reels.IST):%a %d %b}: nothing new can be booked in between")
    return found


def analyse(store: dict, feats: dict, days: list[dict], now: datetime, channels: list[str],
            weights: dict[str, float]) -> dict:
    rows = score(store.get("posts", {}), now)
    _youtube_extras(rows, days)
    best = ranked(rows, feats, weights)
    young = sorted((r for r in rows if r["age_h"] < MIN_AGE_H and not r["repost"]), key=lambda r: r["sent"])
    return {
        "at": now.isoformat(timespec="minutes"),
        "posts": len(rows),
        "trend": trend(rows, store.get("posts", {}), days, now),
        "features": compare(rows, feats),
        "best": best[:8],
        "worst": best[-6:][::-1] if len(best) > 8 else [],
        "newest": [{"reel": r["reel"], "platform": r["platform"], "age_h": r["age_h"], "reach": r["reach"],
                    "title": feats.get(r["reel"] or "", {}).get("title", "")} for r in young[-9:]],
        "problems": problems(rows, store, now, channels),
        "buffer": store.get("buffer") or {},
        "numbers_read": store.get("collected"),
        "rows": rows,
    }
