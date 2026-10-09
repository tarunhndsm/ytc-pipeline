"""Mirror @ankahi.kathayein's Instagram Reels to the brand's Facebook Page through Buffer."""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import instagram
from .spec import ShortSpec

log = logging.getLogger(__name__)

RECORD, LIVE_START = "facebook.json", "facebook_live.json"
# Match reel_cloud.IST, reel_cloud.QUEUE and reel_cloud.LEAD; importing reel_cloud loads Modal during backfill.
IST = ZoneInfo("Asia/Kolkata")
QUEUE = 10
LEAD = timedelta(minutes=30)
BACKFILL_SLOTS = ((19, 0), (21, 0))
BACKFILL_DAYS = 14
SOCIAL_LINE = "Instagram/YouTube: @ankahi.kathayein"
# Buffer's Facebook metadata has no AI label (Instagram's has isAiGenerated), so the caption says it instead.
AI_LINE = "कथा सुनाने वाली आवाज़ AI से बनी है।"


def _when(stamp: str | None) -> datetime | None:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None


def _minute(when: datetime) -> datetime:
    """Match reel_cloud._minute without importing the Modal app for backfills."""
    return when.astimezone(IST).replace(second=0, microsecond=0)


def record(folder: Path) -> dict | None:
    path = folder / RECORD
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def caption(spec: ShortSpec) -> str:
    extra = " ".join(spec.hashtags[instagram.MAX_HASHTAGS:])
    ai = AI_LINE if spec.synthetic_media else ""
    return "\n\n".join(part for part in (instagram.caption(spec), extra, ai, SOCIAL_LINE) if part)


def _post(spec: ShortSpec, media_url: str, due: str) -> dict:
    return {"text": caption(spec), "schedulingType": "automatic", "mode": "customScheduled", "dueAt": due,
            "assets": [{"video": {"url": media_url}}], "metadata": {"facebook": {"type": "reel"}}}


def channel_posts(channel: str, now: datetime | None = None) -> list[dict]:
    """Posts near an hourly run, or the full history needed to avoid a second backfill."""
    query = """
    query Posts($input: PostsInput!, $after: String) {
      posts(input: $input, first: 50, after: $after) {
        edges { node {
          id status dueAt createdAt sentAt externalLink assets { source }
          metadata { ... on InstagramPostMetadata { type } ... on FacebookPostMetadata { type } }
        } }
        pageInfo { hasNextPage endCursor }
      }
    }"""
    filters = {"channelIds": [channel]}
    if now is not None:
        filters |= {"startDate": (now - timedelta(days=3)).isoformat(),
                    "endDate": (now + timedelta(days=60)).isoformat()}
    variables = {"input": {"organizationId": instagram._organization(), "filter": filters}, "after": None}
    found = []
    while True:
        page = instagram._buffer(query, variables)["posts"]
        found += [edge["node"] for edge in page["edges"]]
        if not page["pageInfo"]["hasNextPage"]:
            return found
        variables = variables | {"after": page["pageInfo"]["endCursor"]}


def reel_posts(posts: list[dict]) -> dict[str, dict]:
    found = {}
    for post in posts:
        if post["status"] not in instagram.LIVE:
            continue
        for asset in post["assets"] or []:
            if hosted := instagram.HOSTED_REEL.search(asset.get("source") or ""):
                reel = hosted.group(1)
                if reel not in found or post["status"] == "sent" and found[reel]["status"] != "sent":
                    found[reel] = post | {"media_url": asset["source"]}
    return found


def queued(posts: list[dict], root: Path, now: datetime) -> tuple[set[str], set[datetime], Counter]:
    ids = {post["id"] for post in posts if post["status"] in ("scheduled", "sending")}
    today = now.astimezone(IST).date()
    taken: set[datetime] = set()
    per_day: Counter = Counter()
    seen = set()
    for post in posts:
        due = _when(post.get("dueAt"))
        if post["status"] in instagram.LIVE and due and due.astimezone(IST).date() >= today:
            taken.add(_minute(due))
            # Buffer imports the Page's photo, cover and text posts too; they don't take a Reel's place in the day.
            reel = any(instagram.HOSTED_REEL.search(asset.get("source") or "") for asset in post.get("assets") or [])
            if reel and post["id"] not in seen:
                per_day[_minute(due).date()] += 1
                seen.add(post["id"])
    for path in root.glob(f"ak*/{RECORD}"):
        saved = json.loads(path.read_text(encoding="utf-8"))
        due = _when(saved.get("dueAt"))
        if saved.get("status") in instagram.LIVE and due and due.astimezone(IST).date() >= today:
            taken.add(_minute(due))
            if (post_id := saved.get("id") or str(path)) not in seen:
                per_day[_minute(due).date()] += 1
                seen.add(post_id)
        if saved.get("status") in ("scheduled", "sending") and due and due > now:
            ids.add(saved.get("id") or str(path))
    return ids, taken, per_day


