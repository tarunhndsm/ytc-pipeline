"""Choose every line's picture for a written Reel and frame it for a phone screen: candidates from the open-access
collections for each line's search terms (reel_tools), the best few by SigLIP (rank.py), one look by a vision model
at each line's candidates to choose, and a second look at each chosen picture, as the renderer will open it, to find
its subject and faces and catch what mustn't be shown. The crops, caption heights and credits follow from that by
rule, as plates.py lays pictures out.

    python -m ytc.reel_pictures <reel folder>     write <folder>/short.yaml from its work/script.json
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml
from PIL import Image, ImageDraw, ImageFont

from . import llm, pick, plates, rank, reel_tools, reel_writer, reels, visuals
from .spec import ShortSpec

log = logging.getLogger(__name__)

PER_SEARCH = 6
SHOWN = 6
CELL = 384
FRAME_SIDE = 768
# A picture shorter than this can't fill the screen even before its scan margins are trimmed.
MIN_HEIGHT = 1300
PHONE = 9 / 16
# The caption style's height (CaptionStyle.y), the other heights captions can move to, and how far a caption block
# reaches above and below its height.
CAPTION_Y = 0.60
CAPTION_MOVES = (0.80, 0.22)
CAPTION_REACH = 0.08
# The hook's box over line 1 starts at hook_y and is about this tall; it moves down when a face is under it.
HOOK_Y, HOOK_LOW, HOOK_REACH = 0.125, 0.45, 0.2
FRAME_ROUNDS = 3
# Lines left without a picture of their own show an earlier line's again, framed differently; more than this many
# and the Reel would look repetitive.
MAX_REUSED = 2
KINDS = ("painting", "drawing", "print", "sculpture", "photo")
_NOT_PICTURES = (".tif", ".tiff", ".svg", ".gif", ".webm", ".pdf", ".djvu")
_OPEN = ("public domain", "cc0", "pd")
# What reel_qa needs to swap a picture after the render: the chosen ones as framed, and each line's candidates that
# weren't used or set aside, with the fields to frame and credit them.
PICTURES = "work/pictures.json"
SPARES = "work/spares.json"
# A Reel the judge failed on its pictures (reel_factory.send_back): the pictures it found fault with, never chosen again.
AVOID = "work/avoid.json"
HIT_KEYS = ("ref", "title", "date", "url", "page", "thumb", "source", "license", "artist", "width", "height")
FRAMED_KEYS = ("size", "subject", "faces", "kind", "hindi")
# A title naming another faith's figures or books, or a Mughal court, never reaches the models: both prompts forbid
# such pictures, yet on 13 Oct they chose a Jain teacher for Ajamila, and a Kalpa-sutra page, Buddhist deities and
# Murad Bakhsh's portrait for Astika. A Mughal court goes by its names, since a "Sub-Imperial Mughal" Ramayana folio
# is the epic; the Razmnama carries Persian script. "Saint" stays allowed: museums title rishis and yogis that way too.
OTHER_FAITH = re.compile(r"\b(jaina?|jina|mahavira|tirthankara|kalpa[- ]?sutra|parsh?vanatha|buddh\w*|bodhisattva|"
                         r"avalokiteshvara|manjushri|lama|tibet\w*|shah|jahangir|akbar\w*|aurangzeb|babur\w*|murad|"
                         r"razm\w*|hamza\w*|sultan|nawab|mirza|begum|sufi|dervish|qur'?an|koran|sikh|nanak|christ|"
                         r"jesus|madonna)\b", re.I)
# A book of Hindu myths, with Indian artists' paintings of the epics, whose title names the Buddhists too.
NOT_OTHER_FAITH = re.compile(r"myths of the hindus (&|and) buddhists", re.I)
# Cleveland and the Met say where an object was made ("Northern India, Pahari kingdoms", "China, Ming dynasty"), and
# art from outside South Asia is never wanted. Commons doesn't say, so its files go by their titles alone.
SOUTH_ASIA = re.compile(r"india|nepal|pakistan|bangladesh|sri lanka|ceylon|deccan|kashmir|bengal|rajasthan|punjab|"
                        r"pahari|tamil|mughal|rajput|gandhara", re.I)
MET_GAP = 1.0
MET_SEARCHES = 12
_met_lock = threading.Lock()
_met_budget = {"searches": 0, "refused": False}

CHOOSE_SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {"type": "object", "properties": {
        "line": {"type": "integer"}, "ranked": {"type": "array", "items": {"type": "string"}}},
        "required": ["line", "ranked"]}}},
    "required": ["lines"],
}

CHOOSE_PROMPT = """You choose the pictures for a Reel of अनकही कथाएँ: stories from the Ramayana, the Mahabharata and
the Puranas told in Hindi on Instagram. While a line of narration plays, its picture fills a vertical phone screen, so
what it shows must be clear, and every line needs a different picture.

