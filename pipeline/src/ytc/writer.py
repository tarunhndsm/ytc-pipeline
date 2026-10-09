"""Write an episode from its research: the script and metadata, checked against strategy/SCRIPT-RULES.md."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import yaml

from . import llm

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
SERIES = (
    "Wait, That Happened?",
    "History's Luckiest People",
    "History's Unluckiest People",
    "Hoaxes That Fooled Everyone",
    "Trials You Won't Believe",
    "Bad Ideas That Seemed Good",
    "History Got It Wrong",
)
HOOK_STYLES = ["statement", "question", "number", "contradiction"]
SFX = ["none", "whoosh", "pop", "riser"]
CARD_KINDS = ["none", "dateline", "fact", "quote"]
# The longest a title card's lines may be, in characters, so they stay large on a phone.
CARD_BIG = {"dateline": 24, "fact": 28, "quote": 90}
CARD_SMALL = 60
# Format 2 (strategy/SCRIPT-RULES.md): what the episode's topic.json records as "format", and its limits.
FORMAT = 2
BEATS, WORDS, HOOK_WORDS, BEAT_WORDS = (7, 12), (65, 95), 10, 14
# A year (a number of 1,000 or more otherwise has a comma), or a month and day.
_YEAR = re.compile(r"\b(?:1\d{3}|20[0-2]\d)s?\b")
_DATE_FIRST = re.compile(
    r"\s*(?:(?:in|on|by|back in|around|during|since|from|until)\s+)?(?:the\s+)?(?:(?:early|late|mid)[\s-]+)?"
    r"(?:(?:1\d{3}|20[0-2]\d)s?\b|(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d)",
    re.I)
_OVERRIDE = re.compile(r"\[([^\]]+)\]\(/[^)]*/\)")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF]")

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "hooks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"style": {"type": "string", "enum": HOOK_STYLES}, "text": {"type": "string"}},
                "required": ["style", "text"],
            },
        },
        "hook_style": {"type": "string", "enum": HOOK_STYLES},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "beats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "emphasis": {"type": "array", "items": {"type": "string"}},
                    "sfx": {"type": "string", "enum": SFX},
                    "pause_after": {"type": "number"},
                    "claims": {"type": "array", "items": {"type": "integer"}},
                    "visual": {"type": "string"},
                    "subjects": {"type": "array", "items": {"type": "string"}},
                    "year": {"type": "integer"},
                    "queries": {"type": "array", "items": {"type": "string"}},
                    "card": {
                        "type": "object",
                        "properties": {"kind": {"type": "string", "enum": CARD_KINDS}, "big": {"type": "string"},
                                       "small": {"type": "string"}},
                        "required": ["kind", "big", "small"],
                    },
                },
                "required": ["text", "emphasis", "sfx", "pause_after", "claims", "visual", "subjects", "year", "queries", "card"],
            },
        },
        "hook_text": {"type": "string"},
        "loop": {"type": "boolean"},
        "better_than_last": {"type": "string"},
    },
    "required": ["hooks", "hook_style", "title", "description", "hashtags", "tags", "beats", "hook_text", "loop",
                 "better_than_last"],
}

PROMPT = """You write Shorts for Days of Odd (@DaysOfOdd): strange-but-true history in American English for a
US audience. Promise: one true story you won't believe happened, told in about 30 seconds and sourced.

Episode {id}, series "{series}". Topic: {topic}

Follow these rules exactly:

{rules}

What the channel has learned so far (apply every proven rule; try to beat the numbers):

{learnings}

Recent episodes, for format and tone only. Don't reuse their wording or structure beat for beat:

{exemplars}

Research. The numbered claims are the only facts you may state; say nothing beyond them.

Story: {story}
Most surprising fact: {angle}
Claims:
{claims}
Disputed (leave out, or attribute "according to one account"):
{disputed}
Visual ideas:
{visuals}

