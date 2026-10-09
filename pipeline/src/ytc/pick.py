"""Choose a picture for each beat, or a designed title card where no picture fits.

Candidates come from sources.gather (each beat's subjects through Wikipedia, Wikidata, and Commons, plus keyword
searches), and from other open archives for beats those leave weak. rank.py orders them by how well they match
what the beat should show and marks modern photos, so the model only looks at the best few per beat. It picks,
says how well each pick fits and where its subject is, and a cheaper second look flags clear problems before
anything is rendered.
"""

from __future__ import annotations

import io
import logging
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from . import llm, rank, sources
from .visuals import _get

log = logging.getLogger(__name__)

SHOWN_PER_BEAT = 6
SHOWN_POOL = 6
THUMB_SIDE = 512
RANK_SIDE = 240
# A beat whose best candidate matches worse than this (0 to 1) also gets candidates from other archives.
WEAK_MATCH = 0.3
# A picture that is probably a modern color photo counts for this much in a beat set before MODERN_BEFORE.
MODERN_BEFORE = 1945
MODERN_FACTOR = 0.35
MODERN_BELOW = 0.3
# Found through the beat's own subject, a candidate is more likely to be the right thing than a keyword hit.
ROUTE_BONUS = {"subject": 0.08, "article": 0.05, "depicts": 0.05, "category": 0.03, "period": 0.02}
# Pictures whose 64-bit difference hashes are this close are copies of one another.
SAME_PICTURE = 6
# Three in format 2's 7 to 12 beats: with the writer's two, a picture had to fill three beats running (ep111).
MAX_CARDS = 3
# Image checks a beat's picture gets before the render: the first pick, then what replaced it if that was flagged.
# The reviewer judges only after a render, and its daily quota is far smaller than the checker's.
CHECKS = 2
FITS = ("exact", "close", "scene", "none")
LABEL = re.compile(r"\b(B\d+-\d+|P\d+)\b")
MOTIONS_WIDE = ("pan_left", "pan_right")
MOTIONS_TALL = ("pan_up", "pan_down")
MOTIONS_SQUARE = ("zoom_in", "zoom_out")
# What a visual keeps of its candidate (the rest is bytes or only used while picking).
KEPT = ("key", "origin", "file", "title", "width", "height", "license", "license_url", "artist", "date", "year", "page",
        "route", "original", "source_name", "match", "historical")

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "beat": {"type": "integer"},
                    "ranked": {"type": "array", "items": {"type": "string"}},
                    "fit": {"type": "string", "enum": list(FITS)},
                    "shows": {"type": "string"},
                    "focus_x": {"type": "number"},
                    "focus_y": {"type": "number"},
                    "box": {"type": "array", "items": {"type": "number"}},
                },
                "required": ["beat", "ranked", "fit", "shows", "focus_x", "focus_y", "box"],
            },
        }
    },
    "required": ["picks"],
}

PICK_PROMPT = """You choose the pictures for a Days of Odd history Short. While each beat of narration plays,
its picture is on a vertical phone screen: a picture big enough fills the screen, a tall one whole and a wide
one cropped to a tall strip around its box; a small picture, or one whose subject is too wide for that strip,
is shown whole as a framed print over a dimmed copy of itself, and a long beat may cut to a close-up of the box.

Candidates are labelled like [B3-2] (found for beat 3) or [P4] (found for the story in general), each with its
file title, date, and how it was found ("subject" means it is the named subject's own picture on Wikidata).
Any candidate may be used for any beat, but each only once. Beat 1 is the hook: its picture has to stop someone
scrolling, so give it the most striking picture of the story's own subject.

For every beat:
- ranked: up to 3 labels, best first. Prefer, in this order: the actual person, place, object, document, or
  event the line names; the same subject or place at the time; a period picture that sets the scene without
  contradicting the line. Never: an unrelated or anachronistic picture (a modern photo for a historical line, any
  photograph for a line set before 1839, when photography began, or a photograph or object from decades after the
  line's year; a later painting of the event itself is fine),
  corpses, wounds, executions, gore, nudity, a large watermark, or a page of text unless the line is about that
  document. Leave ranked empty when nothing qualifies.
- fit: how well your first choice fits its line: "exact" (it shows what the line names), "close" (the same
  subject or place around that time), "scene" (it only sets the period), or "none".
- shows: what your first choice actually shows, as a short caption.
- focus_x, focus_y (0 = left or top edge, 1 = right or bottom): the center of the subject in your first choice.
- box: [left, top, right, bottom], each 0 to 1, around the part of your first choice that shows exactly what the
  line names (a face, a headline, a ship), all of it, since the screen is cropped to it; [] only when the whole
  picture is the subject (a drawing or print with wide blank paper around it is not: box the drawing).

Story: {story}
"""

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"checks": {"type": "array", "items": {"type": "object", "properties": {
        "beat": {"type": "integer"}, "ok": {"type": "boolean"}, "problem": {"type": "string"}},
        "required": ["beat", "ok", "problem"]}}},
    "required": ["checks"],
}