Each line below has its words, what its picture should show, and a sheet of candidates labelled like 3-2 (the second
candidate found for line 3), with their titles. Any candidate may go to any line, but each to one line only.

For every line, rank up to 3 labels, best first. Prefer, in this order: a painting or sculpture of this very moment
of the story, or of the deity or person the line names; the same deity, person or place in another scene; a picture
that sets the scene (a court, a forest, a river) without contradicting the line. Go by the titles as much as the
pictures. Line 1's picture is the Reel's first frame in the feed, where viewers decide in a second whether to stay:
rank first the most vivid, colourful painting of the figure line 1 names, clear at phone size (Ravana's ten heads in
orange and gold drew 65,000 plays); Reels that opened on a grey sculpture, a flower or a snake lost most viewers at
once. Never: a photograph of the world today (a river, an animal, an object, a building, people), which looks out
of place among the paintings (a photograph of a painting, carving or sculpture counts as that art); nudity (bare
breasts count, as temple sculpture and some paintings show them: Instagram hides such Reels), gore, a large
watermark, a page of text, writing in Persian or Arabic script (Mughal manuscripts of the epics, such as the
Razmnama, carry it), art from outside South Asia (Greek, Roman, European, or a European artist's view of India, such
as a British watercolour of a ghat), a deity standing in for a person or a person for a deity (Vishnu is never a
sage), a figure of another faith standing in for the story's (a Jain monk for a rishi, a Tibetan Buddhist Yama for
the Yama of the Upanishads), a named deity, person or story other than the one the line is about, or a figure who
hasn't entered the story yet at that line (Hanuman beside Rama before they meet). Judges failed Reels for each of
these. Where two candidates are close, keep the Reel to one kind of art (miniatures, or later realistic paintings)
rather than a mix. Leave the list empty when nothing qualifies.

