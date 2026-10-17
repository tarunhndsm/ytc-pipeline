"""The last resort that keeps @ankahi.kathayein posting when the factory and the judge haven't made enough new
Reels: one of the page's older Reels goes out again on Instagram and Facebook.

The owner wants 3 to 4 Reels a day "with consistency no matter what" (17 Oct). The hourly reels run books every new
Reel first; then a day with fewer than MIN_PER_DAY Reels on Instagram gets its soonest open slot filled, once that
slot is WITHIN away (a new Reel made meanwhile takes it first), by a Reel that went out REST ago or more and not
again for EVERY, outside the quiz chain and the series (their questions and parts belong to their week), whose
review passed. Never on YouTube, which counts re-uploads as repetitious content.

The videos wait in the STOCK draft release (`python -m ytc.reel_repost stock` on the Mac uploads them) and are hosted
on Cloudinary under names of their own (REPOST_URL), so no Reel takes a repost's Buffer post for its own
(instagram.HOSTED_REEL). LEDGER keeps the reposts; reel_cloud._dues counts their Instagram slots.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import reels

log = logging.getLogger(__name__)

# Off for good: on 17 Oct, after ak001 went out again, the owner asked that every Reel posted be new and unique.
ON = False
MIN_PER_DAY = 3
WITHIN = timedelta(hours=4)
REST = timedelta(days=14)
EVERY = timedelta(days=30)
STOCK = "evergreen"
LEDGER = reels.REELS / "automation" / "reposts.json"
REPOST_URL = re.compile(r"/ankahikathayein/repost-(ak\d+)-(\d{12})\.mp4$")
# A Reel is stocked ahead of its rest: asked at SOMEDAY, only the rules that never lapse can turn it down.
SOMEDAY = datetime(2100, 1, 1, tzinfo=timezone.utc)


def ledger() -> list[dict]:
    return json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else []


def _save(entries: list[dict]) -> None:
    LEDGER.write_text(json.dumps(entries, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def dues() -> list[datetime]:
    """When the reposts go out on Instagram."""
    return [_when(entry["dueAt"]) for entry in ledger() if entry.get("dueAt")]


def why_not(folder: Path, now: datetime, entries: list[dict]) -> str:
    """Why a Reel can't go out again now, or ""."""
    from . import instagram

    record = reels._read(folder / instagram.RECORD)
    if not record or record.get("withdrawn") or not record.get("dueAt"):
        return "it hasn't gone out on Instagram"
    if _when(record["dueAt"]) > now - REST:
        return "it went out too recently"
    spec_path = folder / "short.yaml"
    spec = (yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}) if spec_path.exists() else {}
    if not spec or spec.get("quiz") or spec.get("answers") or spec.get("part"):
        return "it's in the quiz chain or a series"
    verdict = reels._read(folder / reels.REVIEW)
    if not verdict:
        return "it has no review"
    scores = verdict.get("scores") or {}
    if low := [k for k, v in scores.items() if isinstance(v, (int, float)) and v < reels.PASS_SCORE]:
        return f"its review scored {', '.join(low)} under {reels.PASS_SCORE}"
    if any(entry["reel"] == folder.name and _when(entry["dueAt"]) > now - EVERY for entry in entries):
        return "it went out again too recently"
    return ""


@functools.cache
def stocked() -> frozenset[str]:
    """The Reels whose videos wait in STOCK."""
    from . import reel_media

    return frozenset(n.removesuffix(".mp4") for n in reel_media.Store().assets(STOCK) if n.endswith(".mp4"))


def candidates(now: datetime, entries: list[dict], stock: frozenset[str]) -> list[str]:
    """The stocked Reels that may go out again, the least repeated and longest unseen first."""
    from . import instagram

    def key(reel: str) -> tuple[int, datetime]:
        times = [_when(entry["dueAt"]) for entry in entries if entry["reel"] == reel]
        times.append(_when(reels._read(reels.REELS / reel / instagram.RECORD)["dueAt"]))
        return len(times), max(times)

    found = [reel for reel in sorted(stock)
             if (reels.REELS / reel).is_dir() and not why_not(reels.REELS / reel, now, entries)]
    return sorted(found, key=key)