def adopt(folder: Path, post: dict) -> dict:
    """Save a Facebook post Buffer already has when a fresh clone lost its record."""
    if record(folder) is not None:
        raise RuntimeError(f"{folder.name} already has a Facebook record")
    hosted = instagram.HOSTED_REEL.search(post["media_url"])
    saved = {k: post[k] for k in ("id", "status", "dueAt")} | {
        "media_url": post["media_url"], "media_public_id": f"ankahikathayein/{folder.name}{hosted.group(2) or ''}",
        "scheduled_at": post["createdAt"], "adopted": True,
    }
    (folder / RECORD).write_text(json.dumps(saved, indent=1) + "\n", encoding="utf-8")
    log.info("recorded %s's Facebook post already in Buffer (%s)", folder.name, post["dueAt"])
    return saved


def schedule(spec_path: Path, when: datetime) -> dict:
    """Schedule the hosted Instagram video on Facebook and save its post in facebook.json."""
    spec_path = spec_path.resolve()
    folder, spec = spec_path.parent, ShortSpec.load(spec_path)
    if record(folder) is not None:
        raise RuntimeError(f"{spec.id} is already on Facebook")
    instagram_record = json.loads((folder / instagram.RECORD).read_text(encoding="utf-8"))
    media_url = instagram_record["media_url"]
    hosted = instagram.HOSTED_REEL.search(media_url)
    if not hosted or hosted.group(1) != spec.id or hosted.group(2):
        raise RuntimeError(f"{spec.id}'s Instagram record does not have its original hosted Reel")
    instagram._still_hosted(spec, media_url)
    payload = {"channelId": instagram._env("IG_BUFFER_FACEBOOK_CHANNEL_ID"),
               **_post(spec, media_url, when.isoformat())}
    result = instagram._mutate("createPost", payload)
    if result["__typename"] != "PostActionSuccess":
        raise RuntimeError(f"Buffer rejected {spec.id} for Facebook: {result.get('message')}")
    saved = result["post"] | {"media_url": media_url, "media_public_id": f"ankahikathayein/{spec.id}",
                               "scheduled_at": datetime.now(timezone.utc).isoformat()}
    (folder / RECORD).write_text(json.dumps(saved, indent=1) + "\n", encoding="utf-8")
    log.info("scheduled %s on Facebook for %s", spec.id, when.isoformat())
    return saved


def live_start(root: Path, now: datetime, dry: bool = False) -> datetime:
    """Keep the first enabled run's time so a full Facebook queue cannot strand a later Instagram Reel."""
    path = root / LIVE_START
    if path.exists():
        return _when(json.loads(path.read_text(encoding="utf-8"))["started_at"])
    if not dry:
        path.write_text(json.dumps({"started_at": now.isoformat()}, indent=1) + "\n", encoding="utf-8")
        log.info("Facebook mirror starts with Instagram Reels due from %s", now.isoformat())
    return now


def auto_candidate(folder: Path, start: datetime) -> datetime | None:
    path = folder / instagram.RECORD
    if not path.exists() or record(folder) is not None:
        return None
    saved = json.loads(path.read_text(encoding="utf-8"))
    due, scheduled = _when(saved.get("dueAt")), _when(saved.get("scheduled_at"))
    return due if due and (due >= start or scheduled and scheduled >= start) else None


def _backfill_slot(now: datetime, taken: set[datetime], per_date: Counter, per_day: int,
                   days: int, slots: tuple[tuple[int, int], ...] = BACKFILL_SLOTS) -> datetime | None:
    first = now.astimezone(IST).date()
    for offset in range(days):
        day = first + timedelta(days=offset)
        if per_date[day] >= per_day:
            continue
        for hour, minute in slots:
            slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)
            if slot > now + LEAD and slot not in taken:
                return slot
    return None