VERIFY_PROMPT = """Check the pictures chosen for a history Short before it is made. Each beat below has its line of
narration and the picture chosen for it. Mark a picture not ok only for a clear problem: it shows a different
person, place, or thing than the line is about, including someone the line only mentions (a line about a queen's
wedding needs her, not the king she married); it is from the wrong period (a modern photo, or present-day people,
for a historical line; any photograph for a line set before 1839, when photography began; a photograph or object
from decades after the line's year, though a later painting of the event itself is fine); it shows corpses,
wounds, gore, or nudity; it has a large watermark; it is mostly text
though the line isn't about a document; or it is too damaged or blurry to make out. A period picture that sets the
scene is ok. Give the problem in a few words, or "" when it is ok.

Story: {story}
"""


def _jpeg(data: bytes, side: int) -> tuple[bytes, Image.Image] | None:
    try:
        image = Image.open(io.BytesIO(data))
        image.draft("RGB", (side, side))
        image = image.convert("RGB")
    except Exception:
        return None
    image.thumbnail((side, side), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    return buffer.getvalue(), image


def _dhash(image: Image.Image) -> int:
    pixels = list(image.convert("L").resize((9, 8), Image.Resampling.BILINEAR).getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = bits << 1 | (pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return bits


def _thumbs(candidates: list[dict]) -> None:
    """Fetch each candidate's thumbnail as a JPEG (the model reads no WebP or TIFF), with its difference hash."""
    def fetch(candidate: dict) -> None:
        try:
            made = _jpeg(_get(candidate["thumb"], timeout=30).content, THUMB_SIDE)
        except Exception:
            made = None
        if made:
            candidate["image"], image = made
            candidate["hash"] = _dhash(image)
            candidate["small"] = _jpeg(candidate["image"], RANK_SIDE)[0]

    with ThreadPoolExecutor(8) as workers:
        list(workers.map(fetch, [c for c in candidates if "image" not in c]))


_year = sources.beat_year


def _looked_for(beat: dict) -> str:
    return beat.get("visual") or beat["text"]


def _rank(beats: list[dict], numbers: list[int], candidates: list[dict]) -> None:
    """Each candidate's match to every beat, and how likely it's a historical picture."""
    fresh = [c for c in candidates if c.get("small") and "matches" not in c]
    if not fresh:
        return
    scores = rank.remote_or_here([_looked_for(beats[n - 1]) for n in numbers], [c["small"] for c in fresh])
    for j, candidate in enumerate(fresh):
        if scores is None:
            # No ranker: keep the order the searches returned.
            candidate["matches"] = {n: 0.5 for n in numbers}
            candidate["historical"] = 0.5
        else:
            candidate["matches"] = {n: scores["match"][i][j] for i, n in enumerate(numbers)}
            candidate["historical"] = scores["historical"][j]


class _Board:
    """The candidates for one pick: per beat, and for the story in general."""

    def __init__(self, beats: list[dict], numbers: list[int], used: set[str]):
        self.beats, self.numbers, self.used = beats, numbers, used
        years = [y for n in numbers if (y := _year(beats[n - 1]))]
        self.story_year = min(years) if years else None
        self.per_beat: dict[int, list[dict]] = {n: [] for n in numbers}
        self.pool: list[dict] = []

    def value(self, candidate: dict, number: int, own: bool) -> float:
        match = (candidate.get("matches") or {}).get(number, 0.0)
        year = _year(self.beats[number - 1]) or self.story_year
        if year and year < MODERN_BEFORE and candidate.get("historical", 1.0) < MODERN_BELOW:
            match *= MODERN_FACTOR
        return match + (ROUTE_BONUS.get(candidate.get("route", ""), 0.0) if own else 0.0)

    def best(self, number: int) -> float:
        return max((self.value(c, number, True) for c in self.per_beat[number]), default=0.0)

    def add(self, per_beat: dict[int, list[dict]], pool: list[dict]) -> None:
        for n, found in per_beat.items():
            self.per_beat[n] += [c for c in found if c["key"] not in self.used]
        self.pool += [c for c in pool if c["key"] not in self.used]

    def settle(self) -> None:
        """Drop candidates without a thumbnail and copies of a better candidate; sort each beat's list."""
        everything = [(self.value(c, owner, True), c) for owner, found in self.per_beat.items() for c in found] + \
                     [(max((self.value(c, n, False) for n in self.numbers), default=0.0), c) for c in self.pool]
        kept_hashes: list[int] = []
        dropped: set[int] = set()
        for _, candidate in sorted(everything, key=lambda pair: -pair[0]):
            if not candidate.get("image"):
                dropped.add(id(candidate))
                continue
            if any(bin(candidate["hash"] ^ h).count("1") <= SAME_PICTURE for h in kept_hashes):
                dropped.add(id(candidate))
                continue
            kept_hashes.append(candidate["hash"])
        for n in self.numbers:
            found = [c for c in self.per_beat[n] if id(c) not in dropped]
            self.per_beat[n] = sorted(found, key=lambda c: -self.value(c, n, True))
        self.pool = sorted((c for c in self.pool if id(c) not in dropped),
                           key=lambda c: -max((self.value(c, n, False) for n in self.numbers), default=0.0))

    def shown(self) -> tuple[dict[int, list[dict]], list[dict]]:
        """The best few per beat, and a general pool: the story's own pictures, then beats' leftovers."""
        per_beat = {n: self.per_beat[n][:SHOWN_PER_BEAT] for n in self.numbers}
        spare = [c for n in self.numbers for c in self.per_beat[n][SHOWN_PER_BEAT:]]
        spare.sort(key=lambda c: -max((self.value(c, n, False) for n in self.numbers), default=0.0))
        return per_beat, (self.pool + spare)[:SHOWN_POOL]


def _ask(beats: list[dict], numbers: list[int], per_beat: dict[int, list[dict]], pool: list[dict], story: str,
         purpose: str) -> tuple[dict[str, dict], dict[int, dict]]:
    """(label -> candidate, beat -> pick) from one look at the candidates shown."""
    parts: list = [PICK_PROMPT.format(story=story)]
    labels: dict[str, dict] = {}

    def show(label: str, candidate: dict) -> None:
        labels[label] = candidate
        date = candidate.get("date") or (str(candidate["year"]) if candidate.get("year") else "undated")
        parts.append(f"[{label}] {candidate['title']} | {date} | {candidate.get('route', 'search')}")
        parts.append(candidate["image"])

    for n in numbers:
        beat = beats[n - 1]
        year = _year(beat)
        parts.append(f"\n## Beat {n}{f' ({year})' if year else ''}: \"{beat['text']}\"\nShould show: {_looked_for(beat)}")
        for k, candidate in enumerate(per_beat.get(n, []), start=1):
            show(f"B{n}-{k}", candidate)
    if pool:
        parts.append("\n## Candidates for the story in general")
        for k, candidate in enumerate(pool, start=1):
            show(f"P{k}", candidate)
    if not labels:
        return {}, {}
    parts.append("\nNow return a pick for each of these beats: " + ", ".join(map(str, numbers)))
    answer = llm.generate(parts, schema=PICK_SCHEMA, models=llm.BROAD, purpose=purpose)
    picks = {}
    for item in answer.get("picks", []):
        ranked = [m.group(1) for label in item.get("ranked", []) if (m := LABEL.search(label))]
        picks[item.get("beat")] = {**item, "ranked": [l for l in dict.fromkeys(ranked) if l in labels]}
    log.info("%s: %s", purpose, {n: (picks.get(n, {}).get("fit"), picks.get(n, {}).get("ranked")) for n in numbers})
    return labels, picks


def _verify(beats: list[dict], chosen: dict[int, dict], story: str, purpose: str, note: str = "") -> dict[int, str]:
    """Beats whose chosen picture has a clear problem, with the problem; {} when the check itself fails. `note` is
    what the reviewer said of the pictures these replace."""
    if not chosen:
        return {}
    parts: list = [VERIFY_PROMPT.format(story=story)]
    if note:
        parts.append(f"The reviewer turned down the pictures these replace. {note}")
    for n, candidate in sorted(chosen.items()):
        year = _year(beats[n - 1])
        date = candidate.get("date") or candidate.get("year") or "undated"
        parts += [f"\nBeat {n}{f' ({year})' if year else ''}: \"{beats[n - 1]['text']}\" (picture: {candidate['title']}, "
                  f"{date})", candidate["image"]]
    try:
        answer = llm.generate(parts, schema=VERIFY_SCHEMA, models=llm.LIGHT, purpose=purpose)
    except Exception as err:  # the reviewer still looks at every frame after the render
        log.warning("%s failed (%s); keeping the picks unchecked", purpose, err)
        return {}
    flagged = {c["beat"]: c.get("problem") or "flagged" for c in answer.get("checks", []) if not c.get("ok") and c.get("beat") in chosen}
    if flagged:
        log.info("%s: %s", purpose, flagged)
    return flagged


def _checked(beats: list[dict], options: dict[int, list[dict]], story: str, purpose: str, note: str) -> set[str]:
    """Take out of each beat's alternates, in place, the first ones the image check flags (up to CHECKS a beat), and
    return their keys."""
    taken_out: set[str] = set()
    # Copies: the thumbnails fetched for the check must not end up in visuals.json.
    checking = {n: dict(found[0]) for n, found in options.items() if found}
    for _ in range(CHECKS):
        _thumbs(list(checking.values()))
        flagged = _verify(beats, {n: c for n, c in checking.items() if c.get("image")}, story, purpose, note)
        checking = {}
        for n in flagged:
            taken_out.add(options[n].pop(0)["key"])
            if options[n]:
                checking[n] = dict(options[n][0])
    return taken_out


def _motion(candidate: dict, previous: str | None, number: int) -> str:
    ratio = (candidate.get("width") or 1) / (candidate.get("height") or 1)
    options = MOTIONS_WIDE if ratio > 1.25 else MOTIONS_TALL if ratio < 0.8 else MOTIONS_SQUARE
    choice = options[number % 2]
    return options[(number + 1) % 2] if choice == previous else choice


def _box(value) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        return []
    try:
        left, top, right, bottom = (min(max(float(v), 0.0), 1.0) for v in value)
    except (TypeError, ValueError):
        return []
    area = (right - left) * (bottom - top)
    return [round(left, 3), round(top, 3), round(right, 3), round(bottom, 3)] if left < right and top < bottom and 0.02 <= area <= 0.7 else []


def _credit(candidate: dict) -> dict:
    return {"source": candidate.get("source_name") or "Wikimedia Commons", "title": candidate["title"],
            "credit": candidate.get("artist") or "Unknown author", "license": candidate["license"], "url": candidate.get("page")}


def _card(beat: dict) -> dict | None:
    card = beat.get("card") or {}
    if card.get("kind") in ("dateline", "fact", "quote") and (card.get("big") or "").strip():
        return {"kind": card["kind"], "big": card["big"].strip(), "small": (card.get("small") or "").strip()}
    return None


def _kept(candidate: dict, number: int) -> dict:
    kept = {k: candidate[k] for k in KEPT if k in candidate}
    kept["match"] = round((candidate.get("matches") or {}).get(number, candidate.get("match", 0.0)), 3)
    if "historical" in kept:
        kept["historical"] = round(kept["historical"], 3)
    return kept


def _topic_articles(research: dict) -> list[str]:
    """The English Wikipedia articles the research read, by title."""
    titles = []
    for source in research.get("sources", []):
        if match := re.match(r"https?://en\.(?:m\.)?wikipedia\.org/wiki/([^#?]+)", source.get("url", "")):
            titles.append(urllib.parse.unquote(match.group(1)).replace("_", " "))
    return list(dict.fromkeys(titles))[:2]


def picture_visual(candidate: dict, number: int, previous: str | None, pick: dict | None = None, card: dict | None = None) -> dict:
    """The spec's visual for a candidate: fetched by URL, with its credit; the model's framing if it gave one."""
    pick = pick or {}
    visual = {"source": "url", "url": sources.full_url(candidate), "credit": _credit(candidate),
              "fallbacks": ["card", "color"] if card else ["color"], "motion": _motion(candidate, previous, number),
              "focus_x": round(min(max(float(pick.get("focus_x", 0.5)), 0), 1), 2),
              "focus_y": round(min(max(float(pick.get("focus_y", 0.5)), 0), 1), 2)}
    if box := _box(pick.get("box")):
        visual["box"] = box
    if card:
        visual["card"] = card
    visual["_choice"] = _kept(candidate, number)
    if pick.get("fit"):
        visual["_fit"] = pick["fit"]
    if pick.get("shows"):
        visual["_shows"] = pick["shows"][:160]
    return visual


def card_visual(card: dict) -> dict:
    return {"source": "card", "card": card, "motion": "zoom_in"}


def _is_loop(script: dict) -> bool:
    return bool(script.get("loop")) and len(script["beats"]) > 1


def _loop_visual(first: dict) -> dict:
    return {"reuse": 1, "motion": "zoom_out" if first.get("motion") != "zoom_out" else "zoom_in",
            **{k: first[k] for k in ("focus_x", "focus_y") if k in first}}


def _reuse_of(visuals: list, number: int) -> dict | None:
    """The nearest earlier picture again, framed differently, for a beat left without one of its own: not the one the
    beat before shows while there is another."""
    before = visuals[number - 2] if number > 1 else None
    previous = before.get("motion") if before else None
    on_screen = (before.get("reuse") or number - 1) if before else None
    earlier = [k for k in range(number - 1, 0, -1)
               if visuals[k - 1] and visuals[k - 1].get("_choice") and not visuals[k - 1].get("reuse")]
    for k in sorted(earlier, key=lambda k: k == on_screen):
        source = visuals[k - 1]
        options = [m for m in MOTIONS_SQUARE if m != source.get("motion")]
        return {"reuse": k, "motion": next((m for m in options if m != previous), options[0]),
                **{key: source[key] for key in ("focus_x", "focus_y") if key in source}}
    return None


def _line_card(beat: dict) -> dict:
    """The writer's card for the beat, else a fact card of the line's first emphasized words."""
    if card := _card(beat):
        return card
    words = " ".join((beat.get("emphasis") or [beat["text"]])[:1]).strip() or beat["text"][:28]
    return {"kind": "fact", "big": words[:28], "small": ""}


def _fallback(beat: dict, visuals: list, number: int, cards_left: int) -> dict:
    """A designed card if the writer gave one, else an earlier picture again, else a card from the line itself."""
    if (card := _card(beat)) and cards_left > 0:
        return card_visual(card)
    if reused := _reuse_of(visuals, number):
        return reused
    return card_visual(_line_card(beat))


def _cards(visuals: list) -> int:
    return sum(1 for v in visuals if v and v.get("source") == "card")


def pick(script: dict, research: dict, episode_id: str, keep: dict[int, dict] | None = None) -> list[dict]:
    """One visual per beat. Beats in keep (by number) keep the visual given; the others get a picture or a card."""
    beats = script["beats"]
    loop = _is_loop(script)
    last = len(beats)
    keep = dict(keep or {})
    numbers = [n for n in range(1, last + (0 if loop else 1)) if n not in keep]
    used = {v["_choice"]["key"] for v in keep.values() if v.get("_choice", {}).get("key")}
    visuals: list = [keep.get(n) for n in range(1, last + 1)]
    story = research.get("story", "")
    if numbers:
        board = _Board(beats, numbers, used)
        general = [v["queries"][0] for v in research.get("visuals", []) if v.get("queries")][:8]
        per_beat, pool = sources.gather(beats, numbers, _topic_articles(research), general)
        board.add(per_beat, pool)
        _thumbs([c for found in board.per_beat.values() for c in found] + board.pool)
        _rank(beats, numbers, [c for found in board.per_beat.values() for c in found] + board.pool)
        if weak := [n for n in numbers if board.best(n) < WEAK_MATCH]:
            seen = {c["key"] for found in board.per_beat.values() for c in found} | {c["key"] for c in board.pool} | used
            more = sources.extras(beats, weak, seen)
            board.add(more, [])
            _thumbs([c for found in more.values() for c in found])
            _rank(beats, numbers, [c for found in board.per_beat.values() for c in found])
        board.settle()
        shown, pool_shown = board.shown()
        labels, picks = _ask(beats, numbers, shown, pool_shown, story, f"{episode_id} images")
        chosen = _assign(numbers, labels, picks, used)
        checking = {n: c[0] for n, c in chosen.items()}
        for _ in range(CHECKS):
            flagged = _verify(beats, checking, story, f"{episode_id} image check")
            checking = {}
            for n in flagged:
                # The next choice not taken by another beat. After the last check it goes unchecked; the reviewer
                # sees every frame after the render.
                alternates = [c for c in chosen[n][1] if c["key"] not in used]
                used.discard(chosen[n][0]["key"])
                if alternates:
                    chosen[n] = (alternates[0], alternates[1:], {**picks.get(n, {}), "fit": "close", "box": [], "shows": ""})
                    used.add(alternates[0]["key"])
                    checking[n] = alternates[0]
                else:
                    del chosen[n]
        previous = None
        for n in range(1, last + 1):
            if n in keep or (loop and n == last):
                previous = (visuals[n - 1] or {}).get("motion", previous)
                continue
            beat = beats[n - 1]
            card = _card(beat)
            fit = chosen[n][2].get("fit") if n in chosen else "none"
            if n in chosen and not (fit == "scene" and card and n > 1 and _cards(visuals) < MAX_CARDS):
                candidate, alternates, choice = chosen[n]
                visual = picture_visual(candidate, n, previous, choice, card)
                visual["_alternates"] = [_kept(c, n) | {"thumb": c.get("thumb")} for c in alternates[:3]]
            else:
                visual = _fallback(beat, visuals, n, MAX_CARDS - _cards(visuals))
            visuals[n - 1] = visual
            previous = visual.get("motion")
    if loop:
        visuals[-1] = _loop_visual(visuals[0])
    log.info("%s: %s", episode_id, ", ".join(
        f"{n}:{'card' if v.get('source') == 'card' else 'reuse' if v.get('reuse') else v.get('_fit', 'kept')}"
        for n, v in enumerate(visuals, 1)))
    return visuals


def _assign(numbers: list[int], labels: dict[str, dict], picks: dict[int, dict], used: set[str]) -> dict[int, tuple]:
    """Each beat's first ranked candidate not already taken, best fits first: {beat: (choice, alternates, pick)}."""
    order = sorted(numbers, key=lambda n: (FITS.index(picks.get(n, {}).get("fit", "none")) if picks.get(n, {}).get("fit") in FITS else 3, n))
    chosen = {}
    for n in order:
        p = picks.get(n)
        if not p or p.get("fit") == "none":
            continue
        options = [labels[l] for l in p["ranked"] if labels[l]["key"] not in used]
        if not options:
            continue
        if options[0] is not labels[p["ranked"][0]]:
            # Its first choice went to another beat, so the framing the model gave doesn't apply.
            p = {**p, "fit": "close" if p.get("fit") == "exact" else p.get("fit"), "box": [], "focus_x": 0.5, "focus_y": 0.5, "shows": ""}
        used.add(options[0]["key"])
        chosen[n] = (options[0], options[1:], p)
    return chosen


def unchanged(old_script: dict, old_visuals: list[dict], new_script: dict, skip: set[int] = frozenset()) -> dict[int, dict]:
    """Visuals a rewrite can keep: a new beat whose line and wanted picture match an old beat's (other than the
    old beats in skip) keeps its picture."""
    by_line = {}
    for n, (beat, visual) in enumerate(zip(old_script.get("beats", []), old_visuals), start=1):
        if n in skip or not visual or visual.get("reuse"):
            continue
        if visual.get("_choice", {}).get("key") or visual.get("source") == "card":
            by_line.setdefault((beat["text"].strip(), (beat.get("visual") or "").strip()), visual)
    keep, taken = {}, set()
    for n, beat in enumerate(new_script.get("beats", []), start=1):
        visual = by_line.get((beat["text"].strip(), (beat.get("visual") or "").strip()))
        if visual and id(visual) not in taken:
            keep[n] = visual
            taken.add(id(visual))
    return keep


def replace(script: dict, research: dict, episode_id: str, visuals: list[dict], numbers: list[int], why: str = "") -> list[dict]:
    """New pictures for these beats after the review: the next ranked alternate, else a card or an earlier picture.

    Visuals from before the picker used URLs (no candidate keys) are picked again instead.
    """
    visuals = [dict(v) for v in visuals]
    beats = script["beats"]
    loop = _is_loop(script)
    numbers = sorted({1 if loop and n == len(beats) else n for n in numbers})
    if any(v.get("_choice") and "key" not in v["_choice"] for v in visuals):
        keep = {n: v for n, v in enumerate(visuals, 1) if n not in numbers and v.get("_choice", {}).get("key")}
        return pick(script, research, episode_id, keep=keep)
    rejected = {v["_choice"]["key"] for n, v in enumerate(visuals, 1) if n in numbers and v.get("_choice")}
    rejected |= {k for v in visuals for k in v.get("_rejected", [])}
    in_use = {v["_choice"]["key"] for n, v in enumerate(visuals, 1) if n not in numbers and v.get("_choice")}
    # A beat turned down again after its picture was replaced gets a title card: its alternates and an earlier
    # picture rarely do better. Of 20 Shorts rejected from ep038 to ep069, 13 failed on nothing but the visuals
    # score, 8 of them over a single beat (ep063's beat 3 and ep064's beat 4 in all three rounds).
    carded: list[int] = []
    for n in numbers:
        if (n > 1 and visuals[n - 1].get("_rejected") and visuals[n - 1].get("source") != "card"
                and len(carded) < MAX_CARDS - _cards(visuals)):
            carded.append(n)
    options = {n: [a for a in visuals[n - 1].get("_alternates", []) if a["key"] not in in_use | rejected]
               for n in numbers if visuals[n - 1].get("source") != "card" and n not in carded}
    rejected |= _checked(beats, options, research.get("story", ""), f"{episode_id} image recheck", why)
    for n in numbers:
        old = visuals[n - 1]
        beat = beats[n - 1]
        previous = visuals[n - 2].get("motion") if n > 1 else None
        alternates = [a for a in options.get(n, []) if a["key"] not in in_use]
        if n in carded:
            new = card_visual(_line_card(beat))
        elif alternates:
            new = picture_visual(alternates[0], n, previous, None, _card(beat))
            new["_alternates"] = alternates[1:]
            in_use.add(alternates[0]["key"])
        else:
            visuals[n - 1] = None
            new = _fallback(beat, visuals, n, MAX_CARDS - _cards(visuals))
            if new.get("source") == "card" and old.get("source") == "card":
                # The card itself was flagged: show an earlier picture instead.
                new = _reuse_of(visuals, n) or new
        new["_rejected"] = sorted(set(old.get("_rejected", [])) | rejected)
        visuals[n - 1] = new
    if loop:
        visuals[-1] = _loop_visual(visuals[0])
    log.info("%s: replaced beats %s (%s)", episode_id, numbers, why[:200])
    return visuals
