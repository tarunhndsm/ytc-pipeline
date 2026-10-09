"""Reels for the Instagram brand (@ankahi.kathayein), scheduled through the brand's own Buffer account.

The brand has its own Buffer key and limit (IG_BUFFER_API_KEY), its Instagram channel (IG_BUFFER_CHANNEL_ID) and its
YouTube channel, अनकही कथाएँ (IG_BUFFER_YOUTUBE_CHANNEL_ID), where each Reel also goes out as a Short; videos are
hosted on Cloudinary like the Days of Odd Shorts, under their own folder.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import publish
from .spec import ShortSpec

log = logging.getLogger(__name__)

# Instagram cuts a caption at 2,200 characters and, since December 2025, takes at most 5 hashtags a post (the
# Graph API reference still says 30); it shows about 125 characters before "more", so the caption opens on the title.
CAPTION_CHARS, MAX_HASHTAGS = 2200, 5
FOLLOW_LINE = "ऐसी ही अनकही कथाओं के लिए फ़ॉलो करें 🙏"
SUBSCRIBE_LINES = ("ऐसी ही अनकही कथाओं के लिए चैनल Subscribe करें 🙏", "Instagram: @ankahi.kathayein")
RECORD, YOUTUBE_RECORD = "instagram.json", "youtube.json"
# A Reel's own cut for YouTube, under <reel>/youtube/ with the same id: a shorter telling with its own title and
# opening, rendered like a Reel. Shorts the channel's viewers swiped past in the first seconds (1 Oct: 30-48% stayed)
# go out as their cut instead, and a Short taken off the channel goes out again only as one.
CUT = "youtube"
# The hook is on screen from the first frame (captions._hook_event) and the first caption comes with the first
# word; the cover is the frame between, with the hook's question and no subtitle.
HOOK_SHOWN = 0.0
# A Reel's video is hosted as ankahikathayein/<id>.mp4 and its cut's as <id>-yt.mp4, so a post's video says which
# Reel it is.
HOSTED_REEL = re.compile(r"/ankahikathayein/(ak\d+)(-yt)?\.mp4$")
# Buffer posts that still count: one that failed or is only a draft doesn't hold the Reel's place.
LIVE = ("scheduled", "sending", "sent")


def _env(name: str) -> str:
    if value := os.environ.get(name):
        return value
    raise RuntimeError(f"{name} is not set (the brand's own Buffer account)")


def _buffer(query: str, variables: dict | None = None) -> dict:
    response = requests.post(
        publish.BUFFER_API,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {_env('IG_BUFFER_API_KEY')}"},
        timeout=60,
    )
    if response.status_code == 429:
        raise publish.BufferBusy(datetime.now(timezone.utc))
    response.raise_for_status()
    body = response.json()
    if errors := body.get("errors"):
        raise RuntimeError(f"Buffer API error: {errors}")
    return body["data"]


@functools.cache
def _organization() -> str:
    return _buffer("query { account { organizations { id } } }")["account"]["organizations"][0]["id"]


def posts_by_reel(channel: str) -> dict[str, dict]:
    """The Reels Buffer has a post for on a channel, due from three days ago on, each with its post."""
    query = """
    query Posts($input: PostsInput!, $after: String) {
      posts(input: $input, first: 50, after: $after) {
        edges { node { id status dueAt createdAt assets { source } } }
        pageInfo { hasNextPage endCursor }
      }
    }"""
    now = datetime.now(timezone.utc)
    filters = {"channelIds": [channel], "startDate": (now - timedelta(days=3)).isoformat(),
               "endDate": (now + timedelta(days=60)).isoformat()}
    variables = {"input": {"organizationId": _organization(), "filter": filters}, "after": None}
    found: dict[str, dict] = {}
    while True:
        page = _buffer(query, variables)["posts"]
        for post in (edge["node"] for edge in page["edges"]):
            for asset in post["assets"] or []:
                if post["status"] in LIVE and (hosted := HOSTED_REEL.search(asset["source"] or "")):
                    # A cut's post outranks the Short it replaced, which Buffer keeps as sent.
                    if hosted.group(2) or hosted.group(1) not in found:
                        found[hosted.group(1)] = post | {"media_url": asset["source"]}
        if not page["pageInfo"]["hasNextPage"]:
            return found
        variables["after"] = page["pageInfo"]["endCursor"]


def adopt(folder: Path, post: dict, youtube: bool = False) -> dict:
    """Record a post Buffer already has for a Reel that git doesn't: Modal restarts a preempted run on a fresh clone,
    and scheduling that Reel again would post it twice."""
    record = {k: post[k] for k in ("id", "status", "dueAt")} | {"media_url": post["media_url"],
                                                                "scheduled_at": post["createdAt"], "adopted": True}
    if not youtube:
        record["media_public_id"] = f"ankahikathayein/{folder.name}"
    elif HOSTED_REEL.search(post["media_url"]).group(2):
        record |= {"cut": True, "media_public_id": f"ankahikathayein/{folder.name}-yt"}
    (folder / (YOUTUBE_RECORD if youtube else RECORD)).write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    log.info("recorded %s's post already in Buffer for %s (%s)", folder.name, "YouTube" if youtube else "Instagram",
             post["dueAt"])
    return record


def caption(spec: ShortSpec) -> str:
    tags = " ".join(spec.hashtags[:MAX_HASHTAGS])
    body = "\n\n".join(part for part in (spec.title, (spec.description or "").strip(), FOLLOW_LINE, tags) if part)
    return body[:CAPTION_CHARS]


def youtube_text(spec: ShortSpec) -> str:
    """The Short's description: the Reel's own, which names its sources and credits its pictures, then the channel's."""
    tags = " ".join(spec.hashtags[:MAX_HASHTAGS])
    body = "\n\n".join(part for part in ((spec.description or "").strip(), "\n".join(SUBSCRIBE_LINES), tags) if part)
    return publish._youtube_text(body).encode()[:publish.DESCRIPTION_BYTES].decode(errors="ignore")