The story: {story}
"""

FRAME_SCHEMA = {
    "type": "object",
    "properties": {"pictures": {"type": "array", "items": {"type": "object", "properties": {
        "line": {"type": "integer"},
        "ok": {"type": "boolean"},
        "problem": {"type": "string"},
        "subject": {"type": "array", "items": {"type": "number"}},
        "faces": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}},
        "kind": {"type": "string", "enum": list(KINDS)},
        "hindi": {"type": "string"},
    }, "required": ["line", "ok", "problem", "subject", "faces", "kind", "hindi"]}}},
    "required": ["pictures"],
}

FRAME_PROMPT = """Each picture below was chosen for a line of a Reel of stories from the Ramayana, the Mahabharata and
the Puranas, told in Hindi. For each one:
- ok: false only for a clear problem: a photograph of the world today rather than of a painting, carving or
  sculpture, nudity (bare breasts count, in sculpture too), gore, a large watermark or text across it, writing in
  Persian or Arabic script anywhere on it, too damaged or blurry to make out, art from outside
  South Asia (a European artist's view of India counts), a deity shown for a person or a person for a deity, a figure
  of another faith shown for the story's (a Jain monk for a rishi, a Tibetan Buddhist Yama), or it names or shows a
  different deity, person or story than the line is about. Otherwise true.
- problem: that problem in a few words, or "".
- subject: [left, top, right, bottom], each 0 to 1 (0 is the left or top edge), around what the line is about: the
  figure or group it names, with what they hold or do.
- faces: a box like that around each face big enough to make out, or [] when there is none.
- kind: painting, drawing, print, sculpture or photo.
- hindi: for the credits, what the picture itself is, going by its title, in 3 to 7 Hindi words, like "रथ में सूर्य
  और अरुण" or "गंगा देवी की मूर्ति". Never the line's own story: a picture titled "River Goddess Ganga" is "गंगा देवी की
  मूर्ति" even on a line about someone drowning.
"""


def _met(term: str, limit: int) -> list[dict]:
    # About a hundred requests in a few seconds get an address refused (HTTP 403) for some minutes, so the Met
    # gets one search at a time, gently, up to MET_SEARCHES a Reel, and none after a refusal.
    with _met_lock:
        if _met_budget["refused"] or _met_budget["searches"] >= MET_SEARCHES:
            return []
        _met_budget["searches"] += 1
        try:
            found = reel_tools.met(term, limit, workers=2)
        except requests.HTTPError as err:
            if err.response is not None and err.response.status_code == 403:
                _met_budget["refused"] = True
                log.info("the Met refused a search; no more Met searches for this Reel")
                return []
            raise
        time.sleep(MET_GAP)
        return found


def _search(term: str) -> list[dict]:
    hits = []
    for find in (reel_tools.cleveland, _met, reel_tools.commons):
        try:
            hits += find(term, PER_SEARCH)
        except (requests.RequestException, ValueError, KeyError) as err:
            log.info("%s search for %r failed: %s", find.__name__, term, str(err)[:120])
    return [h for h in hits if h["allowed"] and h.get("thumb") and h.get("url")
            and int(h.get("height") or MIN_HEIGHT) >= MIN_HEIGHT and not h["url"].lower().endswith(_NOT_PICTURES)
            and not other_faith(h.get("title") or "") and (not h.get("culture") or SOUTH_ASIA.search(h["culture"]))]


def other_faith(title: str) -> bool:
    return bool(OTHER_FAITH.search(NOT_OTHER_FAITH.sub("", title)))


def own_lines(beats: list[dict]) -> list[int]:
    """The lines that get a picture of their own: the story's. The answer to the last Reel's question shows that
    Reel's first picture and the question for the viewers line 1's; a script from before the quiz, without roles,
    gave its last line line 1's picture."""
    if not any("role" in beat for beat in beats):
        return list(range(1, len(beats)))
    return [n for n, beat in enumerate(beats, start=1) if beat.get("role", "story") == "story"]


def candidates(beats: list[dict]) -> dict[int, list[dict]]:
    """Each line's candidates (the lines with a picture of their own), one dict per picture."""
    lines = own_lines(beats)
    # Every line's first term (the name of whoever it shows) is searched first, then every line's second, and so on.
    ordered = sorted((k, t) for n in lines for k, t in enumerate(beats[n - 1]["search"]))
    terms = list(dict.fromkeys(t for _, t in ordered))
    _met_budget.update(searches=0, refused=False)
    with ThreadPoolExecutor(6) as workers:
        found = dict(zip(terms, workers.map(_search, terms)))
    pictures: dict[str, dict] = {}
    per_line: dict[int, list[dict]] = {}
    for number in lines:
        beat = beats[number - 1]
        mine = per_line[number] = []
        for term in beat["search"]:
            for hit in found[term]:
                hit = pictures.setdefault(hit["ref"], hit)
                if hit not in mine:
                    mine.append(hit)
    return per_line


def _thumbs(hits: list[dict]) -> None:
    def fetch(hit: dict) -> None:
        try:
            made = pick._jpeg(visuals._get(hit["thumb"], timeout=30).content, CELL)
        except requests.RequestException:
            made = None
        if made:
            hit["image"], image = made
            hit["hash"] = pick._dhash(image)
            hit["small"] = pick._jpeg(hit["image"], pick.RANK_SIDE)[0]

    with ThreadPoolExecutor(8) as workers:
        list(workers.map(fetch, [h for h in hits if "image" not in h]))


def shortlist(beats: list[dict], per_line: dict[int, list[dict]]) -> dict[int, list[dict]]:
    """Each line's best SHOWN candidates by SigLIP's match to its brief, without copies of one another."""
    hits = list({h["ref"]: h for found in per_line.values() for h in found}.values())
    _thumbs(hits)
    hits = [h for h in hits if "image" in h]
    lines = sorted(per_line)
    scores = rank.remote_or_here([beats[n - 1]["picture"] for n in lines], [h["small"] for h in hits])
    for j, hit in enumerate(hits):
        # Without a ranker every match is equal, and the sort keeps the order the searches returned.
        hit["match"] = {n: scores["match"][i][j] if scores else 0.5 for i, n in enumerate(lines)}
    shown = {}
    for number in lines:
        kept: list[dict] = []
        for hit in sorted((h for h in per_line[number] if "image" in h), key=lambda h: -h["match"][number]):
            if not any(bin(hit["hash"] ^ k["hash"]).count("1") <= pick.SAME_PICTURE for k in kept):
                kept.append(hit)
            if len(kept) == SHOWN:
                break
        shown[number] = kept
    return shown


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(plates.FONTS / "Poppins-Black.ttf"), size)


