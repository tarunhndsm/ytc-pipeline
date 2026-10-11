"""The pages of the primary texts the Reel writer reads, kept as plain text in content/instagram/sources/ once read.

sacred-texts.com and vedabase.io refuse CircleCI's addresses (HTTP 403) while they answer a laptop, so a page is read
from, in turn: its saved copy, the page itself, the Internet Archive's copy of it, and, for a Bhagavata chapter, the
same chapter on bhagavata.org. What reads is saved, and the factory pushes the folder with its other changes.

    python -m ytc.reel_sources fill      (save the pages of every backlog story the writer can read)
    python -m ytc.reel_sources check     (which routes answer from here; saves nothing)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

import requests

from . import reel_tools, reels, visuals

log = logging.getLogger(__name__)

SOURCES = reels.REELS / "sources"
# A page with less text than this didn't bring its chapter.
MIN_PAGE = 1500
# A refusal some hosts send with HTTP 200, which must not be saved as the text.
_REFUSED = re.compile(r"Just a moment\.\.\.|Enable JavaScript and cookies|Access denied|Attention Required|captcha",
                      re.I)
CHECKS = ("https://sacred-texts.com/hin/m01/m01056.htm", "https://vedabase.io/en/library/sb/9/4/",
          "https://www.valmikiramayan.net/utf8/sundara/sarga1/sundarasans1.htm")


def path_for(url: str) -> Path:
    return SOURCES / (re.sub(r"[^A-Za-z0-9.]+", "_", re.sub(r"^https?://(www\.)?", "", url)).strip("_") + ".txt")


def routes(url: str) -> list[str]:
    """Where the page can be read, in the order tried."""
    found = [url, f"https://web.archive.org/web/2024id_/{url}"]
    if m := re.fullmatch(r"https://vedabase\.io/en/library/sb/(\d+)/(\d+)/", url):
        found.append(f"https://bhagavata.org/canto{m[1]}/chapter{m[2]}.html")
    return found


def _try(route: str) -> tuple[str | None, str]:
    """The route's text, or None and why not."""
    try:
        response = visuals._get(route, timeout=40)
    except requests.RequestException as err:
        code = getattr(err.response, "status_code", None)
        return None, f"HTTP {code}" if code else type(err).__name__
    try:
        # valmikiramayan.net and sacred-texts send UTF-8 without saying so, which requests reads as Latin-1.
        text = reel_tools._text(response.content.decode("utf-8"))
    except UnicodeDecodeError:
        text = reel_tools._text(response.text)
    if _REFUSED.search(text[:3000]):
        return None, "a refusal page"
    if len(text) < MIN_PAGE:
        return None, f"only {len(text)} characters"
    return text, "ok"


def read(url: str) -> str:
    """The page's text: its saved copy, else the first route that reads, which is then saved."""
    saved = path_for(url)
    if saved.exists():
        return saved.read_text(encoding="utf-8")
    tried = []
    for route in routes(url):
        text, why = _try(route)
        if text:
            if route != url:
                log.info("read %s from %s", url, route)
            SOURCES.mkdir(parents=True, exist_ok=True)
            saved.write_text(text, encoding="utf-8")
            return text
        tried.append(f"{route.split('/')[2]}: {why}")
    raise LookupError(f"couldn't read {url} ({'; '.join(tried)})")


def check() -> dict:
    """For a page of each text, what every route answers from here."""
    return {url: {route.split("/")[2]: _try(route)[1] for route in routes(url)} for url in CHECKS}


def fill() -> dict:
    """Save the pages of every story in the backlog's Next and Set aside tables that the writer can read."""
    from . import reel_writer

    rows = reel_writer.backlog() + reel_writer.set_aside()
    found = {"saved": 0, "had": 0, "failed": []}
    for row in rows:
        for url in reel_writer.source_urls(row["source"]):
            if path_for(url).exists():
                found["had"] += 1
                continue
            try:
                read(url)
                found["saved"] += 1
            except LookupError as err:
                found["failed"].append(str(err)[:300])
    return found


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ytc.reel_sources", description="the writer's source pages")
    parser.add_argument("command", choices=("fill", "check"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    from .cli import PIPELINE_ROOT, _load_env

    _load_env(PIPELINE_ROOT / ".env")
    print(json.dumps(fill() if args.command == "fill" else check(), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