def cover_ms(spec: ShortSpec, manifest: dict) -> int:
    first = min((word["start"] for word in manifest.get("words", [])), default=1.0)
    shown = HOOK_SHOWN if spec.hook_text else 0.0
    return round(1000 * (shown + max(first - shown, 0.0) / 2))


def _post(folder: Path, spec: ShortSpec, media_url: str, due: str) -> dict:
    """The whole post: Buffer's editPost replaces a post rather than patching it."""
    manifest = json.loads((folder / "work" / "manifest.json").read_text(encoding="utf-8"))
    return {
        "text": caption(spec),
        "schedulingType": "automatic",
        "mode": "customScheduled",
        "dueAt": due,
        "assets": [{"video": {"url": media_url, "metadata": {"thumbnailOffset": cover_ms(spec, manifest)}}}],
        "metadata": {"instagram": {"type": "reel", "shouldShareToFeed": True, "isAiGenerated": spec.synthetic_media}},
    }


def _mutate(name: str, payload: dict) -> dict:
    kind = name[0].upper() + name[1:]
    mutation = f"""
    mutation Change($input: {kind}Input!) {{
      {name}(input: $input) {{
        __typename
        ... on PostActionSuccess {{ post {{ id status dueAt }} }}
        ... on MutationError {{ message }}
      }}
    }}"""
    return _buffer(mutation, {"input": payload})[name]


def update(spec_path: Path) -> dict:
    """Send a scheduled Reel's caption and cover again from its spec, keeping its video and time."""
    spec_path = spec_path.resolve()
    folder, spec = spec_path.parent, ShortSpec.load(spec_path)
    record = json.loads((folder / RECORD).read_text(encoding="utf-8"))
    result = _mutate("editPost", {"id": record["id"], **_post(folder, spec, record["media_url"], record["dueAt"])})
    if result["__typename"] != "PostActionSuccess":
        raise RuntimeError(f"Buffer rejected the edit of {spec.id}: {result.get('message')}")
    log.info("updated %s on Instagram (due %s)", spec.id, result["post"]["dueAt"])
    return result["post"]


def schedule(spec_path: Path, when: datetime, note: dict | None = None) -> dict:
    """Host the rendered Reel and schedule it on the brand's Instagram; the post is recorded in instagram.json,
    with `note`."""
    spec_path = spec_path.resolve()
    folder, spec = spec_path.parent, ShortSpec.load(spec_path)
    if (folder / RECORD).exists():
        raise RuntimeError(f"{spec.id} is already scheduled: {json.loads((folder / RECORD).read_text())}")
    video = folder / f"{spec.id}.mp4"
    public_id = f"ankahikathayein/{spec.id}"
    media_url = publish.host_video(video, public_id)
    payload = {"channelId": _env("IG_BUFFER_CHANNEL_ID"), **_post(folder, spec, media_url, when.isoformat())}
    result = _mutate("createPost", payload)
    if result["__typename"] != "PostActionSuccess":
        publish.unhost_video(public_id)
        raise RuntimeError(f"Buffer rejected {spec.id}: {result.get('message')}")
    record = result["post"] | {"media_public_id": public_id, "media_url": media_url,
                               "scheduled_at": datetime.now(timezone.utc).isoformat()} | (note or {})
    (folder / RECORD).write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    log.info("scheduled %s on Instagram for %s", spec.id, when.isoformat())
    return record


def _short(spec: ShortSpec, media_url: str, due: str) -> dict:
    """The whole YouTube post: Buffer's editPost replaces a post rather than patching it."""
    return {"text": youtube_text(spec), "schedulingType": "automatic", "mode": "customScheduled", "dueAt": due,
            "assets": [{"video": {"url": media_url}}], "metadata": publish._metadata(spec)}


def cut(folder: Path) -> Path | None:
    """The folder of a Reel's YouTube cut once it is rendered."""
    found = folder / CUT
    return found if (found / "short.yaml").exists() and (found / f"{folder.name}.mp4").exists() else None


