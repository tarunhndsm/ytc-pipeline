"""Paintings made for the lines of a Reel that no museum picture shows (owner, 20 Oct: when a source runs short,
switch to another). Judges failed Reels for the right figures in other episodes (visuals 4 to 6.5 with every other
score 8 or more), and a line with no picture at all made the picture step give up on the story.

Each painting comes from the line's own brief (reel_writer's "picture"), in the style of the Pahari miniatures most
lines show, from FLUX.2 [klein] 4B on Workers AI, else FLUX.1 [schnell] there (both Apache-2.0), on the owner's one
Cloudflare account: 10,000 neurons a day, renewed at 00:00 UTC and shared with the news desk; a painting costs about
132. It is hosted on Cloudinary like the Reels' videos, so every render, QA swap and rescue fetches it like a museum
picture, and it goes through the same framing check. Its credit says it was made with AI; the Reels already carry
the platforms' AI label (synthetic_media).
"""

from __future__ import annotations

import base64
import io
import logging
import os
import random
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image

from . import llm, publish

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 864, 1536
MODELS = {"@cf/black-forest-labs/flux-2-klein-4b": "FLUX.2 klein", "@cf/black-forest-labs/flux-1-schnell": "FLUX.1 schnell"}
# Paintings a picture try may make; a Reel has 12 to 15 lines.
MAX_PAINTED = 8
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
STYLE = ("Indian miniature painting in the 18th-century Pahari style: opaque watercolour on paper, fine brushwork, "
         "flat perspective, rich natural pigments, every figure fully clothed in traditional Indian dress, Hindu "
         "iconography as in the old paintings. Sages and ascetics have matted hair, beards, rudraksha beads and plain "
         "saffron or bark-cloth garments, never a crown or jewels. One clear scene filling a tall vertical picture; "
         "no text, letters, border or frame.")
# Line 1's picture is the Reel's first frame in the feed (reel_pictures.CHOOSE_PROMPT).
FIRST = "The figure large and close, vivid and colourful, clear at phone size. "


class OutOfNeurons(RuntimeError):
    """The Cloudflare account's free neurons for the day are spent (both models draw on them)."""


def ready() -> bool:
    return bool(os.environ.get("IG_CLOUDFLARE_ACCOUNT_ID") and os.environ.get("IG_CLOUDFLARE_API_TOKEN"))


def prompt(beat: dict, first: bool = False) -> str:
    return f"{beat['picture'].strip().rstrip('.')}. {FIRST if first else ''}{STYLE}"[:2000]


def _run(model: str, text: str, seed: int) -> tuple[bytes, float]:
    """The picture and the neurons it cost."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{os.environ['IG_CLOUDFLARE_ACCOUNT_ID']}/ai/run/{model}"
    headers = {"Authorization": f"Bearer {os.environ['IG_CLOUDFLARE_API_TOKEN']}"}
    if "klein" in model:
        fields = {"prompt": text, "width": WIDTH, "height": HEIGHT, "seed": seed}
        response = requests.post(url, headers=headers, files={k: (None, str(v)) for k, v in fields.items()}, timeout=180)
    else:
        response = requests.post(url, headers=headers, json={"prompt": text, "steps": 8, "seed": seed}, timeout=180)
    if response.status_code == 429 or (response.status_code >= 400 and "neuron" in response.text.lower()):
        raise OutOfNeurons(response.text[:300])
    response.raise_for_status()
    return base64.b64decode(response.json()["result"]["image"]), float(response.headers.get("cf-ai-neurons") or NEURONS_EACH)


def paint(reel: str, number: int, beat: dict) -> dict | None:
    """A painting for line `number`, hosted, as a hit like the museum searches give; None when no model made one."""
    text, seed = prompt(beat, number == 1), random.randrange(1 << 31)
    for model, name in MODELS.items():
        try:
            data, neurons = _run(model, text, seed)
            image = Image.open(io.BytesIO(data)).convert("RGB")
            llm.record({"model": f"cloudflare:{model}", "purpose": f"{reel} line {number} painting", "neurons": neurons})
            break
        except OutOfNeurons:
            raise
        except (requests.RequestException, KeyError, ValueError, OSError) as err:
            log.info("line %d: %s painted nothing (%s)", number, name, str(err)[:200])
    else:
        return None
    public_id = f"{CLOUDINARY_FOLDER}/{reel}-{number:02d}-{seed}"
    path = Path(tempfile.gettempdir()) / f"{reel}-{number:02d}-{seed}.jpg"
    image.save(path, "JPEG", quality=92)
    try:
        url = publish.host_image(path, public_id)
    finally:
        path.unlink(missing_ok=True)
    return {"ref": f"ai:{reel}-{number:02d}-{seed}", "title": f"AI painting: {beat['picture'][:160]}", "url": url,
            "page": url, "thumb": url, "source": SOURCE, "license": LICENSE, "artist": name, "width": image.width,
            "height": image.height}


def paint_lines(reel: str, beats: list[dict], numbers: list[int]) -> dict[int, dict]:
    """Paintings for up to MAX_PAINTED of `numbers` (most needed first), by line; fewer when the day's neurons run
    out or a model fails."""
    if not ready() or not numbers:
        return {}
    left = float(os.environ.get("YTC_PAINT_NEURONS") or DAILY_NEURONS)
    if not (todo := numbers[:min(MAX_PAINTED, int(left // NEURONS_EACH))]):
        log.info("no painting for lines %s: today's %d neurons are spent", numbers, DAILY_NEURONS)
        return {}

    def one(number: int) -> tuple[int, dict | None]:
        try:
            return number, paint(reel, number, beats[number - 1])
        except OutOfNeurons as err:
            log.warning("line %d: Cloudflare's free neurons for today are spent (%s)", number, str(err)[:160])
        except Exception as err:
            log.warning("line %d: no painting (%s: %s)", number, type(err).__name__, str(err)[:200])
        return number, None

    with ThreadPoolExecutor(3) as workers:
        made = {number: hit for number, hit in workers.map(one, todo) if hit}
    log.info("painted lines %s of %s", sorted(made), todo)
    return made
