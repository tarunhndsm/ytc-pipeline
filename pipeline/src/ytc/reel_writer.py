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

from . import captions, llm, reel_sources, reels
from .spec import ShortSpec

log = logging.getLogger(__name__)

BACKLOG = reels.REELS / "backlog.md"
COLUMNS = ("story", "hook", "source", "picture", "care")
# An optional sixth column puts a row in a story told over several Reels: "<the series' Hindi name> | k/n".
SERIES = "series"
SCRIPT = "work/script.json"
# The brand project's Flash models except the judge's, whose daily quota the reviews need, and gemini-flash-latest,
# which may be one of the others under another name.
WRITERS = tuple(m for m in llm.FLASH if m not in (reels.JUDGE, "gemini-flash-latest"))
# Once those are spent, the same project's Flash Lite; Gemma only picks and frames pictures, since a weak script
# costs one of the day's few good voice takes and Gemma got a script's facts wrong where Flash Lite didn't.
SCRIPT_MODELS = (*WRITERS, *llm.FLASH_LITE)
PICTURE_MODELS = (*SCRIPT_MODELS, *llm.GEMMA)
# The narrator speaks 2.4 to 2.9 words a second: the writer's lines run about fifty seconds, and with the answer
# and the question the page adds around them (ANSWER, ASK, INVITE) the Reel runs 65 to 80 seconds, under
# reels.LONGEST, with room for the story to finish.
BEATS = (10, 13)
WORDS = (115, 140)
LINE_WORDS = (4, 16)
# Each Reel asks its viewers a question about its story once the story has ended, and the next Reel answers it
# right after its hook, thanking those who got it right (reel_cloud books every Reel after the one it answers).
# Comments and follows decide how far Instagram carries a Reel, and a question with its answer promised in the next
# Reel brings viewers back for it. The question is a fact from the story's own source that the narration leaves out.
QUESTION_WORDS = (5, 12)
ANSWER_WORDS = (1, 4)
ASK = "आज का सवाल… {question}"
ANSWER = "पिछले सवाल का जवाब… {answer}! सही बताने वालों को बधाई।"
INVITE = "सही जवाब कमेंट में लिखिए… अगली रील में शाउटआउट मिलेगा, फ़ॉलो कर लीजिए!"
INVITE_PART = "सही जवाब कमेंट में लिखिए… शाउटआउट और भाग {next}, अगली रील में! फ़ॉलो कर लीजिए।"
ASK_LABEL = "आज का सवाल: {question}"
ANSWER_LABEL = "जवाब: {answer}"
QUIZ_TONE = "a bright, playful, inviting tone for the question to the viewers and the last line"
NUMBER_WORDS = {1: "एक", 2: "दो", 3: "तीन", 4: "चार", 5: "पाँच", 6: "छह"}
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
        "quiz": {"type": "object", "properties": {
            "question": {"type": "string"},
            "answer": {"type": "string"},
            "accepted": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "string"},
        }, "required": ["question", "answer", "accepted", "evidence"]},
    },
    "required": ["title", "hook_text", "beats", "summary", "teaser", "source_line", "tones", "hashtags", "tags",
                 "quiz"],
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

How the Reel runs around your lines (the page adds the lines in quotes, in the same voice):
{around}

The question for the viewers (quiz). After your last line the Reel says "{ask}" and invites them to write the
answer in the comments; the next Reel gives the answer and thanks those who got it right. Comments decide how far
Instagram carries a Reel, so make it one people want to answer:
- question: in Hindi, {question_min} to {question_max} words, ending with "?": a fact from this story's source text
  that your narration doesn't give, so viewers who know the story can answer and the rest want to know. A name, a
  number, a place or an object, answered in a word or two, like "राजा अश्वपति ने संतान के लिए कितने साल तप किया
  था?" Not a trick, not about death, violence or caste, and not about this Reel's own revelation.
- answer: the answer in Hindi, {answer_min} to {answer_max} words, numbers as words, like "अठारह साल".
- accepted: the ways a right answer may be written in the comments, in Devanagari and in Latin letters, with
  common spellings and digits, like ["अठारह", "18", "atharah", "eighteen"].
- evidence: one sentence of the source text below that gives the answer, copied exactly.