Write the episode:
- hooks: three candidate first lines in different styles, each 10 words or fewer, opening inside the story
  on its most surprising true fact, stated plainly so a stranger gets it on one hearing, with something
  everyone pictures at once (a famous name, a place, an animal, money, a life at stake). Never open with a
  year or a date, and never a riddle that hides the fact: "An actor saved the president's son right before
  his brother struck" hides that the brother of Lincoln's assassin saved Lincoln's son. Use the strongest as
  beat 1 and name its style in hook_style.
- beats: 7 to 12 beats and 65 to 95 spoken words in total, each beat 14 words or fewer: one sentence, or two
  very short ones. Say it the way you'd tell a friend, with contractions and everyday words. The shape is
  the hook, a beat that makes it stranger (never background), escalating beats in the order things
  happened, a twist or payoff in the last 2 or 3 beats, and a last beat that runs straight into beat 1 as
  one sentence when the Short loops (a last beat "And that's the story of how..." loops into a first beat
  "Australia went to war with emus."). Set loop to true when it does. No beat starts with a year or date.
- Each beat: claims lists the numbers of the claims it states (the last beat may reuse the hook's);
  emphasis has 1 or 2 words or short phrases copied exactly from that beat's text; sfx is "whoosh" on up to 3
  scene changes, "pop" on up to 2 punchlines, "riser" once just before the twist, otherwise "none";
  pause_after is 0.12, or 0.3 after a punchline.
- Each beat's picture: visual describes the ideal picture the way an archive would caption it (subject, kind of
  picture, year), in 15 words or fewer ("photograph of Elsie Wright with paper fairies, 1917"). It shows whoever
  or whatever the line is about, not someone it only mentions (a line about a queen's wedding shows her, not the
  king), and a place or event as it looked then, not a present-day photograph of it. subjects lists
  0 to 3 exact English Wikipedia article titles for the people, places, ships, buildings, objects, or events that
  picture shows, most specific first ("Cottingley Fairies", "Arthur Conan Doyle"); year is when the beat takes
  place (0 if it has no time); queries gives 2 or 3 Wikimedia Commons searches for it, naming the exact subject.
- Each beat's card: a title card shown instead when no picture fits. kind "dateline" (big: the date, like
  "November 1932"; small: the place), "fact" (big: a number or a fact of 1 to 3 words from the line, like
  "20,000 emus"; small: a few words explaining it), or "quote" (big: words quoted in the claims, exactly; small:
  who said or wrote them). Use only that beat's text and claims. Use kind "none", with big and small empty, when
  the beat has no date, number, or quote worth a card.
- hook_text: 2 to 5 words in giant letters over beat 1 that add to the spoken hook without repeating it or
  giving away the payoff: the place and year, a number, or the stakes ("AUSTRALIA, 1932").
