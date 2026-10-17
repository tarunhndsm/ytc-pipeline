"""The collector: what each channel's posts did, what failed, and what is booked next.

Buffer's API returns each sent post's numbers as the platform reports them, refreshed every few hours: Instagram gives
views, reactions, comments, shares and saves; Facebook impressions, reactions, comments, shares and clicks; YouTube
views, reactions and comments. The YouTube channel's own analytics (average view percentage, retention, subscribers
gained) come from the daily snapshots the brand's pipeline already saves, and its comments from the Data API with the
channel's sign-in, read only.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .. import publish
from .brands import Brand

log = logging.getLogger(__name__)

# Days of sent posts to fetch: older Reels still gather views, and the analyst compares whole lives.
DAYS = 75
# The number each platform's reach is read from (Buffer gives Facebook posts impressions, not views).
REACH = {"instagram": "views", "facebook": "impressions", "youtube": "views"}
FIELDS = "id status sentAt dueAt externalLink metricsUpdatedAt text assets { source } metrics { type value }"
QUERY = """query Posts($input: PostsInput!, $after: String) { posts(input: $input, first: 50, after: $after) {
  edges { node { %s } } pageInfo { hasNextPage endCursor } } }"""
# A point of each post's reach every few hours is enough to see how fast it grows.
HISTORY_GAP = timedelta(hours=5)
HISTORY_POINTS = 40
YOUTUBE_ID = re.compile(r"(?:shorts/|watch\?v=|youtu\.be/)([\w-]{11})")


def when(stamp: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None


class Buffer:
    def __init__(self, key: str):
        self.key = key

    def ask(self, query: str, variables: dict | None = None) -> dict:
        for attempt in range(4):
            response = requests.post(publish.BUFFER_API, json={"query": query, "variables": variables or {}},
                                     headers={"Authorization": f"Bearer {self.key}"}, timeout=60)
            if response.status_code == 429 and attempt < 3:
                time.sleep(30 * (attempt + 1))
                continue
            response.raise_for_status()
            body = response.json()
            if body.get("errors"):
                raise RuntimeError(f"Buffer: {body['errors'][0].get('message', body['errors'])}"[:300])
            return body["data"]
        raise RuntimeError("Buffer kept answering 429")

    @functools.cached_property
    def organization(self) -> str:
        return self.ask("query { account { organizations { id } } }")["account"]["organizations"][0]["id"]

    def posts(self, channel: str, status: str, start: datetime, end: datetime) -> list[dict]:
        variables = {"input": {"organizationId": self.organization, "filter": {
            "channelIds": [channel], "status": [status], "startDate": start.isoformat(), "endDate": end.isoformat()}},
            "after": None}
        nodes: list[dict] = []
        while True:
            page = self.ask(QUERY % FIELDS, variables)["posts"]
            nodes += [edge["node"] for edge in page["edges"]]
            if not page["pageInfo"]["hasNextPage"] or len(nodes) >= 1000:
                return nodes
            variables["after"] = page["pageInfo"]["endCursor"]


def _post(node: dict, platform: str, brand: Brand, made: dict[str, str]) -> dict:
    metrics = {}
    for metric in node.get("metrics") or []:
        try:
            metrics[metric["type"]] = float(metric["value"])
        except (TypeError, ValueError, KeyError):
            continue
    sources = [asset.get("source") or "" for asset in node.get("assets") or []]
    reel = made.get(node["id"])
    if reel is None and brand.reel_in_url:
        reel = next((m[1] for s in sources if (m := brand.reel_in_url.search(s))), None)
    link = node.get("externalLink") or ""
    video = m[1] if platform == "youtube" and (m := YOUTUBE_ID.search(link)) else None
    return {
        "id": node["id"], "platform": platform, "status": node.get("status"), "reel": reel,
        "repost": bool(brand.repost_mark) and any(brand.repost_mark in s for s in sources),
        "sent": node.get("sentAt") or node.get("dueAt"), "due": node.get("dueAt"), "link": link, "video": video,
        "metrics": metrics, "metrics_at": node.get("metricsUpdatedAt"), "caption": (node.get("text") or "")[:160],
    }


def collect(brand: Brand, store: dict, key: str, made: dict[str, str], now: datetime) -> dict:
    """Fetch the brand's sent, failed and booked posts on every channel and merge the numbers into `store`, keeping a
    short history of each post's reach."""
    buffer = Buffer(key)
    sent, failed, booked, errors = [], [], [], []
    for platform, channel in brand.channels.items():
        sent += [_post(n, platform, brand, made) for n in buffer.posts(channel, "sent", now - timedelta(days=DAYS), now)]
        for status, start, end, into in (("error", now - timedelta(days=14), now + timedelta(days=1), failed),
                                         ("scheduled", now - timedelta(hours=2), now + timedelta(days=10), booked)):
            try:
                into += [_post(n, platform, brand, made) for n in buffer.posts(channel, status, start, end)]
            except Exception as err:
                errors.append(f"{platform} {status}: {err}"[:200])
    posts = store.setdefault("posts", {})
    stamp = now.isoformat(timespec="minutes")
    for post in sent:
        history = posts.get(post["id"], {}).get("history", [])
        reach = post["metrics"].get(REACH[post["platform"]])
        if reach is not None:
            last = when(history[-1][0]) if history else None
            if last and now - last < HISTORY_GAP and len(history) > 1:
                history[-1] = [stamp, reach]
            elif not history or history[-1][1] != reach or (last and now - last >= HISTORY_GAP):
                history.append([stamp, reach])
        posts[post["id"]] = post | {"history": history[-HISTORY_POINTS:]}
    store.update(failed=failed, booked=booked, collected=stamp, collect_errors=errors)
    return store


def youtube_days(folder: Path | None, count: int = 28) -> list[dict]:
    """The brand's daily YouTube analytics snapshots, oldest first, without the days whose reading failed."""
    days = []
    for path in sorted(folder.glob("*.json"))[-count:] if folder and folder.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "channel" in data:
            days.append(data)
    return days


def youtube_comments(token_file: Path, channel_id: str, titles: dict[str, str], since: datetime,
                     limit: int = 300) -> list[dict]:
    """Viewers' comments on the channel's videos since `since`, newest first (the channel's own replies left out)."""
    from googleapiclient.discovery import build

    from .. import youtube

    data = build("youtube", "v3", credentials=youtube._credentials(token_file), cache_discovery=False)
    comments: list[dict] = []
    page = None
    while len(comments) < limit:
        result = data.commentThreads().list(part="snippet", allThreadsRelatedToChannelId=channel_id, maxResults=100,
                                            order="time", textFormat="plainText",
                                            **({"pageToken": page} if page else {})).execute()
        for thread in result.get("items", []):
            top = thread["snippet"]["topLevelComment"]["snippet"]
            if (top.get("authorChannelId") or {}).get("value") == channel_id:
                continue
            at = when(top.get("publishedAt"))
            if at and at < since:
                return comments
            video = thread["snippet"].get("videoId")
            comments.append({"video": video, "title": titles.get(video, ""), "text": top.get("textDisplay", "")[:400],
                             "likes": top.get("likeCount", 0), "replies": thread["snippet"].get("totalReplyCount", 0),
                             "at": top.get("publishedAt")})
        page = result.get("nextPageToken")
        if not page:
            break
    return comments


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
