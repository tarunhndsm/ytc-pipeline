"""Write the next Reel's script from the story backlog with no one at the keyboard: the story's chapters in a
primary-text translation, then Hindi narration with a brief for every line's picture, held to the rules the reviewer
scores (reels.GATE) before any picture is looked for.

    python -m ytc.reel_writer next                  the next story it can source, and the pages it reads
    python -m ytc.reel_writer write [--id akNNN]    write that story's script into <id>/work/script.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import re
from pathlib import Path

import yaml

from . import captions, llm, reel_tools, reels, visuals
from .spec import ShortSpec

log = logging.getLogger(__name__)

BACKLOG = reels.REELS / "backlog.md"
COLUMNS = ("story", "hook", "source", "picture", "care")
SCRIPT = "work/script.json"
# The brand project's Flash models except the judge's, whose daily quota the reviews need, and gemini-flash-latest,
# which may be one of the others under another name.
WRITERS = tuple(m for m in llm.FLASH if m not in (reels.JUDGE, "gemini-flash-latest"))
# Once those are spent, the same project's Flash Lite; Gemma only picks and frames pictures, since a weak script
# costs one of the day's few good voice takes and Gemma got a script's facts wrong where Flash Lite didn't.
SCRIPT_MODELS = (*WRITERS, *llm.FLASH_LITE)
PICTURE_MODELS = (*SCRIPT_MODELS, *llm.GEMMA)
# The narrator speaks 2.4 to 2.9 words a second: about a minute, with room for the story to finish.
BEATS = (11, 14)
WORDS = (125, 150)
LINE_WORDS = (4, 16)
# Line 1 is said in about 3 seconds: on YouTube, half to three quarters of viewers swiped away in the first seconds
# of Reels whose line 1 ran 11 to 15 words (1 Oct); "रावण का असली नाम… रावण नहीं था।" (6) drew 65.5k plays.
HOOK_WORDS = 10
# The first word comes this soon (seconds): a silent start loses viewers before the hook is heard.
LEAD_IN = 0.1
# The hook's on-screen question, as large as two lines can be in reel_pictures.HOOK_REACH.
HOOK_SIZE = 148
# After the last word the picture holds and the music fades out, so the story ends rather than stops.
TAIL, MUSIC_FADE = 1.6, 2.0
FIX_ROUNDS = 2
# A page with less text than this didn't bring its chapter.
MIN_PAGE = 1500
HASHTAGS = ("#sanatandharma", "#ankahikathayein")

GANGULI_BOOKS = {"adi": 1, "sabha": 2, "vana": 3, "virata": 4, "udyoga": 5, "bhishma": 6, "drona": 7, "karna": 8,
                 "shalya": 9, "salya": 9, "sauptika": 10, "stri": 11, "santi": 12, "shanti": 12, "anusasana": 13,
                 "anushasana": 13, "aswamedha": 14, "ashvamedhika": 14, "asramavasika": 15, "mausala": 16,
                 "mahaprasthanika": 17, "svargarohanika": 18}
# valmikiramayan.net's folder and file stem for each book. It has no Uttara Kanda.
KANDAS = {"bala": ("baala", "bala"), "ayodhya": ("ayodhya", "ayodhya"), "aranya": ("aranya", "aranya"),
          "kishkindha": ("kish", "kishkindha"), "sundara": ("sundara", "sundara"), "yuddha": ("yuddha", "yuddha")}
# "20-34", "145, 146 and 150", or "6.22-23" (verses of one chapter).
_CHAPTERS = r"(\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?(?:\s*(?:,|\band\b)\s*\d+(?:\s*[-–]\s*\d+)?)*)"

DIRECTION = ("Narrate the transcript below in Hindi as a warm, captivating storyteller from North India telling a "
             "story from the {text} to friends on Instagram. Natural, conversational Hindi with authentic native "
             "pronunciation, not a formal news-reader voice. Brisk, confident pace; short dramatic pauses where you "
             "see '…'; {tones}. Speak only the transcript, nothing else.")

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "hook_text": {"type": "string"},
        "beats": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"},
            "emphasis": {"type": "string"},
            "sfx": {"type": "string", "enum": ["whoosh", "pop", "riser", "none"]},
            "picture": {"type": "string"},
            "search": {"type": "array", "items": {"type": "string"}},
        }, "required": ["text", "emphasis", "sfx", "picture", "search"]}},
        "summary": {"type": "string"},
        "teaser": {"type": "string"},
        "source_line": {"type": "string"},
        "tones": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "hook_text", "beats", "summary", "teaser", "source_line", "tones", "hashtags", "tags"],
}

PROMPT = """You write Reels for अनकही कथाएँ (@ankahi.kathayein), an Instagram page of lesser-known, sourced stories
from the Ramayana, the Mahabharata and the Puranas, told in Hindi to Indian families. A demanding editor scores every
Reel from 1 to 10 on its hook, retention, shareability, payoff, ending, narration, visuals and accuracy, and it is
published only if it scores at least as well as the Reel before it. Write the next one.