The pictures (they are chosen later from what you write):
- Every line gets a picture of its own, the last one too; the question after it shows line 1's picture again.
- picture: in English, exactly what the line's picture should show: the person, being, place or object the line
  names, at that moment of the story (a child, if the line is about his childhood). The pictures come from museum
  collections of Indian paintings and sculpture (the Cleveland Museum of Art, the Met, LACMA, the Freer, the
  Wellcome Collection) and from Wikimedia Commons, so describe what such a picture would show, and nothing about
  effects, light or motion. Never a photograph: a photo among the paintings looks out of place, so a river, a
  mountain, a plant, an animal or a temple is as Indian art shows it (the Ganga in a Pahari painting). Line 1's
  picture is the first frame in the feed: the figure line 1 names, in a vivid, colourful painting, never a grey
  sculpture, a landscape, a plant or an animal on its own.
- search: 2 to 4 short English search terms, each what you would type into a museum's search box to find that
  picture. The first is the name of whoever or whatever the line shows, as museums spell it (Krishna, Brahma,
  Garuda, Ashtavakra), since a painting of this very story may exist; then the scene (Janaka court) or the
  painting's tradition (Bhagavata Purana, Pahari, Kangra, Kalighat). Never a museum's name or a bare word like
  "sage" or "sculpture".{picture}
{series}
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
            rows.append(dict(zip(COLUMNS, cells)) | ({SERIES: cells[len(COLUMNS)]} if cells[len(COLUMNS):] else {}))
    return rows


def part_of(row: dict) -> dict | None:
    """The part of a series a backlog row is ("गोवर्धन लीला 2/3": {"series": "गोवर्धन लीला", "number": 2, "of": 3}),
    or None for a story told in one Reel."""
    if m := re.fullmatch(r"(.*\S)\s+(\d+)\s*/\s*(\d+)", (row.get(SERIES) or "").strip()):
        if 1 <= int(m[2]) <= int(m[3]) and int(m[3]) >= 2:
            return {"series": m[1], "number": int(m[2]), "of": int(m[3])}
    return None


def turned_down(folder: Path) -> bool:
    """Whether the judge has failed a Reel, with all its judgments in."""
    verdict = reels._read(folder / reels.REVIEW)
    if not verdict or sum(r.get("judge") == reels.JUDGE for r in verdict.get("runs") or []) < reels.RUNS:
        return False
    try:
        return not reels.passes(verdict, folder.name, with_bar=False)
    except Exception:
        return False


def _made_before(reel_id: str):
    """The made Reels before `reel_id` (their short.yaml read), newest first."""
    for folder in sorted((p for p in reels.REELS.glob("ak*") if p.is_dir() and p.name < reel_id), reverse=True):
        if (folder / "short.yaml").exists():
            yield folder, yaml.safe_load((folder / "short.yaml").read_text(encoding="utf-8"))


def asked_before(reel_id: str) -> tuple[Path, dict] | None:
    """The newest Reel before `reel_id` that asked its viewers a question and can still go out (booked on Instagram,
    or made and passed by the judge or not judged yet): the Reel whose question `reel_id` answers. The factory
    writes no new story while that Reel waits for the judge."""
    for folder, spec in _made_before(reel_id):
        if not spec.get("quiz"):
            continue
        if record := reels._read(folder / "instagram.json"):
            if record.get("withdrawn"):
                continue
            return folder, spec
        if not turned_down(folder):
            return folder, spec
    return None


def part_told(part: dict, reel_id: str) -> list[str]:
    """What the series' part before `part` told, as its narration's story lines; [] if no such Reel can go out."""
    for folder, spec in _made_before(reel_id):
        made = spec.get("part") or {}
        if made.get("series") == part["series"] and made.get("number") == part["number"] - 1 and \
                not turned_down(folder):
            return [beat["text"] for beat in spec["beats"] if beat.get("role", "story") == "story"]
    return []


def next_story() -> tuple[dict, list[str]] | None:
    """The first story in the backlog whose source this can read, with its pages."""
    for row in backlog():
        if urls := source_urls(row["source"]):
            return row, urls
    return None


def next_id() -> str:
    numbers = [int(m[1]) for p in reels.REELS.glob("ak*") if (m := re.fullmatch(r"ak(\d+)", p.name))]
    return f"ak{max(numbers, default=0) + 1:03d}"


