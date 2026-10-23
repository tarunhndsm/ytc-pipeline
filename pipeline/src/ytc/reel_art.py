"""Paintings made for the lines of a Reel that no museum picture shows (owner, 20 Oct: when a source runs short,
switch to another). Judges failed Reels for the right figures in other episodes (visuals 4 to 6.5 with every other
score 8 or more), and a line with no picture at all made the picture step give up on the story.

Each painting comes from the line's own brief (reel_writer's "picture"), in the style of the Pahari miniatures most
lines show, from FLUX.2 [klein] 4B on Workers AI (Apache-2.0), on the owner's one Cloudflare account: 10,000 neurons
a day, renewed at 00:00 UTC and shared with the news desk; a painting costs about 132, a flagged one too. It is
hosted on Cloudinary like the Reels' videos, so every render, QA swap and rescue fetches it like a museum
picture, and it goes through the same framing check. Its credit says it was made with AI; the Reels already carry
the platforms' AI label (synthetic_media).

With CINEMATIC every line is painted, in the look of the niche's most-watched Shorts, and the museums aren't searched.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import random
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image

from . import llm, publish

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 864, 1536
# Not FLUX.1 [schnell]: it paints only 1,024 squares, which the picture step turns down (reel_pictures.MIN_HEIGHT).
MODELS = {"@cf/black-forest-labs/flux-2-klein-4b": "FLUX.2 klein"}
# Every line of a Reel gets a painting of its own (owner, 23 Oct: apply every strategy that makes Reels go viral). The
# 104 most-viewed Hindi mythology Shorts of 13 to 23 Oct are vivid, film-like paintings with close, emotional faces; ours,
# crops of old miniatures with small figures and cards on a blurred ground, held viewers 12 to 16 seconds, and no Reel
# since 13 Oct passed 5,000 plays. Off, paintings stand in only where the museums have no picture.
CINEMATIC = True
# Paintings a picture try may make; a Reel has 12 to 15 lines.
MAX_PAINTED = 15 if CINEMATIC else 8
# Ankahi's share of the account's 10,000 free neurons a day (UTC); the rest stays for the news desk's text fallback.
# The factory passes what is left of it to the step (YTC_PAINT_NEURONS). A painting at WIDTH x HEIGHT cost 131.88
# on 20 Oct (Cloudflare's cf-ai-neurons header).
DAILY_NEURONS = 6000
NEURONS_EACH = 132
# How the picture chooser rated its first choice for a line (reel_pictures.CHOOSE_SCHEMA "fit") when a painting
# should take its place: the right figure in another scene, or nothing that qualifies.
WEAK = ("elsewhere", "none")
SOURCE = "AI"
LICENSE = "AI-generated"
CLOUDINARY_FOLDER = "ankahikathayein/art"
# Not the museums' words for the paintings ("opaque watercolour on paper"): they bring the catalogue's margins with
# them, and FLUX writes a made-up caption there.
MINIATURE = ("Indian miniature painting in the Pahari style: fine brushwork, flat perspective, rich natural pigments, "
             "every figure fully clothed in traditional Indian dress, Hindu iconography as in the old paintings. Only "
             "the figures the brief names, each large and clear. The painted scene fills the whole tall vertical "
             "picture edge to edge, with no margin, border or frame, and no writing anywhere: no inscription, caption, "
             "label or signature.")
CINEMA = ("Cinematic digital painting, like a frame from an epic Indian mythological film: dramatic light with glowing "
          "golds and deep shadows, rich saturated colours, fine detail, expressive faces with clear emotion. Every "
          "figure fully clothed in traditional Indian dress and jewellery, Hindu iconography as in temple art. Only "
          "the figures the brief names, each large and clear. The scene fills the whole tall vertical picture edge to "
          "edge, with no margin, border or frame, and no writing anywhere: no text, letters, caption, label, logo or "
          "signature.")
STYLE = CINEMA if CINEMATIC else MINIATURE
# A brief written for the museums' pictures names a school of painting ("Pahari painting of ..."), which would undo
# CINEMA.
SCHOOL = re.compile(r"(?:\b(?:in|like)\s+)?(?:\b(?:the|an?)\s+)?(?:\b(?:traditional|classical|old)\s+)?\b"
                    r"(?:(?:Pahari|Rajasthani|Kangra|Mewar|Basohli|Guler|"
                    r"Mughal|Kalighat|Indian)\s+(?:miniature\s+)?|miniature\s+)(?:paintings?|folios?|style)"
                    r"(?:\s+(?:of|showing|depicting))?\b\s*", re.I)
# A clause of a brief that names a style ("traditional Indian art style", "classical painting"); reel_art.plain.
STYLED = re.compile(r"\b(?:style|miniature|folio)\b|\b(?:painting|art)\s*$", re.I)
# Line 1's picture is the Reel's first frame in the feed (reel_pictures.CHOOSE_PROMPT).
FIRST = ("A dramatic close-up: the main figure's face and shoulders fill most of the frame, the emotion unmistakable, "
         "vivid and striking at phone size. " if CINEMATIC else
         "The figure large and close, vivid and colourful, clear at phone size. ")
# How the old paintings show the figures a brief names, at most MAX_ICONS of them: the briefs give only the name, and
# FLUX painted Krishna pale and Yama as a sage (ak072, 20 Oct). Only for a brief that names them: said of every line,
# the sages filled every painting with a crowd of them (ak065).
ICONS = (
    (r"\b(sage|rishi|muni|ascetic|hermit|yogi|sadhu|maharishi|maharshi)s?\b|\bhermitage\b",
     "sages and ascetics have matted hair, beards, rudraksha beads and plain saffron or bark-cloth garments, never a "
     "crown or jewels"),
    # Their clothes are named: a bare blue torso got all four of ak073's paintings of Rama flagged (21 Oct).
    (r"\bkrishna\b", "Krishna has blue skin, a peacock feather in his crown, a yellow dhoti and a scarf over his "
                     "shoulders"),
    (r"\brama(chandra)?\b", "Rama has blue skin, a crown, a yellow dhoti and a long cloth across his chest, a bow and "
                            "a quiver"),
    (r"\bvishnu\b|\bnarayana\b", "Vishnu has blue skin, yellow robes and four arms holding a conch, a discus, a mace "
                                 "and a lotus"),
    (r"\bs(h)?iva\b|\bmahadeva\b", "Shiva has pale ash-smeared skin, matted locks with the crescent moon, a snake at "
                                   "his neck and a trident"),
    (r"\bbalarama\b", "Balarama has fair skin, blue garments and a plough"),
    # "A monkey god" came out as a bearded man or a real ape, flagged on most tries (ak087, 23 Oct).
    (r"\bhanuman(ji)?\b|\bhanumat\b|\bmaruti\b|\banjaneya\b|\bbajrang(bali)?\b",
     "Hanuman, the vanara god, has a monkey's face with golden-orange fur, a long tail, a golden crown and a mace, "
     "and wears a red dhoti and golden armour over his chest"),
    (r"\bganesh(a)?\b|\bganapati\b", "Ganesha has an elephant's head"),
    (r"\byama(raja?)?\b", "Yama, god of death, is dark-skinned and crowned, with a mace and a noose"),
    (r"\bbrahma\b", "Brahma has four bearded heads"),
    (r"\bindra\b", "Indra is crowned and rides the white elephant Airavata"),
    (r"\bdurga\b", "Durga rides a lion and holds weapons in many arms"),
    (r"\blakshmi\b", "Lakshmi sits on a lotus"),
    (r"\bsaraswati\b", "Saraswati holds a veena, a white swan beside her"),
    (r"\bnarada\b", "Narada is a sage carrying a veena"),
)
MAX_ICONS = 3
# Cloudflare's safety check flagged all four of ak073's paintings of Rama (21 Oct); a flagged painting is painted
# again with another seed, up to this many tries in all.
FLAGGED_TRIES = 2
# The usage-log key of a step's note that Cloudflare refused it for the day's neurons.
OUT_OF_NEURONS = "out_of_neurons"


class OutOfNeurons(RuntimeError):
    """The Cloudflare account's free neurons for the day are spent."""