The story: {story}
Its hook, to open with (sharpen it if you can): {hook}
Source: {source}
{care}
The narration, as beats (one line of narration each):
- {beats_min} to {beats_max} lines, {words_min} to {words_max} words in all, so the Reel runs about a minute; never
  cut the story short to fit. Each line is one breath of {line_min} to {line_max} words.
- Line 1 is the hook: in under 2 seconds it must stop a devotee scrolling past, with a startling fact or question
  from this story, not a greeting or a summary. At most {hook_words} words, naming the story's best-known figure:
  "रावण का असली नाम… रावण नहीं था।" drew 65,000 plays, while openings of 11 to 15 words lost most viewers.
- Every line makes the viewer want the next: no background they don't need, no repetition, no sermon.
- The last few lines pay off the hook with a real revelation from the source.
- The last line ends the story: a complete sentence that settles the hook, so the Reel feels finished to someone
  who watches it once. Never end on an unfinished thought, a trailing "…" or a question. A Reel whose line 1 was
  "सूर्य देव के सारथी के पैर ही नहीं हैं… एक माँ की जल्दबाज़ी की वजह से।" ended with "अगली बार वो लाली देखो, तो
  याद रखना… वो बिना पैरों वाले सारथी हैं।"
- Natural spoken Hindi, the way a warm North Indian storyteller talks to friends: simple everyday words, not
  Sanskritized or formal, all in Devanagari with no Latin letters. Write numbers as words (पाँच सौ). Use "…" for a
  dramatic pause, at most once a line. Speak of deities and sages with respect (शिव जी, ब्रह्मा जी, सीता माँ), and
  never call a sage, a parent or an elder foolish or ignorant.
- Every fact comes from the source text below: add no event, name or number it doesn't have. When a detail is
  only in the popular text or later tradition, as the source or the care note says, say so in a few words.
- No politics, no comparing communities or castes, nothing that would offend a devotee. Violence, death or injury
  get a few words, never detail, and only where the story turns uplifting.
- emphasis: the one word of the line, exactly as written in it, that carries its meaning. It is highlighted on
  screen.
- sfx: "whoosh" on the one or two lines where the story turns, "pop" on a sudden act (something breaks or
  appears), "riser" on at most one line, the one that leads into the revelation, "none" otherwise. The last line
  ends calmly, never on a riser.

The pictures (they are chosen later from what you write):
- Every line gets a picture of its own; only the last line shows line 1's picture again.
- picture: in English, exactly what the line's picture should show: the person, being, place or object the line
  names, at that moment of the story (a child, if the line is about his childhood). The pictures come from museum
  collections of Indian paintings and sculpture (the Cleveland Museum of Art, the Met, LACMA, the Freer, the
  Wellcome Collection) and from Wikimedia Commons, so describe what such a picture would show, and nothing about
  effects, light or motion. Never a photograph: a photo among the paintings looks out of place, so a river, a
  mountain, a plant, an animal or a temple is as Indian art shows it (the Ganga in a Pahari painting). Line 1's
  picture is the first frame in the feed: the figure line 1 names, in a vivid, colourful painting, never a grey
  sculpture, a landscape, a plant or an animal on its own. For the last line, write "line 1" and give no search
  terms.
