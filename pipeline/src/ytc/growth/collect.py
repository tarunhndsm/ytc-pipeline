"""The collector: what each channel's posts did.

Buffer's API returns each sent post's numbers as the platform reports them, refreshed every few hours: Instagram gives
views, reactions, comments, shares and saves; Facebook impressions, reactions, comments, shares and clicks; YouTube
views, reactions and comments. The YouTube channel's own analytics (average view percentage, retention, subscribers
gained) come from the daily snapshots the brand's pipeline already saves, and its comments from the Data API with the
channel's sign-in, read only.

The brand's Buffer key is the one its Reel pipeline books with, and Buffer limits the key's requests (3,000 in 30 days
on the free plan), so the desk reads only what the pipeline can spare: see Buffer.spare. What is booked and what
failed come from the pipeline's own records, which cost no request.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .. import publish
from .brands import Brand

log = logging.getLogger(__name__)

# Days of sent posts to read: a Reel's numbers barely move after two weeks, and an older post keeps the numbers last
# read. Each page of posts is one of the key's requests.
DAYS = 14
# Buffer's windows by their length in seconds, and how many requests the desk leaves in each beyond what the Reel
# pipeline needs until the window resets.
MONTH, DAY = 2592000, 86400
SPARE = {MONTH: 150, DAY: 25}
WINDOWS = {MONTH: "30-day", DAY: "daily"}
# The Reel pipeline's requests an hour until two of the desk's readings measure it: about five reads every hourly run.
PACE = 5.0
POLICY = re.compile(r'"([^"]+)";\s*q=\d+;\s*w=(\d+)')
LEFT = re.compile(r'"([^"]+)";\s*r=(\d+);\s*t=(\d+)')
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


class Short(Exception):
    """What is left of the Buffer key's requests is the Reel pipeline's."""


def limits(headers) -> dict[int, tuple[int, int]]:
    """Requests left and seconds until reset in each of the key's windows, by window length: Buffer sends a RateLimit
    and a RateLimit-Policy header for each window, which requests joins with commas."""
    windows = dict(POLICY.findall(headers.get("ratelimit-policy", "")))
    return {int(windows[name]): (int(left), int(reset))
            for name, left, reset in LEFT.findall(headers.get("ratelimit", "")) if name in windows}


class Buffer:
    def __init__(self, key: str, organization: str | None = None, last: dict | None = None):
        """`last` is the reading kept from the desk's previous read (reading()), which measures the pipeline's pace."""
        self.key = key
        self.org = organization
        self.last = last or {}
        self.left: dict[int, tuple[int, int]] = {}
        self.used = 0
        self.pace = PACE

    def ask(self, query: str, variables: dict | None = None) -> dict:
        if self.left:
            self.spare()
        response = requests.post(publish.BUFFER_API, json={"query": query, "variables": variables or {}},
                                 headers={"Authorization": f"Bearer {self.key}"}, timeout=60)
        self.used += 1
        self.left = limits(response.headers) or self.left
        if response.status_code == 429:
            wait = response.headers.get("retry-after", "?")
            raise Short(f"Buffer refused a request: the key's limit is reached for {wait} more seconds")
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            raise RuntimeError(f"Buffer: {body['errors'][0].get('message', body['errors'])}"[:300])
        return body["data"]

    def spare(self) -> None:
        """Raise Short unless each window has what the Reel pipeline needs until it resets, at the pace it has used the
        key since the last reading, and SPARE more."""
        month, then = self.left.get(MONTH), when(self.last.get("at"))
        now = utcnow()
        if month and then and self.last.get("left") is not None and now - then > timedelta(hours=1):
            others = self.last["left"] - month[0] - self.used
            # A window that reset in between leaves more than before, which says nothing of the pace.
            if others >= 0:
                self.pace = others / ((now - then).total_seconds() / 3600)
        for window, margin in SPARE.items():
            if window not in self.left:
                continue
            left, reset = self.left[window]
            if left < (need := self.pace * reset / 3600 + margin):
                raise Short(f"Buffer's {WINDOWS[window]} window has {left} requests left for {reset / 3600:.0f} more "
                            f"hours, and the Reel pipeline needs about {need - margin:.0f} of them at "
                            f"{self.pace * 24:.0f} a day, so the desk stopped reading")

    def reading(self) -> dict:
        """What is left of the 30-day window and when it resets, for the next read's pace and the report."""
        if MONTH not in self.left:
            return {}
        now = utcnow()
        left, reset = self.left[MONTH]
        return {"at": now.isoformat(timespec="minutes"), "left": left, "pace_per_day": round(self.pace * 24),
                "resets": (now + timedelta(seconds=reset)).isoformat(timespec="minutes"),
                "day_left": self.left.get(DAY, (None,))[0]}

    @property
    def organization(self) -> str:
        if not self.org:
            self.org = self.ask("query { account { organizations { id } } }")["account"]["organizations"][0]["id"]
        return self.org

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
    """Read the posts each channel sent in the last DAYS days while the key has requests to spare, and merge their
    numbers into `store`, keeping a short history of each post's reach."""
    buffer = Buffer(key, store.get("organization"), store.get("buffer"))
    sent, errors = [], []
    try:
        for platform, channel in brand.channels.items():
            sent += [_post(n, platform, brand, made)
                     for n in buffer.posts(channel, "sent", now - timedelta(days=DAYS), now)]
    except Short as err:
        errors.append(str(err))
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
        posts[post["id"]] = post | {"history": history[-HISTORY_POINTS:], "seen": stamp}
    store.update(organization=buffer.org, read_at=stamp, collect_errors=errors)
    if reading := buffer.reading():
        store["buffer"] = reading
    if not errors:
        store["collected"] = stamp
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