def past_reels(root: Path, now: datetime) -> list[dict]:
    """Reels already out on Instagram (by their records) with no Facebook post yet: what a backfill catches up."""
    local = []
    for path in sorted(root.glob("ak*/instagram.json")):
        folder = path.parent
        if record(folder) is not None or not (folder / "short.yaml").exists():
            continue
        saved = json.loads(path.read_text(encoding="utf-8"))
        due = _when(saved.get("dueAt"))
        if due and due < now and saved.get("media_url"):
            local.append({"folder": folder, "due": due, "published": due})
    return local


def backfill(root: Path, now: datetime | None = None, per_day: int = 2, days: int = BACKFILL_DAYS,
             dry: bool = False, slots: tuple[tuple[int, int], ...] = BACKFILL_SLOTS,
             reserve_within: timedelta | None = None) -> dict:
    """Catch-up for sent public Instagram Reels, by hand or from the hourly run; a dry run only reads local records.
    Queue room stays free for the Instagram Reels the hourly mirror has yet to place: all of them, or with
    `reserve_within` only those due that soon (a full queue of later ones would otherwise hold the catch-up for
    weeks). per_day caps a day's Facebook Reels, counting the mirror's, booked or still to come."""
    if not 1 <= per_day <= len(slots) or days < 1:
        raise ValueError(f"per_day must be 1-{len(slots)} and days must be positive")
    now = now or datetime.now(timezone.utc)
    local = past_reels(root, now)

    result = {"dry_run": dry, "planned": [], "adopted": [], "unverified": [], "held": []}
    posts = []
    if not dry:
        posts = channel_posts(instagram._env("IG_BUFFER_FACEBOOK_CHANNEL_ID"))
        existing = reel_posts(posts)
        sent = reel_posts(channel_posts(instagram._env("IG_BUFFER_CHANNEL_ID")))
        pending = []
        for item in local:
            reel = item["folder"].name
            if post := existing.get(reel):
                adopt(item["folder"], post)
                result["adopted"].append(reel)
            elif (post := sent.get(reel)) and post["status"] == "sent" and post.get("externalLink") \
                    and (post.get("metadata") or {}).get("type") == "reel":
                item["published"] = _when(post.get("sentAt")) or item["due"]
                pending.append(item)
            else:
                result["unverified"].append(reel)
        local = pending
    else:
        result["note"] = "Local due times only; a live run verifies Instagram publication and Buffer's Facebook queue."

    ids, taken, per_date = queued(posts, root, now)
    start = live_start(root, now, dry=True)
    coming = [due for folder in root.glob("ak*") if folder.is_dir() and (due := auto_candidate(folder, start))]
    live = sum(1 for due in coming if reserve_within is None or due <= now + reserve_within)
    for due in coming:
        # The mirror gives these Reels Instagram's time on Facebook only while their day is under its cap.
        if due > now:
            taken.add(_minute(due))
            per_date[_minute(due).date()] += 1
    times = " or ".join(f"{hour:02d}:{minute:02d}" for hour, minute in slots)
    for item in sorted(local, key=lambda row: (row["published"], row["folder"].name)):
        reel = item["folder"].name
        if len(ids) + live >= QUEUE:
            result["held"].append({"reel": reel, "reason": "Facebook queue is full"})
            continue
        if not (slot := _backfill_slot(now, taken, per_date, per_day, days, slots)):
            result["held"].append({"reel": reel, "reason": f"no free {times} IST slot in {days} days"})
            continue
        if dry:
            ids.add(f"planned:{reel}")
        else:
            ids.add(schedule(item["folder"] / "short.yaml", slot)["id"])
        taken.add(slot)
        per_date[slot.date()] += 1
        result["planned"].append({"reel": reel, "instagram_due": item["due"].isoformat(),
                                  "facebook_at": slot.isoformat()})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ytc.facebook", description="The brand's Facebook Reel mirror")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("backfill", help="hand-run catch-up for public Instagram Reels")
    command.add_argument("--dry-run", action="store_true", help="print a local-only plan without calling Buffer")
    command.add_argument("--per-day", type=int, default=2, help="maximum Facebook posts per day (default: 2)")
    command.add_argument("--days", type=int, default=BACKFILL_DAYS, help="days to search for slots (default: 14)")
    args = parser.parse_args()
    from .cli import PIPELINE_ROOT, _load_env
    from .reels import REELS

    _load_env(PIPELINE_ROOT / ".env")
    print(json.dumps(backfill(REELS, per_day=args.per_day, days=args.days, dry=args.dry_run),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