- search: 2 to 4 short English search terms, each what you would type into a museum's search box to find that
  picture. The first is the name of whoever or whatever the line shows, as museums spell it (Krishna, Brahma,
  Garuda, Ashtavakra), since a painting of this very story may exist; then the scene (Janaka court) or the
  painting's tradition (Bhagavata Purana, Pahari, Kangra, Kalighat). Never a museum's name or a bare word like
  "sage" or "sculpture".{picture}

For the post:
- title: a Hindi curiosity question of at most 8 words, like "सूर्य देव के सारथी के पैर क्यों नहीं हैं?"
- hook_text: at most 6 Hindi words shown on screen over line 1, adding to the spoken hook, not repeating it.
- summary: the caption's paragraph in Hindi, 4 to 7 sentences, starting with the text's name and "की कथा:", and
  telling the story as the source does.
- teaser: one short Hindi line after it, a thought to leave the reader with.
- source_line: the source in Hindi, with the book, chapters and translation, like "महाभारत, आदि पर्व, खंड 16 और
  24, किसारी मोहन गांगुली का अनुवाद".
- tones: in English, how the narrator's tone moves through the Reel, as short phrases for the voice actor, like
  "a knowing, intriguing tone in the first line; restless suspense in the lines that follow; rising awe towards
  the end; gentle wonder in the last line". Tone only, no events.
- hashtags: 5 in lower case: 3 about the story's deities, people or text, then {hashtags}.
- tags: 6 to 10 English keywords: names, the text, the book.

What the reviewer asked of the last Reels, for next time. Effects, captions and editing are done later, not by
you: follow what is about the story, the words and what the pictures show.
{lessons}

The narration of the best-reviewed Reel so far, for the voice and the pace (not its story):
{example}

The source text:
{sources}
"""

FIX = """That script breaks these rules:
{problems}
Write it again, fixing them and keeping the rest as it is."""


def _roman(numeral: str) -> int:
    values = [{"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}[c] for c in numeral.upper()]
    return sum(-v if v < after else v for v, after in zip(values, values[1:] + [0]))


def _numbers(text: str) -> list[int]:
    """Chapter numbers: "20-34", "145, 146 and 150", or "6.22-23" (verses of one chapter, so just 6)."""
    if m := re.match(r"\s*(\d+)\.\d", text):
        return [int(m[1])]
    found: list[int] = []
    for part in re.split(r",|\band\b", text):
        if m := re.fullmatch(r"\s*(\d+)\s*[-–]\s*(\d+)\s*", part):
            found += range(int(m[1]), int(m[2]) + 1)
        elif m := re.fullmatch(r"\s*(\d+)\s*", part):
            found.append(int(m[1]))
    return found


def _ganguli(book: int, section: int) -> str:
    return f"https://sacred-texts.com/hin/m{book:02d}/m{book:02d}{section:03d}.htm"


def _valmiki(kanda: str, sarga: int) -> str:
    folder, stem = KANDAS[kanda]
    return f"https://www.valmikiramayan.net/utf8/{folder}/sarga{sarga}/{stem}sans{sarga}.htm"


def source_urls(source: str) -> list[str]:
    """The pages of a primary-text translation for a backlog row's source (its first text, before any ";"), or []
    when it isn't in one this reads: Ganguli's Mahabharata on sacred-texts, the Valmiki Ramayana on valmikiramayan.net
    (without the Uttara Kanda), or the Bhagavata Purana on vedabase."""
    first = source.split(";")[0]
    if m := re.search(r"Ganguli\s+([IVXLC]+)\.\s*" + _CHAPTERS, first):
        return [_ganguli(_roman(m[1]), n) for n in _numbers(m[2])]
    if m := re.search(r"Mahabharata,\s*([A-Za-z]+)\s+Parva\s+" + _CHAPTERS, first):
        book = GANGULI_BOOKS.get(m[1].lower())
        return [_ganguli(book, n) for n in _numbers(m[2])] if book else []
    if first.startswith("Valmiki Ramayana"):
        parts = re.findall(r"([A-Z][a-z]+)\s+Kanda\s+" + _CHAPTERS, re.sub(r"\([^)]*\)", "", first))
        if not parts or any(kanda.lower() not in KANDAS for kanda, _ in parts):
            return []
        return [_valmiki(kanda.lower(), n) for kanda, numbers in parts for n in _numbers(numbers)]
    if m := re.search(r"Bhagavata Purana\s+(\d+)\.(\d+)(?:\s*[-–]\s*(?:(\d+)\.)?(\d+))?", first):
        canto, start = int(m[1]), int(m[2])
        if m[3] and int(m[3]) != canto:
            return []
        return [f"https://vedabase.io/en/library/sb/{canto}/{n}/" for n in range(start, int(m[4] or start) + 1)]
    return []


def backlog() -> list[dict]:
    """The rows of the backlog's Next table, in order."""
    rows = []
    for line in BACKLOG.read_text(encoding="utf-8").split("\n## Next", 1)[1].splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) >= len(COLUMNS) and cells[0] != "Story" and set(cells[0]) - set("-: "):
            rows.append(dict(zip(COLUMNS, cells)))
    return rows