def youtube_record(folder: Path) -> dict | None:
    record = folder / YOUTUBE_RECORD
    return json.loads(record.read_text(encoding="utf-8")) if record.exists() else None


def youtube_spec(folder: Path) -> ShortSpec:
    """The spec of the Reel's Short as YouTube has it: its cut's once the post is the cut."""
    record = youtube_record(folder) or {}
    return ShortSpec.load(folder / (f"{CUT}/short.yaml" if record.get("cut") else "short.yaml"))


def _host_cut(folder: Path) -> tuple[ShortSpec, str, str]:
    """Host the Reel's YouTube cut: its spec, video URL and public id."""
    found = cut(folder)
    if found is None:
        raise RuntimeError(f"{folder.name} has no rendered YouTube cut")
    public_id = f"ankahikathayein/{folder.name}-yt"
    return ShortSpec.load(found / "short.yaml"), publish.host_video(found / f"{folder.name}.mp4", public_id), public_id


def _still_hosted(spec: ShortSpec, media_url: str) -> None:
    # Buffer fetches the video at the slot; a video no longer hosted would fail there, out of sight.
    head = requests.head(media_url, allow_redirects=True, timeout=60)
    if head.status_code != 200 or not head.headers.get("content-type", "").startswith("video/"):
        raise RuntimeError(f"{spec.id}'s video is no longer hosted ({head.status_code}): {media_url}")


def move_short(folder: Path, when: datetime) -> dict:
    """Move a Reel's scheduled Short on YouTube to `when`, as the same post with the same video; youtube.json
    follows."""
    spec = youtube_spec(folder)
    record = json.loads((folder / YOUTUBE_RECORD).read_text(encoding="utf-8"))
    _still_hosted(spec, record["media_url"])
    result = _mutate("editPost", {"id": record["id"], **_short(spec, record["media_url"], when.isoformat())})
    if result["__typename"] != "PostActionSuccess":
        raise RuntimeError(f"Buffer wouldn't move {spec.id}'s Short: {result.get('message')}")
    record |= {"dueAt": result["post"]["dueAt"], "moved_at": datetime.now(timezone.utc).isoformat()}
    (folder / YOUTUBE_RECORD).write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    log.info("moved %s on YouTube to %s", spec.id, when.isoformat())
    return record


def mirror(spec_path: Path, when: datetime) -> dict:
    """Schedule a Reel Instagram already has as a Short on the brand's YouTube channel: its YouTube cut when it has
    one, else the video Instagram has; the post is recorded in youtube.json. A Short taken off the channel goes out
    again only as a cut, and its record keeps the one it replaces."""
    spec_path = spec_path.resolve()
    folder, spec = spec_path.parent, ShortSpec.load(spec_path)
    old = youtube_record(folder)
    if old and not old.get("withdrawn"):
        raise RuntimeError(f"{spec.id} is already on YouTube: {old}")
    extra = {"replaces": old} if old else {}
    if cut(folder):
        spec, media_url, public_id = _host_cut(folder)
        extra |= {"cut": True, "media_public_id": public_id}
    elif old:
        raise RuntimeError(f"{spec.id} was taken off YouTube and has no cut to send instead")
    else:
        media_url = json.loads((folder / RECORD).read_text(encoding="utf-8"))["media_url"]
        _still_hosted(spec, media_url)
    payload = {"channelId": _env("IG_BUFFER_YOUTUBE_CHANNEL_ID"), **_short(spec, media_url, when.isoformat())}
    result = _mutate("createPost", payload)
    if result["__typename"] != "PostActionSuccess":
        raise RuntimeError(f"Buffer rejected {spec.id} for YouTube: {result.get('message')}")
    record = result["post"] | {"media_url": media_url, "scheduled_at": datetime.now(timezone.utc).isoformat()} | extra
    (folder / YOUTUBE_RECORD).write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    log.info("scheduled %s on YouTube for %s%s", spec.id, when.isoformat(), " (its cut)" if extra.get("cut") else "")
    return record


def send_cut(folder: Path) -> dict:
    """Make a Reel's scheduled Short its YouTube cut, at the same time; youtube.json follows."""
    record = json.loads((folder / YOUTUBE_RECORD).read_text(encoding="utf-8"))
    spec, media_url, public_id = _host_cut(folder)
    result = _mutate("editPost", {"id": record["id"], **_short(spec, media_url, record["dueAt"])})
    if result["__typename"] != "PostActionSuccess":
        raise RuntimeError(f"Buffer wouldn't take {folder.name}'s cut: {result.get('message')}")
    record |= {"dueAt": result["post"]["dueAt"], "media_url": media_url, "media_public_id": public_id, "cut": True,
               "cut_at": datetime.now(timezone.utc).isoformat()}
    (folder / YOUTUBE_RECORD).write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    log.info("%s's Short on YouTube is now its cut (due %s)", folder.name, record["dueAt"])
    return record