def ready(now: datetime) -> bool:
    """Whether a Reel can go out again, so no coming day goes without Instagram and Facebook posts."""
    return ON and bool(candidates(now, ledger(), stocked()))


def last_booked() -> datetime | None:
    """When the newest Reel was booked (not a repost)."""
    from . import instagram

    booked = [datetime.fromisoformat(record["scheduled_at"]) for path in reels.REELS.glob(f"*/{instagram.RECORD}")
              if (record := reels._read(path)) and record.get("scheduled_at")]
    return max(booked, default=None)


def slot(now: datetime, taken: set[datetime]) -> datetime | None:
    """The soonest open Instagram slot of a day (today or tomorrow) with fewer than MIN_PER_DAY Reels, once it is
    WITHIN away."""
    from . import reel_cloud

    per_day = Counter(when.astimezone(reel_cloud.IST).date() for when in taken)
    for day in reel_cloud._ahead(now, 2):
        if per_day[day] >= MIN_PER_DAY:
            continue
        for hour, mins in reel_cloud.SLOTS:
            when = datetime(day.year, day.month, day.day, hour, mins, tzinfo=reel_cloud.IST)
            if when in taken or when <= now + reel_cloud.LEAD:
                continue
            if when - now <= WITHIN:
                return when
            break
    return None


def _adopt(now: datetime, entries: list[dict]) -> list[str]:
    """Record the reposts Buffer has that the ledger doesn't (a run whose push failed), rather than post again."""
    from . import facebook, instagram

    known = {entry.get("instagram") for entry in entries}
    adopted = []
    for post in facebook.channel_posts(instagram._env("IG_BUFFER_CHANNEL_ID"), now):
        if post["status"] not in instagram.LIVE or post["id"] in known:
            continue
        for asset in post.get("assets") or []:
            if found := REPOST_URL.search(asset.get("source") or ""):
                entries.append({"reel": found.group(1), "dueAt": post["dueAt"], "instagram": post["id"],
                                "media_public_id": f"ankahikathayein/repost-{found.group(1)}-{found.group(2)}",
                                "media_url": asset["source"], "scheduled_at": post["createdAt"], "adopted": True})
                adopted.append(found.group(1))
                break
    return adopted