def next_story() -> tuple[dict, list[str]] | None:
    """The first story in the backlog whose source this can read, with its pages."""
    for row in backlog():
        if urls := source_urls(row["source"]):
            return row, urls
    return None


def next_id() -> str:
    numbers = [int(m[1]) for p in reels.REELS.glob("ak*") if (m := re.fullmatch(r"ak(\d+)", p.name))]
    return f"ak{max(numbers, default=0) + 1:03d}"


def read_sources(urls: list[str]) -> list[dict]:
    """Each page's text. A page that doesn't load its chapter raises, since the script needs all of them."""
    pages = []
    for url in urls:
        text = reel_tools._text(visuals._get(url, timeout=40).text)
        if len(text) < MIN_PAGE:
            raise LookupError(f"{url} has only {len(text)} characters of text")
        pages.append({"url": url, "text": text})
    return pages


def _reviewed() -> list[tuple[Path, dict]]:
    return [(f, v) for f in sorted(reels.REELS.glob("ak*")) if (v := reels._read(f / reels.REVIEW))]


def lessons(count: int = 4) -> str:
    """What the reviews of the last `count` reviewed Reels said to do next time."""
    notes: list[str] = []
    for _, verdict in _reviewed()[-count:]:
        for run in verdict.get("runs") or [verdict]:
            if (note := (run.get("next_time") or "").strip()) and note not in notes:
                notes.append(note)
    return "\n".join(f"- {note}" for note in notes) or "- nothing yet"


def example() -> str:
    """The narration of the Reel with the best review so far, less a last line that trails off ("…"), which no
    ending may copy."""
    reviewed = _reviewed()
    if not reviewed:
        return "(none yet)"
    folder, _ = max(reviewed, key=lambda found: reels.total(found[1]))
    lines = [beat.text for beat in ShortSpec.load(folder / "short.yaml").beats]
    if lines[-1].rstrip().endswith("…"):
        lines.pop()
    return "\n".join(lines)


_NOT_A_SEARCH = re.compile(r"\b(met|lacma|cleveland|freer|wellcome|museum|gallery|collection|commons)\b", re.I)
_EFFECT = re.compile(r"\b(glow\w*|particles?|aura|overlay|light beam|light effect|flicker\w*|shimmer\w*|sparkl\w*)\b", re.I)


def _words(text: str) -> list[str]:
    return [w for w in (captions._key(w) for w in text.split()) if w]


