"""Shoutouts for the viewers who answer a quiz Reel's question right (reel_writer's quiz from Reel to Reel): their
names go into the Reel that answers it, on each channel, while that post is still to go out.

YouTube's come from the comments on the asking Short, each reels run (youtube_round), through the brand channel's
sign-in. Buffer's API has no comments and the pipeline has no Meta token, so Instagram's and Facebook's are read in a
session, in the brand's open Instagram tab, and passed in as names or as comments to judge:

    cd pipeline && PYTHONPATH=src .venv/bin/python -m ytc.shoutouts ak044 --instagram @asha.r @dev_k
    cd pipeline && PYTHONPATH=src .venv/bin/python -m ytc.shoutouts ak044 --comments instagram comments.json

comments.json is a list of {"name", "text", "at"} (`at` orders the names: the first right answers win).
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import facebook, instagram, llm, reel_pictures, reel_writer, reels
from .spec import ShortSpec

log = logging.getLogger(__name__)

CHANNELS = ("instagram", "youtube", "facebook")
RECORDS = {"instagram": instagram.RECORD, "youtube": instagram.YOUTUBE_RECORD, "facebook": facebook.RECORD}
# The first right answers, in the order they came: what a caption has room for.
MAX_NAMES = 12
# Buffer may already be sending a post this close to its time.
EDIT_LEAD = timedelta(minutes=15)
# In the answering Reel's folder: each comment judged once, by channel and id.
JUDGED = "shoutouts.json"
JUDGE_MODELS = (*llm.FLASH_LITE, *reel_writer.WRITERS)
SCHEMA = {"type": "object", "properties": {"right": {"type": "array", "items": {"type": "string"}}},
          "required": ["right"]}
PROMPT = """A Hindi page of scripture stories asked its viewers this question at the end of a Reel, and they answered in the comments.

Question: {question}
Right answer: {answer}
Spellings of it: {accepted}
The source says: {evidence}

Which comments give the right answer? The same person, place, thing or number counts in any script, spelling or language. A comment that names two or more different answers, or only asks, praises or greets, does not count.

Comments, one per line as "id: text":
{comments}

Return the ids of the comments that give the right answer."""


def _due(record: dict) -> datetime | None:
    return datetime.fromisoformat(record["dueAt"].replace("Z", "+00:00")) if record.get("dueAt") else None


def post_state(folder: Path, channel: str, now: datetime) -> str:
    """"open" while the Reel's post on `channel` can still be edited, "unscheduled" before it exists, else "out"."""
    record = reels._read(folder / RECORDS[channel])
    if not record:
        return "unscheduled"
    if record.get("withdrawn") or record.get("status") in ("sent", "error") or not (due := _due(record)):
        return "out"
    return "open" if due > now + EDIT_LEAD else "out"


def _specs(folder: Path) -> list[Path]:
    """The Reel's spec and its YouTube cut's, which the Short's description comes from once the post is the cut."""
    return [path for path in (folder / "short.yaml", folder / instagram.CUT / "short.yaml") if path.exists()]


def _edit(folder: Path, channel: str) -> None:
    if channel == "instagram":
        instagram.update(folder / "short.yaml")
    elif channel == "youtube":
        instagram.update_short(folder)
    else:
        facebook.update(folder)


def put(folder: Path, channel: str, names: list[str], now: datetime | None = None) -> bool:
    """Make `names` (the first MAX_NAMES, once each) the right answerers on `channel` in the Reel's specs, and send
    its post again when it is scheduled. False when they are there already or the post is out; a post Buffer won't
    edit leaves the specs as they were."""
    now = now or datetime.now(timezone.utc)
    names = list(dict.fromkeys(name.strip() for name in names if name.strip()))[:MAX_NAMES]
    if (state := post_state(folder, channel, now)) == "out":
        log.info("%s is already out on %s; its shoutouts stay as they are", folder.name, channel)
        return False
    before = {path: path.read_text(encoding="utf-8") for path in _specs(folder)}
    changed = False
    for path, text in before.items():
        spec = yaml.safe_load(text) or {}
        shoutouts = {key: value for key, value in (spec.get("shoutouts") or {}).items() if key != channel}
        if names:
            shoutouts[channel] = names
        if shoutouts == (spec.get("shoutouts") or {}):
            continue
        if shoutouts:
            spec["shoutouts"] = shoutouts
        else:
            spec.pop("shoutouts", None)
        ShortSpec.model_validate(spec)
        path.write_text(yaml.dump(spec, Dumper=reel_pictures._Dumper, allow_unicode=True, sort_keys=False,
                                  width=100000), encoding="utf-8")
        changed = True
    if changed and state == "open":
        try:
            _edit(folder, channel)
        except Exception:
            for path, text in before.items():
                path.write_text(text, encoding="utf-8")
            raise
    return changed


def judge(quiz: dict, comments: list[dict]) -> set[str]:
    """The ids of the comments that give the quiz's right answer, as Gemini reads them."""
    if not comments:
        return set()
    lines = "\n".join(f"{c['id']}: {' '.join(c['text'].split())[:300]}" for c in comments)
    prompt = PROMPT.format(question=quiz["question"], answer=quiz["answer"],
                           accepted=", ".join(quiz.get("accepted") or []) or quiz["answer"],
                           evidence=quiz.get("evidence") or "(not given)", comments=lines)
    answer = llm.generate(prompt, schema=SCHEMA, models=JUDGE_MODELS, purpose="shoutouts")
    return set(answer.get("right") or []) & {c["id"] for c in comments}