def set_aside() -> list[dict]:
    """The rows of the backlog's Set aside table: story, source and why."""
    if "\n## Set aside" not in (text := BACKLOG.read_text(encoding="utf-8")):
        return []
    rows = []
    for line in text.split("\n## Set aside", 1)[1].split("\n## ", 1)[0].splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) >= 3 and cells[0] != "Story" and set(cells[0]) - set("-: "):
            rows.append(dict(zip(("story", "source", "why"), cells)))
    return rows


def read_sources(urls: list[str]) -> list[dict]:
    """Each page's text (reel_sources: saved, live or archived). A page that can't be read raises, since the script
    needs all of them."""
    return [{"url": url, "text": reel_sources.read(url)} for url in urls]


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
    lines = [beat.text for beat in ShortSpec.load(folder / "short.yaml").beats if beat.role == "story"]
    if lines[-1].rstrip().endswith("…"):
        lines.pop()
    return "\n".join(lines)


_NOT_A_SEARCH = re.compile(r"\b(met|lacma|cleveland|freer|wellcome|museum|gallery|collection|commons)\b", re.I)
_EFFECT = re.compile(r"\b(glow\w*|particles?|aura|overlay|light beam|light effect|flicker\w*|shimmer\w*|sparkl\w*)\b", re.I)


def _words(text: str) -> list[str]:
    return [w for w in (captions._key(w) for w in text.split()) if w]