def problems(script: dict) -> list[str]:
    """The rules a script breaks, each as a line the writer is shown."""
    found = []
    beats = script["beats"]
    if not BEATS[0] <= len(beats) <= BEATS[1]:
        found.append(f"It has {len(beats)} lines; write {BEATS[0]} to {BEATS[1]}.")
    if not WORDS[0] <= (words := sum(len(_words(b["text"])) for b in beats)) <= WORDS[1]:
        found.append(f"It has {words} words in all; write {WORDS[0]} to {WORDS[1]}.")
    for number, beat in enumerate(beats, start=1):
        if not LINE_WORDS[0] <= (count := len(_words(beat["text"]))) <= LINE_WORDS[1]:
            found.append(f"Line {number} has {count} words; a line has {LINE_WORDS[0]} to {LINE_WORDS[1]}.")
        elif number == 1 and count > HOOK_WORDS:
            found.append(f"Line 1 has {count} words; the hook has at most {HOOK_WORDS}, said in about 3 seconds.")
        if re.search(r"[A-Za-z0-9]", beat["text"]):
            found.append(f"Line {number} has Latin letters or digits; write it all in Devanagari, numbers as words.")
        if captions._key(beat["emphasis"]) not in _words(beat["text"]):
            found.append(f"Line {number}'s emphasis \"{beat['emphasis']}\" isn't one of its words.")
        if number == len(beats):
            if beat["sfx"] == "riser":
                found.append(f"Line {number}: the last line ends calmly; put \"riser\" on the line before the revelation.")
            if beat["text"].rstrip().endswith(("…", "...", "?", ",")):
                found.append(f"Line {number}: the last line must finish the story in a complete sentence.")
            continue
        if not 1 <= len(beat["search"]) <= 4 or not beat["picture"].strip():
            found.append(f"Line {number} needs a picture brief and 1 to 4 search terms.")
        if bad := [t for t in beat["search"] if _NOT_A_SEARCH.search(t)]:
            found.append(f"Line {number}'s search terms {bad} name a museum; give only names, scenes and traditions.")
        if _EFFECT.search(beat["picture"]):
            found.append(f"Line {number}'s picture brief asks for an effect; say only what the picture shows.")
    if sum(beat["sfx"] == "whoosh" for beat in beats) > 2:
        found.append("Use \"whoosh\" on at most 2 lines.")
    if sum(beat["sfx"] == "riser" for beat in beats) > 1:
        found.append("Use \"riser\" on at most 1 line.")
    if len(script["hook_text"].split()) > 6:
        found.append("hook_text has more than 6 words.")
    if len(script["title"].split()) > 9:
        found.append("The title has more than 8 words.")
    tags = [t.lower() for t in script["hashtags"]]
    if len(tags) != 5 or not all(t.startswith("#") for t in tags) or not all(t in tags for t in HASHTAGS):
        found.append(f"hashtags must be 5, each starting with #, including {' and '.join(HASHTAGS)}.")
    return found


def _hashtags(tags: list[str]) -> list[str]:
    """The story's own hashtags, then HASHTAGS: five in all when the writer gave at least three of its own. Writers
    drop a "#", repeat one or add a sixth, which costs a whole rewrite to fix."""
    story = []
    for tag in tags:
        tag = "#" + re.sub(r"[\s#]+", "", tag).lower()
        if len(tag) > 1 and tag not in story and tag not in HASHTAGS:
            story.append(tag)
    return story[:5 - len(HASHTAGS)] + list(HASHTAGS)


def _tidy(script: dict) -> dict:
    """What the code can fix without a rewrite: the hashtags, and search terms naming a museum (a writer copies the
    backlog's "Cleveland 1965.335" into them); a line left with no terms still goes back to the writer."""
    script["hashtags"] = _hashtags(script["hashtags"])
    for beat in script["beats"]:
        beat["search"] = [term for term in beat.get("search") or [] if not _NOT_A_SEARCH.search(term)]
    return script


def _prompt(row: dict, pages: list[dict]) -> str:
    care = f"Care: {row['care']}\n" if row["care"] else ""
    picture = (f"\n- The backlog suggests this picture; use it for the line it fits: {row['picture']}"
               if row["picture"] else "")
    sources = "\n\n".join(f"[{page['url']}]\n{page['text']}" for page in pages)
    return PROMPT.format(story=row["story"], hook=row["hook"], source=row["source"], care=care, picture=picture,
                         beats_min=BEATS[0], beats_max=BEATS[1], words_min=WORDS[0], words_max=WORDS[1],
                         line_min=LINE_WORDS[0], line_max=LINE_WORDS[1], hook_words=HOOK_WORDS,
                         hashtags=" and ".join(HASHTAGS),
                         lessons=lessons(), example=example(), sources=sources)


@contextlib.contextmanager
def gemini_only():
    """No Cursor answers inside: the SDK key reaches only weaker models, which have hung on pictures."""
    fallback, llm.CURSOR_FALLBACK = llm.CURSOR_FALLBACK, False
    try:
        yield
    finally:
        llm.CURSOR_FALLBACK = fallback


