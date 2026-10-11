"""Keep the story backlog from running dry with no one at the keyboard: when few stories the writer can read are left,
Gemini proposes new ones from the three texts the writer reads, and a story joins the end of "## Next" only when its
source parses, its pages read (reel_sources, which saves them), the names it gives are in those pages, and it isn't a
story already made or planned.

    python -m ytc.reel_backlog refill [--count N] [--dry]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import unicodedata

from . import llm, reel_sources, reel_writer

log = logging.getLogger(__name__)

MAX_PAGES = 8
# A proposed story is a repeat when this share of its title's words are in one title already made or planned.
SAME_TITLE = 0.6
SCHEMA = {
    "type": "object",
    "properties": {"stories": {"type": "array", "items": {"type": "object", "properties": {
        "story": {"type": "string"},
        "hook": {"type": "string"},
        "source": {"type": "string"},
        "picture": {"type": "string"},
        "care": {"type": "string"},
        "names": {"type": "array", "items": {"type": "string"}},
    }, "required": ["story", "hook", "source", "picture", "care", "names"]}}},
    "required": ["stories"],
}
PROMPT = """You plan stories for अनकही कथाएँ (@ankahi.kathayein), an Instagram and YouTube page of lesser-known, sourced
stories from the Ramayana, the Mahabharata and the Puranas, told in Hindi in one-minute Reels to Indian families.

Propose {count} new stories, mixing the three texts. Each must:
- come from one of these translations, with its chapters written exactly in this form, because a program fetches
  them and reads the story there before a word is written:
  - "Mahabharata, <Book> Parva <first>-<last>" in Kisari Mohan Ganguli's section numbers (Adi, Sabha, Vana, Virata,
    Udyoga, Bhishma, Drona, Karna, Shalya, Sauptika, Stri, Santi, Anusasana, Aswamedha, Asramavasika, Mausala,
    Mahaprasthanika or Svargarohanika);
  - "Valmiki Ramayana, <Book> Kanda <first>-<last>" (Bala, Ayodhya, Aranya, Kishkindha, Sundara or Yuddha; not the
    Uttara Kanda);
  - "Bhagavata Purana <canto>.<first>-<last>" (chapters of one canto);
  giving only the chapters that tell the story, at most six;
- be a story most viewers don't know, or a surprising fact from the text about a figure every Hindi viewer knows:
  Reels about well-known figures draw a hundred times the plays of the rest;
- end on an uplifting or meaningful note: nothing political, nothing comparing castes or communities, and no story
  whose point is violence or death;
- not be one of the stories below, already made or planned, nor another telling of one.

For each give: story, a short English title; hook, the opening line in natural spoken Hindi (Devanagari, at most 14
words) that would stop a devotee scrolling; source, as above; picture, the kind of old Indian painting that could
show it, or ""; care, what to leave out or tell gently, or ""; and names, two or three names of people or beings in
the story as they would be spelled in the English translation of those chapters (for example "Ambarisha",
"Durvasa").

Already made or planned:
{known}
"""


def _fold(text: str) -> str:
    """Letters only, spelled one way, so that "Ambarīṣa" and "Ambarisha", "Kṛṣṇa" and "Krishna", "Seetha" and "Sita",
    or "Viswamitra" and "Vishvamitra" match."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if "a" <= c <= "z").replace("h", "").replace("w", "v")
    text = text.replace("ee", "i").replace("oo", "u").replace("ri", "r")
    return re.sub(r"([aeiou])\1+", r"\1", text)


def _words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", title.lower()) if len(w) > 3}


def known() -> list[dict]:
    """Every story made, set aside or waiting in the backlog, as story and source."""
    text = reel_writer.BACKLOG.read_text(encoding="utf-8")
    made = []
    for line in text.split("\n## Made", 1)[-1].split("\n## ", 1)[0].splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if re.fullmatch(r"ak\d+", cells[0]) and len(cells) >= 3:
            made.append({"story": cells[1], "source": cells[2]})
    return made + reel_writer.set_aside() + reel_writer.backlog()


def repeat_of(row: dict, stories: list[dict]) -> str | None:
    """The known story this proposal repeats, if any: most of its title's words, or all of its names, in one title,
    or two of its title's words in the title of a story told from the same chapters."""
    words, names = _words(row["story"]), [_fold(n) for n in row["names"] if _fold(n)]
    pages = set(reel_writer.source_urls(row["source"]))
    for other in stories:
        theirs, folded = _words(other["story"]), _fold(other["story"])
        if words and len(words & theirs) >= SAME_TITLE * len(words):
            return other["story"]
        if names and all(n[:5] in folded for n in names):
            return other["story"]
        if len(words & theirs) >= 2 and pages & set(reel_writer.source_urls(other["source"])):
            return other["story"]
    return None


def check(row: dict, stories: list[dict]) -> str | None:
    """Why a proposed story can't join the backlog, or None when it can (its pages are then saved)."""
    if not row["story"].strip() or not row["hook"].strip():
        return "no title or hook"
    urls = reel_writer.source_urls(row["source"])
    if not urls:
        return f"the writer can't read {row['source']!r}"
    if len(urls) > MAX_PAGES:
        return f"{len(urls)} chapters"
    if other := repeat_of(row, stories):
        return f"repeats {other!r}"
    try:
        pages = _fold(" ".join(reel_sources.read(url) for url in urls))
    except LookupError as err:
        return str(err)[:200]
    names = [n for n in (_fold(n) for n in row["names"]) if len(n) >= 3]
    found = [n for n in names if n[:5] in pages]
    if len(names) < 2 or len(found) < len(names):
        return f"names not in those chapters: {sorted(set(names) - set(found))}"
    return None


def _cell(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("|", "/")).strip()


def append(rows: list[dict]) -> None:
    """Add rows at the end of the Next table."""
    lines = reel_writer.BACKLOG.read_text(encoding="utf-8").rstrip("\n").split("\n")
    nxt = lines.index("## Next")
    last = max(i for i in range(nxt, len(lines)) if lines[i].startswith("|"))
    for k, row in enumerate(rows, start=1):
        cells = [_cell(row[c]) for c in reel_writer.COLUMNS]
        lines.insert(last + k, "| " + " | ".join(cells) + " |")
    reel_writer.BACKLOG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def refill(count: int = 10, dry: bool = False) -> dict:
    """Ask for `count` stories and add the ones that check out; what was added and what was turned down."""
    stories = known()
    listing = "\n".join(f"- {s['story']} ({s['source']})" for s in stories)
    answer = llm.generate(PROMPT.format(count=count, known=listing), schema=SCHEMA, models=reel_writer.SCRIPT_MODELS,
                          purpose="backlog refill")
    added, turned_down = [], {}
    for row in answer.get("stories", []):
        row = {k: str(row.get(k, "")).strip() for k in ("story", "hook", "source", "picture", "care")} | \
            {"names": [str(n) for n in row.get("names") or []]}
        if why := check(row, stories + added):
            turned_down[row["story"][:80]] = why
            log.info("turned down %r: %s", row["story"], why)
        else:
            added.append(row)
    if added and not dry:
        append(added)
    return {"added": [f"{r['story']} ({r['source']})" for r in added], "turned_down": turned_down}


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ytc.reel_backlog", description="refill the story backlog")
    parser.add_argument("command", choices=("refill",))
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--dry", action="store_true", help="check proposals without adding them")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    from .cli import PIPELINE_ROOT, _load_env

    _load_env(PIPELINE_ROOT / ".env")
    llm.use_brand_project()
    print(json.dumps(refill(args.count, args.dry), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