def _plain(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


def quoted(evidence: str, pages: list[dict]) -> bool:
    """Whether the source pages hold the evidence: most of its words, and five of them in a row."""
    words = _plain(evidence)
    if len(words) < 5:
        return False
    text = " ".join(" ".join(_plain(page["text"])) for page in pages)
    vocabulary = set(text.split())
    runs = (" ".join(words[i:i + 5]) for i in range(len(words) - 4))
    return sum(w in vocabulary for w in words) >= 0.8 * len(words) and any(f" {run} " in f" {text} " for run in runs)


def quiz_problems(quiz: dict, beats: list[dict], pages: list[dict] | None = None) -> list[str]:
    """The rules a script's question for the viewers breaks."""
    found = []
    question, answer = quiz.get("question", "").strip(), quiz.get("answer", "").strip()
    if not QUESTION_WORDS[0] <= (count := len(_words(question))) <= QUESTION_WORDS[1] or not question.endswith("?"):
        found.append(f"quiz.question has {count} words; ask in {QUESTION_WORDS[0]} to {QUESTION_WORDS[1]} words, "
                     "ending with \"?\".")
    if not ANSWER_WORDS[0] <= (count := len(_words(answer))) <= ANSWER_WORDS[1]:
        found.append(f"quiz.answer has {count} words; answer in {ANSWER_WORDS[0]} to {ANSWER_WORDS[1]}.")
    if re.search(r"[A-Za-z0-9]", question + answer):
        found.append("quiz.question and quiz.answer are said aloud: write them in Devanagari, numbers as words.")
    told = [_words(beat["text"]) for beat in beats]
    if (said := _words(answer)) and any(line[i:i + len(said)] == said for line in told for i in range(len(line))):
        found.append(f"The narration already says the answer \"{answer}\"; ask about a fact it doesn't give.")
    accepted = quiz.get("accepted") or []
    if not any(re.search(r"[\u0900-\u097F]", a) for a in accepted) or \
            not any(re.search(r"[A-Za-z0-9]", a) for a in accepted):
        found.append("quiz.accepted needs the answer in Devanagari and in Latin letters or digits.")
    if pages is not None and not quoted(quiz.get("evidence", ""), pages):
        found.append("quiz.evidence isn't in the source text; copy the sentence that gives the answer exactly.")
    return found


def problems(script: dict, pages: list[dict] | None = None) -> list[str]:
    """The rules a script breaks, each as a line the writer is shown; the question's evidence is checked against
    `pages` when given."""
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
    return found + quiz_problems(script.get("quiz") or {}, beats, pages)


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


def _invite(part: dict | None) -> str:
    if part and part["number"] < part["of"]:
        return INVITE_PART.format(next=NUMBER_WORDS.get(part["number"] + 1, str(part["number"] + 1)))
    return INVITE


def _around(before: tuple[Path, dict] | None, part: dict | None) -> str:
    lines = ["- Your line 1, the hook."]
    if before:
        quiz = before[1]["quiz"]
        lines.append(f"- \"{ANSWER.format(answer=quiz['answer'])}\": the answer to the question the Reel before this "
                     f"one asked its viewers (\"{quiz['question']}\"), over that Reel's picture. Your line 2 comes "
                     "straight after it, so it pulls the viewer back into your hook.")
    lines += ["- Your lines 2 to the last: the story, which your last line ends.",
              f"- \"{ASK.format(question='<your quiz question>')} {_invite(part)}\", over line 1's picture."]
    return "\n".join(lines)


def _series(part: dict | None, reel_id: str) -> str:
    """What the writer is told of the series a Reel is part of."""
    if not part:
        return ""
    number, of = part["number"], part["of"]
    text = [f"\nThis Reel is part {number} of {of} of one story, \"{part['series']}\", told over {of} Reels one "
            f"after another; its title and first frame say \"भाग {number}\". Tell only what this part's source "
            "tells, as a whole episode with its own hook and payoff."]
    if number > 1 and (told := part_told(part, reel_id)):
        text.append(f"Part {number - 1}, which went out just before, told:\n" + "\n".join(told) +
                    f"\nDon't retell it: open with this part's own hook; line 2 may recall where part {number - 1} "
                    "left off, in a few words.")
    if number < of:
        text.append(f"End at a turning point: the last line is a complete sentence that settles this part's hook and "
                    f"says what is still to come, like \"पर असली परीक्षा… अभी बाकी थी।\" The Reel then tells "
                    f"viewers part {number + 1} comes next.")
    else:
        text.append("This is the last part: the whole story ends here.")
    return "\n".join(text) + "\n"


def _prompt(row: dict, pages: list[dict], before: tuple[Path, dict] | None = None, part: dict | None = None,
            reel_id: str = "") -> str:
    care = f"Care: {row['care']}\n" if row["care"] else ""
    picture = (f"\n- The backlog suggests this picture; use it for the line it fits: {row['picture']}"
               if row["picture"] else "")
    sources = "\n\n".join(f"[{page['url']}]\n{page['text']}" for page in pages)
    return PROMPT.format(story=row["story"], hook=row["hook"], source=row["source"], care=care, picture=picture,
                         beats_min=BEATS[0], beats_max=BEATS[1], words_min=WORDS[0], words_max=WORDS[1],
                         line_min=LINE_WORDS[0], line_max=LINE_WORDS[1], hook_words=HOOK_WORDS,
                         around=_around(before, part), ask=ASK.format(question="<your quiz question>"),
                         question_min=QUESTION_WORDS[0], question_max=QUESTION_WORDS[1],
                         answer_min=ANSWER_WORDS[0], answer_max=ANSWER_WORDS[1], series=_series(part, reel_id),
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


def write(row: dict, pages: list[dict], reel_id: str, before: tuple[Path, dict] | None = None,
          part: dict | None = None) -> dict:
    """A script for the story that keeps every rule, after up to FIX_ROUNDS rewrites. Raises llm.OutOfQuota once
    Flash and Flash Lite are both spent."""
    prompt = _prompt(row, pages, before, part, reel_id)
    with gemini_only():
        script = _tidy(llm.generate(prompt, schema=SCHEMA, models=SCRIPT_MODELS, thinking="high",
                                    purpose=f"{reel_id} script"))
        for _ in range(FIX_ROUNDS):
            if not (found := problems(script, pages)):
                break
            log.info("%s script breaks %d rules: %s", reel_id, len(found), " ".join(found))
            script = _tidy(llm.generate([prompt, "\nYour script:\n" + json.dumps(script, ensure_ascii=False),
                                         FIX.format(problems="\n".join(f"- {p}" for p in found))],
                                        schema=SCHEMA, models=SCRIPT_MODELS, thinking="high",
                                        purpose=f"{reel_id} script fix"))
    if found := problems(script, pages):
        raise ValueError(f"{reel_id} script still breaks rules after {FIX_ROUNDS} rewrites: {' '.join(found)}")
    return script


def chained(script: dict, before: tuple[Path, dict] | None, part: dict | None) -> dict:
    """The writer's lines with the page's own around them: after the hook, the answer to the question the Reel
    `before` asked; after the story, this Reel's question and the invitation to answer it in the comments, with the
    question on screen until the end."""
    beats = [beat | {"role": "story"} for beat in script["beats"]]
    if before:
        quiz = before[1]["quiz"]
        beats.insert(1, {"text": ANSWER.format(answer=quiz["answer"]), "emphasis": _words(quiz["answer"])[0],
                         "sfx": "pop", "role": "answer", "label": ANSWER_LABEL.format(answer=quiz["answer"]),
                         "from": before[0].name, "picture": "", "search": []})
    question = script["quiz"]["question"].strip()
    # One line: a picture shown again always starts from line 1's framing, so two would jump at the cut.
    beats.append({"text": f"{ASK.format(question=question)} {_invite(part)}", "emphasis": "सवाल फ़ॉलो", "sfx": "pop",
                  "role": "quiz", "label": ASK_LABEL.format(question=question), "picture": "", "search": []})
    return script | {"beats": beats}


def house_style() -> dict:
    """What every Reel shares, as the newest one has it: series, voice, music and caption style."""
    spec = yaml.safe_load(max(reels.REELS.glob("ak*/short.yaml")).read_text(encoding="utf-8"))
    return {"series": spec["series"], "synthetic_media": True, "depth_motion": True, "tail": TAIL,
            "voice": {k: spec["voice"][k] for k in ("engine", "voice", "lang_code")} | {"lead_in": LEAD_IN},
            "music": spec["music"] | {"fade_out": MUSIC_FADE},
            "captions": {k: v for k, v in spec["captions"].items() if k != "hook_y"} | {"hook_size": HOOK_SIZE}}


def _spec_beat(beat: dict) -> dict:
    made = {"text": beat["text"], "emphasis": [beat["emphasis"]], **({"sfx": beat["sfx"]} if beat["sfx"] != "none" else {})}
    if beat.get("role", "story") != "story":
        made["role"] = beat["role"]
    return made | ({"label": beat["label"]} if beat.get("label") else {})


def spec_fields(draft: dict) -> dict:
    """The spec's fields that come from the script (all but the pictures, their credits, and caption heights)."""
    script, row, part = draft["script"], draft["row"], draft.get("part")
    style = house_style()
    text = row["source"].split(",")[0].strip()
    tones = script["tones"].strip().rstrip(".")
    if any(b.get("role") == "quiz" for b in script["beats"]):
        tones += "; " + QUIZ_TONE
    style["voice"]["direction"] = DIRECTION.format(text=text, tones=tones)
    title, hook_text = script["title"], script["hook_text"]
    if part:
        title, hook_text = f"{title} | भाग {part['number']}", f"भाग {part['number']} • {hook_text}"
    fields = {"id": draft["id"], "title": title, "series": style.pop("series"),
              "description": f"{script['summary'].strip()}\n{script['teaser'].strip()}\n\nस्रोत: {script['source_line'].strip()}",
              "hook_text": hook_text, "sources": draft["sources"],
              "hashtags": [t.lower() for t in script["hashtags"]], "tags": script["tags"], **style,
              "beats": [_spec_beat(b) for b in script["beats"]]}
    if quiz := script.get("quiz"):
        fields["quiz"] = {k: quiz[k] for k in ("question", "answer", "accepted", "evidence")}
    return fields | {k: draft[k] for k in ("answers", "part") if draft.get(k)}


def draft(reel_id: str | None = None) -> Path:
    """Write the next story's script into its Reel's folder, and return the folder."""
    if not (found := next_story()):
        raise LookupError("no story in the backlog has a source the writer can read")
    row, urls = found
    reel_id = reel_id or next_id()
    pages = read_sources(urls)
    before, part = asked_before(reel_id), part_of(row)
    script = chained(write(row, pages, reel_id, before, part), before, part)
    answers = {"reel": before[0].name, "question": before[1]["quiz"]["question"],
               "answer": before[1]["quiz"]["answer"]} if before else None
    folder = reels.REELS / reel_id
    (folder / "work").mkdir(parents=True, exist_ok=True)
    (folder / SCRIPT).write_text(json.dumps({"id": reel_id, "row": row, "sources": urls, "writer": llm.answered_by(),
                                             "script": script, "answers": answers, "part": part},
                                            ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
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