def write(row: dict, pages: list[dict], reel_id: str) -> dict:
    """A script for the story that keeps every rule, after up to FIX_ROUNDS rewrites. Raises llm.OutOfQuota once
    Flash and Flash Lite are both spent."""
    prompt = _prompt(row, pages)
    with gemini_only():
        script = _tidy(llm.generate(prompt, schema=SCHEMA, models=SCRIPT_MODELS, thinking="high",
                                    purpose=f"{reel_id} script"))
        for _ in range(FIX_ROUNDS):
            if not (found := problems(script)):
                break
            log.info("%s script breaks %d rules: %s", reel_id, len(found), " ".join(found))
            script = _tidy(llm.generate([prompt, "\nYour script:\n" + json.dumps(script, ensure_ascii=False),
                                         FIX.format(problems="\n".join(f"- {p}" for p in found))],
                                        schema=SCHEMA, models=SCRIPT_MODELS, thinking="high",
                                        purpose=f"{reel_id} script fix"))
    if found := problems(script):
        raise ValueError(f"{reel_id} script still breaks rules after {FIX_ROUNDS} rewrites: {' '.join(found)}")
    return script


def house_style() -> dict:
    """What every Reel shares, as the newest one has it: series, voice, music and caption style."""
    spec = yaml.safe_load(max(reels.REELS.glob("ak*/short.yaml")).read_text(encoding="utf-8"))
    return {"series": spec["series"], "synthetic_media": True, "depth_motion": True, "tail": TAIL,
            "voice": {k: spec["voice"][k] for k in ("engine", "voice", "lang_code")} | {"lead_in": LEAD_IN},
            "music": spec["music"] | {"fade_out": MUSIC_FADE},
            "captions": {k: v for k, v in spec["captions"].items() if k != "hook_y"} | {"hook_size": HOOK_SIZE}}


def spec_fields(draft: dict) -> dict:
    """The spec's fields that come from the script (all but the pictures, their credits, and caption heights)."""
    script, row = draft["script"], draft["row"]
    style = house_style()
    text = row["source"].split(",")[0].strip()
    style["voice"]["direction"] = DIRECTION.format(text=text, tones=script["tones"].strip().rstrip("."))
    return {"id": draft["id"], "title": script["title"], "series": style.pop("series"),
            "description": f"{script['summary'].strip()}\n{script['teaser'].strip()}\n\nस्रोत: {script['source_line'].strip()}",
            "hook_text": script["hook_text"], "sources": draft["sources"],
            "hashtags": [t.lower() for t in script["hashtags"]], "tags": script["tags"], **style,
            "beats": [{"text": b["text"], "emphasis": [b["emphasis"]], **({"sfx": b["sfx"]} if b["sfx"] != "none" else {})}
                      for b in script["beats"]]}


def draft(reel_id: str | None = None) -> Path:
    """Write the next story's script into its Reel's folder, and return the folder."""
    if not (found := next_story()):
        raise LookupError("no story in the backlog has a source the writer can read")
    row, urls = found
    reel_id = reel_id or next_id()
    pages = read_sources(urls)
    script = write(row, pages, reel_id)
    folder = reels.REELS / reel_id
    (folder / "work").mkdir(parents=True, exist_ok=True)
    (folder / SCRIPT).write_text(json.dumps({"id": reel_id, "row": row, "sources": urls, "writer": llm.answered_by(),
                                             "script": script}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return folder


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ytc.reel_writer", description="write the next brand Reel's script")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("next", help="the next story the writer can source, and its pages")
    written = commands.add_parser("write", help="write the next story's script")
    written.add_argument("--id", help="the Reel's id (default: the next free one)")
    args = parser.parse_args()

    from .cli import PIPELINE_ROOT, _load_env

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _load_env(PIPELINE_ROOT / ".env")
    if args.command == "next":
        found = next_story()
        print(json.dumps({"row": found[0], "pages": found[1]} if found else None, ensure_ascii=False, indent=1))
        return
    llm.use_brand_project()
    folder = draft(args.id)
    print(json.dumps(json.loads((folder / SCRIPT).read_text(encoding="utf-8"))["script"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