def _sheet(hits: list[dict], number: int) -> bytes:
    cols = 3
    canvas = Image.new("RGB", (cols * CELL, -(-len(hits) // cols) * CELL), "white")
    draw, font = ImageDraw.Draw(canvas), _font(34)
    for k, hit in enumerate(hits):
        image = Image.open(io.BytesIO(hit["image"]))
        image.thumbnail((CELL - 8, CELL - 8))
        x, y = (k % cols) * CELL, (k // cols) * CELL
        canvas.paste(image, (x + (CELL - image.width) // 2, y + (CELL - image.height) // 2))
        label = f"{number}-{k + 1}"
        draw.rectangle((x + 4, y + 4, x + 16 + 24 * len(label), y + 50), fill="black")
        draw.text((x + 10, y + 4), label, fill="#FFD400", font=font)
    buffer = io.BytesIO()
    canvas.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


def choose(beats: list[dict], shown: dict[int, list[dict]], story: str, purpose: str) -> dict[int, list[dict]]:
    """Each line's candidates in the order the model ranks them, best first."""
    labels: dict[str, dict] = {}
    parts: list = [CHOOSE_PROMPT.format(story=story)]
    for number, hits in shown.items():
        beat = beats[number - 1]
        titles = "; ".join(f"{number}-{k}: {h['title'][:90]} ({h.get('date') or 'undated'}, {h['source']})"
                           for k, h in enumerate(hits, start=1))
        parts.append(f"\nLine {number}: \"{beat['text']}\"\nShould show: {beat['picture']}\nCandidates: {titles or 'none'}")
        if hits:
            parts.append(_sheet(hits, number))
            labels |= {f"{number}-{k}": h for k, h in enumerate(hits, start=1)}
    answer = llm.generate(parts, schema=CHOOSE_SCHEMA, models=reel_writer.PICTURE_MODELS, purpose=purpose)
    ranked = {n: [] for n in shown}
    for item in answer["lines"]:
        if item["line"] in ranked:
            names = [label.strip().strip("[]") for label in item["ranked"]]
            ranked[item["line"]] = [labels[name] for name in names if name in labels]
    return ranked


def _framing_image(hit: dict) -> bytes:
    image = reel_tools._picture(hit["url"])
    hit["size"] = image.size
    image = image.copy()
    image.thumbnail((FRAME_SIDE, FRAME_SIDE))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


def _box(value) -> list[float] | None:
    if isinstance(value, list) and len(value) == 4 and all(isinstance(v, (int, float)) for v in value):
        # Flash Lite sometimes gives some of a box's values on Gemini's own 0-1000 scale instead of 0 to 1.
        left, top, right, bottom = (min(max(v / 1000 if v > 1.5 else float(v), 0.0), 1.0) for v in value)
        if right - left > 0.02 and bottom - top > 0.02:
            return [left, top, right, bottom]
    return None


def _opened(item: tuple[int, dict]) -> tuple[int, dict, bytes | None]:
    number, hit = item
    try:
        image = _framing_image(hit)
    except (requests.RequestException, OSError) as err:
        log.info("line %d: %s didn't open (%s)", number, hit["ref"], str(err)[:120])
        hit["unusable"] = True
        return number, hit, None
    # The Met gives no sizes before the download.
    if hit["size"][1] < MIN_HEIGHT:
        log.info("line %d: %s is only %dx%d", number, hit["ref"], *hit["size"])
        hit["unusable"] = True
        return number, hit, None
    return number, hit, image


def frame_all(beats: list[dict], ranked: dict[int, list[dict]], shown: dict[int, list[dict]],
              purpose: str) -> dict[int, dict]:
    """Each line's picture, looked at as the renderer opens it: its size, subject and faces, kind and Hindi caption.
    A picture found wanting gives way to the line's next choice, then to the rest of its shortlist, for up to
    FRAME_ROUNDS more looks; a line whose candidates all fail is left out."""
    order = {n: ranked[n] + [h for h in shown[n] if h not in ranked[n]] for n in ranked}
    chosen: dict[int, dict] = {}
    used: set[str] = set()
    todo = sorted(ranked)
    for _ in range(FRAME_ROUNDS + 1):
        trial = {}
        for number in todo:
            if hit := next((h for h in order[number] if h["ref"] not in used), None):
                trial[number] = hit
                used.add(hit["ref"])
            else:
                log.info("line %d has no candidates left", number)
        if not trial:
            break
        with ThreadPoolExecutor(4) as workers:
            opened = list(workers.map(_opened, trial.items()))
        parts: list = [FRAME_PROMPT]
        for number, hit, image in opened:
            if image:
                parts += [f"\nLine {number}: \"{beats[number - 1]['text']}\" (picture: {hit['title'][:90]})", image]
        answer = llm.generate(parts, schema=FRAME_SCHEMA, models=reel_writer.PICTURE_MODELS, purpose=purpose)
        looked = {item["line"]: item for item in answer["pictures"] if item["line"] in trial}
        todo = []
        for number, hit in trial.items():
            item = looked.get(number) or {}
            # Judges mark a Reel down for a photograph among its paintings, even where the prompt let one through.
            if item.get("ok") and item["kind"] != "photo" and (subject := _box(item["subject"])) and "size" in hit:
                chosen[number] = hit | {"subject": subject, "faces": [f for f in map(_box, item["faces"]) if f],
                                        "kind": item["kind"], "hindi": item["hindi"].strip()}
            else:
                why = item.get("problem") or ("a photograph" if item.get("kind") == "photo" else "not framed")
                log.info("line %d: %s set aside (%s)", number, hit["ref"], why)
                hit.setdefault("set_aside", []).append(number)
                todo.append(number)
        if not todo:
            break
    return chosen


def crop_for(size: tuple[int, int], subject: list[float]) -> list[float]:
    """The part of the picture that fills the phone screen around its subject, with room to breathe, or [] to show
    the whole picture as a framed card when the subject is too wide or the picture too small to fill the screen."""
    width, height = size
    left, top, right, bottom = subject
    aspect = width / height
    tall = min(1.0, max((bottom - top) * 1.35, plates.COVER_MIN_HEIGHT / height, 0.45))

    def wide(ratio: float, h: float) -> float:
        return ratio * h / aspect

    across = max(wide(PHONE, tall), (right - left) * 1.1)
    if across > wide(plates.COVER_RATIO, tall):
        tall = min(1.0, (right - left) * 1.1 * aspect / plates.COVER_RATIO)
        across = max(wide(PHONE, tall), (right - left) * 1.1)
    across = min(across, 1.0)
    if across > wide(plates.COVER_RATIO, tall) + 1e-6 or tall * height < plates.COVER_MIN_HEIGHT:
        return []
    x = min(max((left + right) / 2 - across / 2, 0.0), 1.0 - across)
    y = min(max((top + bottom) / 2 - tall / 2, 0.0), 1.0 - tall)
    return [round(x, 3), round(y, 3), round(x + across, 3), round(y + tall, 3)]


def _spans(faces: list[list[float]], crop: list[float]) -> list[tuple[float, float]]:
    """Each face's top and bottom on the screen, for the faces inside the crop."""
    left, top, right, bottom = crop or [0.0, 0.0, 1.0, 1.0]
    return [((f[1] - top) / (bottom - top), (f[3] - top) / (bottom - top)) for f in faces
            if f[2] > left and f[0] < right and f[3] > top and f[1] < bottom]


def _clear(spans: list[tuple[float, float]], low: float, high: float) -> bool:
    return all(b < low or t > high for t, b in spans)


def caption_height(spans: list[tuple[float, float]]) -> float | None:
    """Where a line's captions go to cover no face: None for the style's own height."""
    if _clear(spans, CAPTION_Y - CAPTION_REACH, CAPTION_Y + CAPTION_REACH):
        return None
    return next((y for y in CAPTION_MOVES if _clear(spans, y - CAPTION_REACH, y + CAPTION_REACH)), None)


def _license(hit: dict) -> str:
    name = hit["license"].strip()
    return "सार्वजनिक डोमेन" if name.lower().startswith("public domain") else name


def _artist(hit: dict) -> str:
    """A Commons artist as a name, or "" when unknown. The field can run on ("Cleveland Museum of Art. Released
    under CC0 per page"), carry life dates ("Colonel George Strahan (1839 – 7 January 1911)"), or say itself twice
    ("Unknown authorUnknown author"); initials ("E. A. Rodrigues") stay."""
    artist = re.sub(r"\s*\([^)]*\d{3}[^)]*\)", "", hit.get("artist") or "")
    artist = re.split(r"(?<=[a-z]{2})\. ", artist)[0].strip()
    half = len(artist) // 2
    if artist and artist[:half] == artist[half:]:
        artist = artist[:half]
    return "" if re.match(r"(unknown|anonymous)\b", artist, re.I) else artist[:60]


def _who(hit: dict) -> str:
    if hit["source"] != "Wikimedia Commons":
        return hit["source"]
    if hit.get("artist") and not _artist(hit):
        return "अज्ञात चित्रकार"
    return _artist(hit) or "Wikimedia Commons"


def credit_lines(hits: list[dict]) -> str:
    """The caption's picture credits, as the hand-made Reels give them."""
    open_art = [f"{h['hindi']}, {_who(h)}" for h in hits if h["kind"] != "photo" and h["license"].lower().startswith(_OPEN)]
    other_art = [f"{h['hindi']}, {_who(h)} ({_license(h)})" for h in hits
                 if h["kind"] != "photo" and not h["license"].lower().startswith(_OPEN)]
    photos = [f"{h['hindi']}, {_who(h)} ({_license(h)})" for h in hits if h["kind"] == "photo"]
    lines = []
    if open_art or other_art:
        art = "; ".join(open_art) + (" (सभी सार्वजनिक डोमेन या CC0)" if open_art else "")
        lines.append("चित्र: " + "; ".join(p for p in (art, "; ".join(other_art)) if p))
    if photos:
        lines.append("फ़ोटो: " + "; ".join(photos))
    return "\n".join(lines)


def _visual(hit: dict, crop: list[float], number: int) -> dict:
    credit = {"source": hit["source"], "title": hit["title"], "license": hit["license"], "url": hit.get("page") or hit["url"]}
    if hit["source"] == "Wikimedia Commons" and hit.get("artist"):
        credit["credit"] = _artist(hit) or "Unknown artist"
    return {"source": "url", "url": hit["url"], "credit": credit, **({"crop": crop} if crop else {}),
            "motion": "zoom_in" if number % 2 else "zoom_out"}


class _Dumper(yaml.SafeDumper):
    """Writes text with line breaks (the caption) as a block, as the hand-made specs have it."""


_Dumper.add_representer(str, lambda dumper, value: dumper.represent_scalar(
    "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None))


def place(spec: dict, number: int, hit: dict) -> None:
    """Show `hit` on line `number`: cropped around its subject, with the line's captions (and on line 1 the hook,
    on a line with a label its box) clear of its faces."""
    beat = spec["beats"][number - 1]
    crop = crop_for(hit["size"], hit["subject"])
    beat["visual"] = _visual(hit, crop, number)
    beat.pop("caption_y", None)
    if number == 1:
        spec["captions"].pop("hook_y", None)
    elif beat.get("label"):
        # The label's box would otherwise sit at line 1's hook height, chosen for line 1's picture.
        beat["label_y"] = HOOK_Y
    if not crop:
        # A card: the picture sits whole in a frame above the captions.
        return
    spans = _spans(hit["faces"], crop)
    if (number == 1 or beat.get("label")) and not _clear(spans, HOOK_Y, HOOK_Y + HOOK_REACH):
        if number == 1:
            spec["captions"]["hook_y"] = HOOK_LOW
        else:
            beat["label_y"] = HOOK_LOW
        spans.append((HOOK_LOW, HOOK_LOW + HOOK_REACH))
    elif beat.get("label"):
        spans.append((HOOK_Y, HOOK_Y + HOOK_REACH))
    if (height := caption_height(spans)) is not None:
        beat["caption_y"] = height


def reuse(spec: dict, number: int, earlier: int) -> None:
    """Show line `earlier`'s picture again on line `number`, moving the other way, with its captions' height."""
    beat = spec["beats"][number - 1]
    beat["visual"] = {"reuse": earlier, "motion": "zoom_out" if earlier % 2 else "zoom_in"}
    beat.pop("caption_y", None)
    if "caption_y" in spec["beats"][earlier - 1]:
        beat["caption_y"] = spec["beats"][earlier - 1]["caption_y"]


def run_length(shows: dict[int, int], number: int) -> int:
    """How many lines in a row, around line `number`, show the picture it shows (`shows`: line -> the line whose
    picture it shows)."""
    picture, run = shows.get(number), 1
    for step in (-1, 1):
        n = number + step
        while shows.get(n) == picture:
            run, n = run + 1, n + step
    return run


def least_run(options: list[int], shows: dict[int, int], number: int) -> int | None:
    """The nearest of `options` (earlier lines) whose picture line `number`, and the lines showing line `number`'s,
    can show without one picture staying on screen for three lines in a row, which judges fail a Reel for."""
    for earlier in sorted(options, reverse=True):
        trial = {n: earlier if n == number or seen == number else seen for n, seen in shows.items()} | {number: earlier}
        if run_length(trial, number) < 3:
            return earlier
    return None


def shown_again(beats: list[dict], chosen: dict[int, dict], number: int, shows: dict[int, int] | None = None) -> int:
    """The earlier line whose picture a line without one shows: the nearest that searched first for the same subject
    (a line naming Ashtavakra shows him again, not the court scene before it), else the nearest; either way not one
    that would stay on screen for a third line in a row, unless every one would."""
    earlier = [n for n in chosen if n < number and n in own_lines(beats)]
    same = [n for n in earlier if beats[n - 1].get("search", [])[:1] == beats[number - 1].get("search", [])[:1]]
    shows = shows or {}
    return least_run(same, shows, number) or least_run(earlier, shows, number) or max(same or earlier)


def spares_for(number: int, hits: list[dict], in_use: set[str]) -> list[dict]:
    """The line's candidates, best first, that no line shows and that weren't set aside for it."""
    kept, seen = [], set()
    for hit in hits:
        if hit["ref"] in seen or hit["ref"] in in_use or hit.get("unusable") or number in hit.get("set_aside", ()):
            continue
        seen.add(hit["ref"])
        kept.append({k: hit[k] for k in HIT_KEYS if hit.get(k) is not None})
    return kept


def with_credits(description: str, hits: list[dict]) -> str:
    """The description with its last lines, the picture credits, made for `hits`."""
    kept = [line for line in description.rstrip("\n").split("\n") if not line.startswith(("चित्र: ", "फ़ोटो: "))]
    return "\n".join(kept) + "\n" + credit_lines(hits)


def save(folder: Path, spec: dict, chosen: dict[int, dict], spares: dict[int, list[dict]]) -> Path:
    """Write short.yaml with the credits of the pictures it shows, and what reel_qa needs to change them."""
    shown = {n: chosen[n] for n in sorted(chosen)}
    spec["description"] = with_credits(spec["description"], list(shown.values()))
    ShortSpec.model_validate(spec)
    path = folder / "short.yaml"
    path.write_text(yaml.dump(spec, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=100000), encoding="utf-8")
    (folder / PICTURES).write_text(json.dumps(
        {n: {k: hit[k] for k in (*HIT_KEYS, *FRAMED_KEYS) if hit.get(k) is not None} for n, hit in shown.items()},
        ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (folder / SPARES).write_text(json.dumps({n: spares[n] for n in sorted(spares)}, ensure_ascii=False, indent=1) + "\n",
                                 encoding="utf-8")
    return path


def picture(folder: Path) -> Path:
    """Choose and frame every picture for the Reel in `folder` and write its short.yaml."""
    draft = json.loads((folder / reel_writer.SCRIPT).read_text(encoding="utf-8"))
    beats = draft["script"]["beats"]
    story = f"{draft['row']['story']} ({draft['row']['source']})"
    per_line = candidates(beats)
    if (folder / AVOID).exists():
        avoid = set(json.loads((folder / AVOID).read_text(encoding="utf-8"))["refs"])
        per_line = {n: [h for h in hits if h["ref"] not in avoid] for n, hits in per_line.items()}
        log.info("left out %d pictures the judge found fault with", len(avoid))
    log.info("%d candidates for %d lines", len({h["ref"] for hits in per_line.values() for h in hits}), len(per_line))
    shown = shortlist(beats, per_line)
    log.info("shortlisted %d pictures", sum(map(len, shown.values())))
    with reel_writer.gemini_only():
        ranked = choose(beats, shown, story, f"{draft['id']} pictures")
        chosen = frame_all(beats, ranked, shown, f"{draft['id']} framing")
    if 1 not in chosen and chosen:
        # The hook can't go without a picture: it takes the next line's, which then shows it again.
        log.info("line 1 takes line %d's picture", min(chosen))
        chosen[1] = chosen.pop(min(chosen))
    own = own_lines(beats)
    missing = [n for n in own if n not in chosen]
    # The factory allows more on a story's last try (reel_factory.LAST_TRY_REUSED).
    if 1 in missing or len(missing) > int(os.environ.get("YTC_MAX_REUSED") or MAX_REUSED):
        raise LookupError(f"no usable picture for lines {missing}")
    spec = reel_writer.spec_fields(draft)
    shows = {n: n for n in chosen} | {n: 1 for n in range(1, len(beats) + 1) if n not in own}
    for number in range(1, len(beats) + 1):
        beat = beats[number - 1]
        if beat.get("role") == "answer" and (hit := asked_with(beat["from"])):
            chosen[number], shows[number] = hit, number
            place(spec, number, hit)
        elif number not in own:
            reuse(spec, number, 1)
        elif number in missing:
            earlier = shows[number] = shown_again(beats, chosen, number, shows)
            log.info("line %d shows line %d's picture again", number, earlier)
            reuse(spec, number, earlier)
        else:
            place(spec, number, chosen[number])
    in_use = {hit["ref"] for hit in chosen.values()}
    spares = {n: spares_for(n, ranked.get(n, []) + hits, in_use) for n, hits in shown.items()}
    return save(folder, spec, chosen, spares)


def asked_with(reel: str) -> dict | None:
    """The first picture of the Reel whose question a line answers, as its pictures.json has it framed."""
    path = reels.REELS / reel / PICTURES
    hit = (json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}).get("1")
    return hit if hit and hit.get("size") and hit.get("subject") else None


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ytc.reel_pictures", description="choose and frame a Reel's pictures")
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()

    from .cli import PIPELINE_ROOT, _load_env

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _load_env(PIPELINE_ROOT / ".env")
    llm.use_brand_project()
    os.environ.setdefault("HF_HOME", reel_tools.MODEL_CACHE)
    print(picture(args.folder.resolve()))


if __name__ == "__main__":
    main()