- title, description, hashtags, tags: per the metadata rules. The first hashtag is #history.
- better_than_last: one sentence naming the learning or improvement this episode applies, and where.
"""


def _between(text: str, start: str, end: str | None) -> str:
    begin = text.index(start)
    stop = text.index(end, begin) if end and end in text[begin:] else len(text)
    return text[begin:stop].strip()


def _rules() -> str:
    rules = (ROOT / "strategy" / "SCRIPT-RULES.md").read_text(encoding="utf-8")
    return "\n\n".join(
        [_between(rules, "## Script rules", "## Metadata rules"), _between(rules, "## Metadata rules", "## Visual rules"),
         _between(rules, "## Sound rules", "## Spec schema")]
    )


def _learnings() -> str:
    text = (ROOT / "strategy" / "LEARNINGS.md").read_text(encoding="utf-8")
    return text.split("## Log")[0].strip()


def _format(folder: Path) -> int:
    topic = folder / "topic.json"
    return json.loads(topic.read_text(encoding="utf-8")).get("format", 1) if topic.exists() else 1


def exemplars(count: int = 2) -> str:
    """The newest scheduled or live specs of this format, trimmed to what shows format and tone."""
    records = sorted((ROOT / "content" / "episodes").glob("ep*/publish.json"), reverse=True)
    folders = [r for r in records if _format(r.parent) >= FORMAT][:count]
    blocks = []
    for record in folders:
        spec = yaml.safe_load((record.parent / "short.yaml").read_text(encoding="utf-8"))
        beats = [{"text": b["text"], "emphasis": b.get("emphasis", []), "sfx": b.get("sfx", "none")} for b in spec["beats"]]
        blocks.append(yaml.safe_dump({"title": spec["title"], "series": spec.get("series"), "beats": beats},
                                     sort_keys=False, allow_unicode=True, width=120))
    return "\n---\n".join(blocks) or "(none yet in this format: follow the whole script in the rules for shape and pace)"


def spoken(text: str) -> str:
    return _OVERRIDE.sub(r"\1", text)


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’.,\-]*", spoken(text)))


def _key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", spoken(text).lower()))


def _plain_words(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)+|[a-z0-9']+", spoken(text).lower().replace(",", ""))


def _card_ok(card: dict, beat: dict, research: dict) -> bool:
    """A title card states nothing its beat and the beat's claims don't: its numbers, and a quote word for word."""
    kind, big, small = card.get("kind"), (card.get("big") or "").strip(), (card.get("small") or "").strip()
    if kind not in CARD_BIG or not big or len(big) > CARD_BIG[kind] or len(small) > CARD_SMALL or _EMOJI.search(big + small):
        return False
    claims = research.get("claims", [])
    cited = " ".join(claims[c - 1]["claim"] for c in beat.get("claims", []) if 1 <= c <= len(claims))
    known = " ".join(_plain_words(f"{beat['text']} {cited}"))
    for number in re.findall(r"\d[\d,.]*", f"{big} {small}"):
        if number.replace(",", "").rstrip(".") not in known.replace(",", ""):
            return False
    if kind == "quote":
        quoted = " ".join(_plain_words(big.strip("\"“”'")))
        return bool(quoted) and quoted in " ".join(_plain_words(" ".join(c["claim"] for c in claims)))
    return True


def normalize(script: dict, research: dict | None = None) -> dict:
    """Fix the mechanical slips (hashtag case, stray emphasis, too many sound effects or tags, a long
    description, missing picture searches, a card or on-screen hook that breaks the rules) without another call."""
    tags = [re.sub(r"[^a-z0-9]", "", t.lower()) for t in script.get("hashtags", [])]
    tags = list(dict.fromkeys(["history"] + [t for t in tags if t and t != "history"]))
    if len(tags) >= 3:
        script["hashtags"] = [f"#{t}" for t in tags[:3]]
    script["tags"] = list(dict.fromkeys(t.strip() for t in script.get("tags", []) if isinstance(t, str) and t.strip()))[:8]
    sentences = re.split(r"(?<=[.!?])\s+", (script.get("description") or "").strip())
    script["description"] = " ".join(sentences[:2])
    limits = {"whoosh": 3, "pop": 2, "riser": 1}
    for beat in script.get("beats", []):
        text = spoken(beat["text"]).lower()
        kept = [e for e in beat.get("emphasis", []) if e.lower() in text]
        if kept:
            beat["emphasis"] = kept[:2]
        sfx = beat.get("sfx", "none")
        if sfx in limits:
            limits[sfx] -= 1
            if limits[sfx] < 0:
                beat["sfx"] = "none"
        beat["pause_after"] = min(max(float(beat.get("pause_after", 0.12)), 0.12), 0.6)
        beat["subjects"] = [s.strip() for s in beat.get("subjects") or [] if isinstance(s, str) and s.strip()][:3]
        queries = [q.strip() for q in beat.get("queries") or [] if isinstance(q, str) and q.strip()]
        for extra in (*beat["subjects"], beat.get("visual", "")):
            if len(queries) < 2 and extra and extra not in queries:
                queries.append(extra)
        beat["queries"] = queries[:3]
        try:
            beat["year"] = int(beat.get("year") or 0)
        except (TypeError, ValueError):
            beat["year"] = 0
        card = beat.get("card") or {}
        if card.get("kind", "none") != "none" and not (research and _card_ok(card, beat, research)):
            log.info("dropping a title card that states more than its beat: %s", card)
            beat["card"] = {"kind": "none", "big": "", "small": ""}
    hook_text = " ".join((script.get("hook_text") or "").split())
    hook = set(_plain_words(script["beats"][0]["text"])) if script.get("beats") else set()
    words = _plain_words(hook_text)
    if not 2 <= len(words) <= 6 or len(hook_text) > 40 or _EMOJI.search(hook_text) or sum(w in hook for w in words) > 0.7 * len(words):
        hook_text = ""
    script["hook_text"] = hook_text
    return script


def problems(script: dict, research: dict) -> list[str]:
    """Every way the draft breaks the script rules, as instructions for a rewrite."""
    found = []
    beats = script.get("beats", [])
    total = sum(word_count(b["text"]) for b in beats)
    if not BEATS[0] <= len(beats) <= BEATS[1]:
        found.append(f"Use {BEATS[0]} to {BEATS[1]} beats (you have {len(beats)}).")
    if not WORDS[0] <= total <= WORDS[1]:
        found.append(f"Use {WORDS[0]} to {WORDS[1]} spoken words in total (you have {total}).")
    if beats:
        hook = spoken(beats[0]["text"])
        if word_count(hook) > HOOK_WORDS:
            found.append(f"The hook (beat 1) must be {HOOK_WORDS} words or fewer (it has {word_count(hook)}).")
        if re.match(r"\s*(did you know|hey|hi|welcome|meet|this is|once upon|back in)\b", hook, re.I) or "days of odd" in hook.lower():
            found.append("The hook must open inside the story on its surprise: no 'Did you know', 'Meet', 'Back in', "
                         "greeting, or channel name.")
        if not re.search(r"[.!?…][\"'”’)]*\s*$", hook):
            found.append(f"The hook (beat 1) must be a whole sentence ending in a full stop, question mark, or "
                         f"exclamation mark (it ends \"{hook[-30:].strip()}\").")
    for number, beat in enumerate(beats, start=1):
        text = spoken(beat["text"])
        if number > 1 and word_count(text) > BEAT_WORDS:
            found.append(f"Beat {number} must be {BEAT_WORDS} words or fewer (it has {word_count(text)}); split it or cut it.")
        if _DATE_FIRST.match(text):
            found.append(f"Beat {number} starts with a year or date (\"{text[:24]}\"): open on what happened and put "
                         "the year later in the line, in another beat, or in hook_text.")
    if len(beats) > 1 and _key(beats[-1]["text"]) == _key(beats[0]["text"]):
        found.append("The last beat repeats beat 1, so the loop would say the hook twice in a row: end on a beat that "
                     "runs into beat 1 as one sentence instead (\"And that's the story of how...\").")
    years = {y for b in beats for y in _YEAR.findall(spoken(b["text"]))}
    if len(years) > 2:
        found.append(f"Say at most 2 different years in the whole Short (you have {', '.join(sorted(years))}).")
    title = script.get("title", "")
    if len(title) > 50:
        found.append(f"The title must be 50 characters or fewer (it has {len(title)}).")
    if _EMOJI.search(title) or len([w for w in re.findall(r"\b[A-Z]{2,}\b", title) if w not in ("US", "UK", "USA", "TV")]) > 1:
        found.append("The title may have at most one word in capitals and no emojis.")
    tags = script.get("hashtags", [])
    if len(tags) != 3 or (tags and tags[0] != "#history") or any(not re.fullmatch(r"#[a-z0-9]+", t) for t in tags):
        found.append("Give exactly 3 lowercase hashtags with no spaces, the first being #history.")
    if not 4 <= len(script.get("tags", [])) <= 8:
        found.append("Give 4 to 8 tags.")
    if len(re.findall(r"[.!?](\s|$)", script.get("description", "").strip())) > 2:
        found.append("The description must be 1 or 2 sentences.")
    claim_count = len(research.get("claims", []))
    counts = {s: 0 for s in SFX}
    for number, beat in enumerate(beats, start=1):
        counts[beat.get("sfx", "none")] += 1
        text = spoken(beat["text"]).lower()
        emphasis = beat.get("emphasis", [])
        if not 1 <= len(emphasis) <= 2 or any(e.lower() not in text for e in emphasis):
            found.append(f"Beat {number}: emphasis needs 1 or 2 words or phrases copied exactly from its text.")
        cited = beat.get("claims", [])
        if any(not 1 <= c <= claim_count for c in cited):
            found.append(f"Beat {number}: claims must be numbers from 1 to {claim_count}.")
        if not cited and number != len(beats):
            found.append(f"Beat {number}: list the claims it states; every fact must come from the research.")
        if not 0.1 <= beat.get("pause_after", 0.12) <= 0.6:
            found.append(f"Beat {number}: pause_after must be between 0.12 and 0.6.")
        if not beat.get("queries"):
            found.append(f"Beat {number}: give 2 or 3 Commons queries.")
    if counts["whoosh"] > 3 or counts["pop"] > 2 or counts["riser"] > 1:
        found.append("Use sfx whoosh at most 3 times, pop at most 2, riser at most once.")
    return found


def _claims_listing(research: dict) -> str:
    return "\n".join(f"{n}. {c['claim']} ({c['confidence']})" for n, c in enumerate(research["claims"], start=1))


def write_script(research: dict, episode_id: str, feedback: list[str] | None = None, draft: dict | None = None) -> dict:
    """A script that meets the rules, from the research alone. With a reviewer's feedback on a rendered draft,
    a revision of that draft. Raises if two rewrites can't fix it."""
    prompt = PROMPT.format(
        id=episode_id,
        series=research["series"],
        topic=research["topic"],
        rules=_rules(),
        learnings=_learnings(),
        exemplars=exemplars(),
        story=research.get("story", ""),
        angle=research.get("angle", ""),
        claims=_claims_listing(research),
        disputed="\n".join(f"- {d}" for d in research.get("disputed", [])) or "- none",
        visuals="\n".join(f"- {v['subject']}: {'; '.join(v['queries'])}" for v in research.get("visuals", [])),
    )
    if feedback and draft:
        prompt += (
            "\n\nA reviewer watched the render of your draft and asks for these changes:\n"
            + "\n".join(f"- {item}" for item in feedback)
            + "\n\nKeep what works, make these changes within the rules, and return the whole episode again. "
            "Your draft:\n" + json.dumps(draft, indent=1)
        )
    script = llm.generate(prompt, schema=SCRIPT_SCHEMA, purpose=f"{episode_id} script" + (" revision" if feedback else ""))
    for attempt in range(2):
        script = normalize(script, research)
        issues = problems(script, research)
        if not issues:
            return script
        log.info("%s script needs fixes: %s", episode_id, issues)
        script = llm.generate(
            prompt + "\n\nYour draft broke these rules:\n" + "\n".join(f"- {i}" for i in issues)
            + "\n\nFix every one and return the whole episode again. Your draft:\n" + json.dumps(script, indent=1),
            schema=SCRIPT_SCHEMA, purpose=f"{episode_id} script fix {attempt + 1}",
        )
    script = normalize(script, research)
    if issues := problems(script, research):
        raise RuntimeError(f"{episode_id} script still breaks the rules: {issues}")
    return script