def right_names(folder: Path, channel: str, quiz: dict, comments: list[dict]) -> list[str]:
    """The names of the right answers on `channel` so far, first answer first: new comments judged, then kept in
    JUDGED with the earlier verdicts."""
    path = folder / JUDGED
    judged = reels._read(path) or {}
    seen = judged.setdefault(channel, {})
    if new := [c for c in comments if c["id"] not in seen]:
        right = judge(quiz, new)
        for c in new:
            seen[c["id"]] = {"name": c["name"], "at": c.get("at", ""), "right": c["id"] in right}
        path.write_text(json.dumps(judged, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return [verdict["name"] for verdict in sorted(seen.values(), key=lambda v: v["at"]) if verdict["right"]]


def asked_quiz(folder: Path) -> tuple[Path, dict] | None:
    """The Reel whose question this one answers, and that question (its spec's quiz)."""
    spec = ShortSpec.load(folder / "short.yaml")
    if not spec.answers:
        return None
    asked = reels.REELS / spec.answers.reel
    quiz = ShortSpec.load(asked / "short.yaml").quiz if (asked / "short.yaml").exists() else None
    return (asked, quiz.model_dump()) if quiz else None


def youtube_comments(asked: Path, token: Path) -> list[dict]:
    """The comments on the asking Reel's Short, the channel's own left out: id, name (the @handle), text and time;
    none before the Short is out."""
    from googleapiclient.discovery import build

    from . import publish, youtube

    data = build("youtube", "v3", credentials=youtube._credentials(token), cache_discovery=False)
    channel = data.channels().list(part="contentDetails", mine=True).execute()["items"][0]
    uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
    title = publish.youtube_title(instagram.youtube_spec(asked))
    items = data.playlistItems().list(part="snippet", playlistId=uploads, maxResults=50).execute().get("items", [])
    video = next((i["snippet"]["resourceId"]["videoId"] for i in items if i["snippet"]["title"] == title), None)
    if not video:
        return []
    found = []
    for thread in youtube._pages(data.commentThreads().list, part="snippet", videoId=video, maxResults=100,
                                 textFormat="plainText"):
        top = thread["snippet"]["topLevelComment"]["snippet"]
        if (top.get("authorChannelId") or {}).get("value") != channel["id"]:
            found.append({"id": thread["id"], "name": top["authorDisplayName"], "text": top["textOriginal"],
                          "at": top["publishedAt"]})
    return found


def links(asked: Path) -> dict[str, str]:
    """Where the asking Reel's posts are once out, by channel: Buffer's link to each ("" before)."""
    query = "query Post($input: PostInput!) { post(input: $input) { externalLink } }"
    found = {}
    for channel, name in RECORDS.items():
        if (record := reels._read(asked / name)) and record.get("id"):
            post = instagram._buffer(query, {"input": {"id": record["id"]}}).get("post") or {}
            found[channel] = post.get("externalLink") or ""
    return found


def youtube_round(now: datetime, done: dict, token: Path) -> None:
    """Each reels run: the right answers so far on each asking Short go into the Short that answers it, while that
    one is scheduled."""
    for spec_path in sorted(reels.REELS.glob("ak*/short.yaml")):
        folder = spec_path.parent
        if post_state(folder, "youtube", now) != "open":
            continue
        try:
            if not (found := asked_quiz(folder)):
                continue
            asked, quiz = found
            names = right_names(folder, "youtube", quiz, youtube_comments(asked, token))
            if put(folder, "youtube", names, now):
                done.setdefault("shoutouts", {})[folder.name] = f"YouTube: {', '.join(names) or 'none'}"
        except (llm.OutOfQuota, llm.Overloaded) as err:
            done.setdefault("held", {})[f"{folder.name} (shoutouts)"] = f"Gemini: {err}"[:200]
        except Exception as err:
            log.exception("the YouTube shoutouts of %s failed", folder.name)
            done["errors"].append(f"the YouTube shoutouts of {folder.name} failed: {err}"[:300])


def main() -> None:
    from .cli import PIPELINE_ROOT, _load_env

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _load_env(PIPELINE_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Name the viewers who answered the previous question right.")
    parser.add_argument("reel", help="the Reel that answers the question, e.g. ak044")
    for channel in CHANNELS:
        parser.add_argument(f"--{channel}", nargs="*", metavar="NAME",
                            help=f"the right answerers on {channel}, as it names them (replaces the list)")
    parser.add_argument("--comments", nargs=2, metavar=("CHANNEL", "FILE"),
                        help='judge a JSON list of {"name", "text", "at"} comments from CHANNEL and name the right ones')
    parser.add_argument("--links", action="store_true", help="where the asking Reel's posts are, to read comments")
    args = parser.parse_args()
    folder = reels.REELS / args.reel
    now, done = datetime.now(timezone.utc), {}
    if args.links:
        if not (found := asked_quiz(folder)):
            raise SystemExit(f"{args.reel} answers no question")
        done["asked"] = {"reel": found[0].name, "question": found[1]["question"], "answer": found[1]["answer"],
                         "links": links(found[0])}
    for channel in CHANNELS:
        if (names := getattr(args, channel)) is not None:
            done[channel] = {"names": names, "changed": put(folder, channel, names, now),
                             "post": post_state(folder, channel, now)}
    if args.comments:
        channel, file = args.comments
        if channel not in CHANNELS or not (found := asked_quiz(folder)):
            raise SystemExit(f"{args.reel} answers no question, or {channel} isn't one of {', '.join(CHANNELS)}")
        comments = [c | {"id": c.get("id") or f"{c['name']}: {c['text']}"[:200]}
                    for c in json.loads(Path(file).read_text(encoding="utf-8"))]
        names = right_names(folder, channel, found[1], comments)
        done[channel] = {"names": names, "changed": put(folder, channel, names, now),
                         "post": post_state(folder, channel, now)}
    print(json.dumps(done, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