def fill(now: datetime, done: dict, planned: list[datetime], dry: bool = False) -> None:
    """Fill the soonest slot of a day short of MIN_PER_DAY Reels with a Reel going out again, if one is near."""
    from . import facebook, instagram, publish, reel_cloud, reel_media
    from .spec import ShortSpec

    if not ON:
        return
    taken = {reel_cloud._minute(when) for when in reel_cloud._dues() + planned}
    if not (when := slot(now, taken)):
        return
    entries = ledger()
    if not dry and (adopted := _adopt(now, entries)):
        _save(entries)
        done["reposted"].update({reel: "already in Buffer; recorded" for reel in adopted})
        return
    if not (choices := candidates(now, entries, stocked())):
        done["held"]["older Reels"] = f"none can go out again for {when:%a %d %b %H:%M} IST"
        return
    reel, folder = choices[0], reels.REELS / choices[0]
    if dry:
        planned.append(when)
        done["reposted"][reel] = f"would go out again {when:%a %d %b %H:%M} IST on Instagram and Facebook"
        return
    spec = ShortSpec.load(folder / "short.yaml")
    public_id = f"ankahikathayein/repost-{reel}-{when:%Y%m%d%H%M}"
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / f"{reel}.mp4"
        if not reel_media.Store().get(STOCK, f"{reel}.mp4", video):
            raise RuntimeError(f"the {STOCK} release lost {reel}.mp4")
        media_url = publish.host_video(video, public_id)
    due = when.isoformat()
    result = instagram._mutate("createPost", {"channelId": instagram._env("IG_BUFFER_CHANNEL_ID"),
                                              **instagram._post(folder, spec, media_url, due)})
    if result["__typename"] != "PostActionSuccess":
        publish.unhost_video(public_id)
        raise RuntimeError(f"Buffer rejected {reel} going out again: {result.get('message')}")
    entry = {"reel": reel, "dueAt": result["post"]["dueAt"], "instagram": result["post"]["id"],
             "media_public_id": public_id, "media_url": media_url,
             "scheduled_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    entries.append(entry)
    _save(entries)
    planned.append(when)
    told = f"{when:%a %d %b %H:%M} IST on Instagram"
    if os.environ.get("IG_BUFFER_FACEBOOK_CHANNEL_ID"):
        try:
            sent = instagram._mutate("createPost", {"channelId": instagram._env("IG_BUFFER_FACEBOOK_CHANNEL_ID"),
                                                    **facebook._post(spec, media_url, due)})
            if sent["__typename"] != "PostActionSuccess":
                raise RuntimeError(sent.get("message"))
            entry["facebook"] = sent["post"]["id"]
            _save(entries)
            told += " and Facebook"
        except Exception as err:
            done["errors"].append(f"{reel} goes out again on Instagram, but Facebook refused it: {err}"[:300])
    done["reposted"][reel] = told
    log.info("%s goes out again %s", reel, told)


def unhost(now: datetime, done: dict, dry: bool = False) -> None:
    """Take a repost's video off Cloudinary once its posts are KEEP old, like a Reel's (reel_cloud.unhost_sent)."""
    from . import publish, reel_cloud

    entries, changed = ledger(), False
    for entry in entries:
        if entry.get("unhosted_at") or _when(entry["dueAt"]) > now - reel_cloud.KEEP:
            continue
        if dry:
            done["unhosted"][f"{entry['reel']} (repost)"] = f"would take off {entry['media_public_id']}"
            continue
        try:
            publish.unhost_video(entry["media_public_id"])
        except Exception as err:
            log.warning("taking %s off Cloudinary failed: %s", entry["media_public_id"], err)
            continue
        entry["unhosted_at"], changed = now.isoformat(timespec="seconds"), True
        done["unhosted"][f"{entry['reel']} (repost)"] = entry["media_public_id"]
    if changed:
        _save(entries)


def _stock(store, folder: Path, video: Path) -> bool:
    """Put a Reel's render in STOCK if the Reel may go out again one day and it's the very video its review judged."""
    if why_not(folder, SOMEDAY, []) or f"{folder.name}.mp4" in store.assets(STOCK):
        return False
    judged = (reels._read(folder / reels.REVIEW) or {}).get("video_sha256")
    if judged and judged != reels.sha256(video):
        log.warning("%s: the video isn't the one its review judged; not stocked", folder.name)
        return False
    store.put(STOCK, f"{folder.name}.mp4", video)
    log.info("stocked %s", folder.name)
    return True


def keep(store, folder: Path) -> None:
    """Before reel_host.prune drops a Reel's "media" release, stock its render if the Reel may go out again one day,
    so the cloud's own Reels are stocked without the Mac. `folder` is in the run's clone."""
    if not ON or why_not(folder, SOMEDAY, []) or f"{folder.name}.mp4" in store.assets(STOCK):
        return
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / f"{folder.name}.mp4"
        if store.get(f"media {folder.name}", video.name, video):
            _stock(store, folder, video)


def stock() -> list[str]:
    """From the Mac: stock the local render of each Reel that may go out again one day."""
    from . import reel_media

    store = reel_media.Store()
    return [folder.name for folder in sorted(p for p in reels.REELS.glob("ak*") if p.is_dir())
            if (video := folder / f"{folder.name}.mp4").exists() and _stock(store, folder, video)]


def main() -> None:
    from .cli import PIPELINE_ROOT, _load_env

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    _load_env(PIPELINE_ROOT / ".env")
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "stock":
        print("stocked:", ", ".join(stock()) or "nothing new")
    elif command == "status":
        now = datetime.now(timezone.utc)
        print("stocked:", len(stocked()), "| may go out again now:",
              ", ".join(candidates(now, ledger(), stocked())) or "none")
        for entry in ledger()[-10:]:
            print(" ", entry["reel"], entry["dueAt"], "facebook" if entry.get("facebook") else "")
    else:
        raise SystemExit("usage: python -m ytc.reel_repost [status|stock]")


if __name__ == "__main__":
    main()