class Refused(requests.RequestException):
    """Workers AI turned a painting down, with its message and not the URL, which names the account, and the neurons
    it charged for the call."""

    def __init__(self, message: str, neurons: float = 0.0) -> None:
        super().__init__(message)
        self.neurons = neurons


class Flagged(Refused):
    """Cloudflare's safety check flagged the painting that came out (code 3030); another seed can pass it."""


def ready() -> bool:
    return bool(os.environ.get("IG_CLOUDFLARE_ACCOUNT_ID") and os.environ.get("IG_CLOUDFLARE_API_TOKEN"))


def icons(brief: str) -> str:
    """How the old paintings show the figures `brief` names, in the order it names them."""
    found = sorted((match.start(), text) for pattern, text in ICONS if (match := re.search(pattern, brief, re.I)))
    return "".join(f"{text[0].upper()}{text[1:]}. " for _, text in found[:MAX_ICONS])


def plain(brief: str) -> str:
    """The brief without the school or style of painting it names, as a clause of its own ("..., traditional Indian
    art style") or in front ("Pahari painting of ...")."""
    brief = SCHOOL.sub("", brief).strip(" ,.")
    kept = [part.strip() for part in brief.split(",") if part.strip() and not STYLED.search(part)]
    found = ", ".join(kept) if kept else brief
    return found[:1].upper() + found[1:]


def prompt(beat: dict, first: bool = False) -> str:
    brief = beat["picture"].strip().rstrip(".")
    if CINEMATIC and plain(brief):
        brief = plain(brief)
    return f"{brief}. {FIRST if first else ''}{icons(brief)}{STYLE}"[:2000]


def _run(model: str, text: str, seed: int) -> tuple[bytes, float]:
    """The picture and the neurons it cost."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{os.environ['IG_CLOUDFLARE_ACCOUNT_ID']}/ai/run/{model}"
    headers = {"Authorization": f"Bearer {os.environ['IG_CLOUDFLARE_API_TOKEN']}"}
    fields = {"prompt": text, "width": WIDTH, "height": HEIGHT, "seed": seed}
    response = requests.post(url, headers=headers, files={k: (None, str(v)) for k, v in fields.items()}, timeout=180)
    if response.status_code == 429 or (response.status_code >= 400 and "neuron" in response.text.lower()):
        raise OutOfNeurons(response.text[:300])
    if response.status_code >= 400:
        try:
            error = response.json()["errors"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            error = {"message": response.text}
        refused = Flagged if error.get("code") == 3030 else Refused
        # A flagged painting was made, and charged: on 20 Oct the account ran out with 6,373 neurons counted.
        charged = response.headers.get("cf-ai-neurons") or (NEURONS_EACH if refused is Flagged else 0)
        raise refused(f"HTTP {response.status_code}: {str(error.get('message'))[:200]}", float(charged))
    return base64.b64decode(response.json()["result"]["image"]), float(response.headers.get("cf-ai-neurons") or NEURONS_EACH)


def _paint_with(model: str, name: str, text: str, number: int) -> tuple[Image.Image | None, int, float]:
    """A painting by `model` (None when it made none), its seed, and the neurons spent on it, flagged tries included;
    a flagged painting is painted again with another seed."""
    spent = 0.0
    for _ in range(FLAGGED_TRIES):
        seed = random.randrange(1 << 31)
        try:
            data, neurons = _run(model, text, seed)
            return Image.open(io.BytesIO(data)).convert("RGB"), seed, spent + neurons
        except OutOfNeurons:
            raise
        except Flagged as err:
            spent += err.neurons
            log.info("line %d: %s's painting was flagged (%s)", number, name, err)
        except (requests.RequestException, KeyError, ValueError, OSError) as err:
            spent += getattr(err, "neurons", 0.0)
            log.info("line %d: %s painted nothing (%s)", number, name, str(err)[:200])
            break
    return None, 0, spent


def paint(reel: str, number: int, beat: dict) -> tuple[dict | None, float]:
    """A painting for line `number`, hosted, as a hit like the museum searches give (None when no model made one),
    and the neurons spent on the line, flagged paintings included."""
    text, spent = prompt(beat, number == 1), 0.0
    for model, name in MODELS.items():
        image, seed, neurons = _paint_with(model, name, text, number)
        if neurons:
            llm.record({"model": f"cloudflare:{model}", "purpose": f"{reel} line {number} painting", "neurons": neurons})
        spent += neurons
        if image:
            break
    else:
        return None, spent
    public_id = f"{CLOUDINARY_FOLDER}/{reel}-{number:02d}-{seed}"
    path = Path(tempfile.gettempdir()) / f"{reel}-{number:02d}-{seed}.jpg"
    image.save(path, "JPEG", quality=92)
    try:
        url = publish.host_image(path, public_id)
    finally:
        path.unlink(missing_ok=True)
    return {"ref": f"ai:{reel}-{number:02d}-{seed}", "title": f"AI painting: {beat['picture'][:160]}", "url": url,
            "page": url, "thumb": url, "source": SOURCE, "license": LICENSE, "artist": name, "width": image.width,
            "height": image.height}, spent


def paint_lines(reel: str, beats: list[dict], numbers: list[int]) -> dict[int, dict]:
    """Paintings for up to MAX_PAINTED of `numbers` (most needed first), by line; fewer when the day's neurons run
    out or a model fails. What they cost comes off YTC_PAINT_NEURONS, for the step's next call."""
    if not ready() or not numbers:
        return {}
    left = float(os.environ.get("YTC_PAINT_NEURONS") or DAILY_NEURONS)
    if not (todo := numbers[:min(MAX_PAINTED, int(left // NEURONS_EACH))]):
        log.info("no painting for lines %s: today's %d neurons are spent", numbers, DAILY_NEURONS)
        return {}

    refused: list[int] = []

    def one(number: int) -> tuple[int, dict | None, float]:
        try:
            return number, *paint(reel, number, beats[number - 1])
        except OutOfNeurons as err:
            refused.append(number)
            log.warning("line %d: Cloudflare's free neurons for today are spent (%s)", number, str(err)[:160])
        except Exception as err:
            log.warning("line %d: no painting (%s: %s)", number, type(err).__name__, str(err)[:200])
        return number, None, 0.0

    with ThreadPoolExecutor(3) as workers:
        done = list(workers.map(one, todo))
    made = {number: hit for number, hit, _ in done if hit}
    left -= sum(spent for _, _, spent in done)
    if refused:
        # Cloudflare's count says when the day is over, not ours: on 20 Oct it stopped at 6,373 of ours, and it can
        # go on refusing past 00:00 UTC. reel_factory takes this line as the end of the day's share.
        llm.record({"model": "cloudflare:refused", "purpose": f"{reel}: Cloudflare refused paintings for the day's "
                    "neurons", OUT_OF_NEURONS: True})
        left = 0.0
    os.environ["YTC_PAINT_NEURONS"] = str(max(0.0, left))
    log.info("painted lines %s of %s", sorted(made), todo)
    return made
