"""Ankahi Kathayein's levers: what the growth desk reads from the Reel pipeline and what it may change there.

It may change three things, all upstream of the judge, who still fails a weak Reel:
- the notes the script writer reads before every Reel (playbook.json, read by reel_writer.audience);
- the order of the backlog's Next table (reel_backlog.reorder; a series already under way stays first);
- new backlog stories aimed at what is working or a festival ahead (reel_backlog.refill with guidance; each still
  has to pass refill's checks: a source that parses, names in its pages, museum paintings of its figures).
A festival a few days off that nothing is planned for gets a story on every run, strategist or not (cover_festivals).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta

import yaml

from .. import reel_backlog, reel_writer, reels

log = logging.getLogger(__name__)

PLAYBOOK = reel_writer.GROWTH_PLAYBOOK
# What a run commits besides the desk's own folder: the backlog, and the source pages refill saves.
PATHS = ("content/instagram/backlog.md", "content/instagram/sources")
SCRIPTURES = (("mahabharat", "Mahabharata"), ("ramayan", "Ramayana"), ("bhagavat", "Bhagavata Purana"),
              ("shiva purana", "Shiva Purana"), ("vishnu purana", "Vishnu Purana"), ("upanishad", "Upanishads"),
              ("harivamsa", "Harivamsa"))
GENERIC = {"hindu mythology", "mythology", "hinduism", "indian mythology", "sanatan dharma", "spirituality",
           "devotion", "bhakti", "hindu", "india", "story", "stories"}
FAMOUS = {"krishna", "rama", "ram", "hanuman", "shiva", "vishnu", "ganesha", "ganesh", "arjuna", "karna", "bhishma",
          "draupadi", "sita", "ravana", "lakshmana", "parvati", "lakshmi", "brahma", "indra", "surya", "narada",
          "yudhishthira", "bhima", "kunti", "duryodhana", "durga", "kali", "radha", "balarama", "vishvarupa"}
FAMOUS_HI = ("कृष्ण", "कान्हा", "राम", "हनुमान", "शिव", "महादेव", "विष्णु", "गणेश", "अर्जुन", "कर्ण", "भीष्म", "द्रौपदी",
             "सीता", "रावण", "लक्ष्मण", "पार्वती", "लक्ष्मी", "ब्रह्मा", "इंद्र", "सूर्य", "नारद", "राधा")
QUESTION = re.compile(r"\?|क्यों|किसने|क्या|कैसे|कौन|किसको|किसे|कहाँ|कब")
# The backlog keeps this many stories the writer can read before the desk adds any of its own.
FULL = 24
REFILL_COUNT = 8
MAX_PRIORITIES = 6
# A festival's story waits in the backlog until this many days before it, then goes out first.
HOLD_DAYS = 3
# A festival this close with nothing planned for it gets a story now, made the day before it.
NEAR_DAYS = 4
# The newest Reels made, which may already be a close festival's story.
RECENT_MADE = 6
# Names and words in too many stories to tie one to a festival theme.
COMMON = {"krishna", "rama", "shiva", "vishnu", "hanuman", "arjuna", "lord", "with", "from", "into", "their", "when",
          "what", "story", "stories", "devotion", "divine", "gods", "secret", "kanda", "parva", "purana", "valmiki",
          "ramayana", "mahabharata", "bhagavata", "forms", "takes", "great"}


def made_posts() -> dict[str, str]:
    """Each Buffer post id the pipeline booked, and its Reel."""
    ids = {}
    for folder in reels.REELS.glob("ak*"):
        for name in ("instagram.json", "facebook.json", "youtube.json"):
            record = reels._read(folder / name) or {}
            if isinstance(record, dict) and record.get("id"):
                ids[record["id"]] = folder.name
    return ids


def _made() -> dict[str, tuple[str, str]]:
    text = reel_writer.BACKLOG.read_text(encoding="utf-8")
    section = text.split("\n## Made", 1)[1].split("\n## ", 1)[0] if "\n## Made" in text else ""
    rows = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) >= 3 and re.fullmatch(r"ak\d+", cells[0]):
            rows[cells[0]] = (cells[1], cells[2])
    return rows


def features() -> dict[str, dict]:
    """What each made Reel is: its title, hook, story, text, figures and form, and the judge's score."""
    made = _made()
    out = {}
    for folder in sorted(reels.REELS.glob("ak*")):
        try:
            spec = yaml.safe_load((folder / "short.yaml").read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        beats = spec.get("beats") or []
        hook = str(beats[0].get("text", "")) if beats else ""
        title = str(spec.get("title") or "")
        tags = [str(t) for t in spec.get("tags") or []]
        story, source = made.get(folder.name, ("", ""))
        text = f"{source} {' '.join(map(str, spec.get('sources') or []))} {' '.join(tags)}".lower()
        figures = [t for t in tags if t.lower() not in GENERIC and not any(w in t.lower() for w, _ in SCRIPTURES)]
        review = reels._read(folder / reels.REVIEW) or {}
        out[folder.name] = {
            "title": title, "hook": hook, "story": story, "source": source,
            "scripture": next((name for word, name in SCRIPTURES if word in text), "other"),
            "figures": figures[:5],
            "famous": any(f.lower() in FAMOUS for f in figures) or any(n in title + hook for n in FAMOUS_HI),
            "question": bool(QUESTION.search(title) or QUESTION.search(hook)),
            "quiz": bool(spec.get("quiz")), "answers": bool(spec.get("answers")), "series": bool(spec.get("series")),
            "words": sum(len(str(b.get("text", "")).split()) for b in beats),
            "judge": review.get("total") if isinstance(review, dict) else None,
        }
    return out


def backlog_view(limit: int = 25) -> list[dict]:
    """The first rows of the backlog's Next table, in the order the pipeline will make them."""
    return [{"story": row["story"], "hook": row["hook"], "source": row["source"],
             "series": row.get(reel_writer.SERIES) or "", "from": row.get(reel_writer.FROM) or ""}
            for row in reel_writer.backlog()[:limit]]


def playbook() -> dict:
    try:
        return json.loads(PLAYBOOK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _guidance(themes: list[dict], today: date) -> str:
    lines = []
    for theme in themes:
        when = f" (for {theme['festival']} on {theme['date']})" if theme.get("festival") and theme.get("date") else ""
        lines.append(f"- {theme['theme']}{when}: {theme['why']}")
    return "\n".join(lines)


def _soon(theme: dict, today: date, days: int = 21) -> bool:
    try:
        return today <= date.fromisoformat(theme.get("date") or "") <= today + timedelta(days=days)
    except ValueError:
        return False


def _words(text: str) -> set[str]:
    """Words of five letters or more, cut to five so "returns" meets "return"; sources in brackets left out."""
    return {w[:5] for w in re.findall(r"[a-z]+", re.sub(r"\([^)]*\)", " ", text.lower()))
            if len(w) > 4 and w not in COMMON}


def _festival_of(story: str, themes: list[dict]) -> dict | None:
    """The festival theme a new story was planned for: they share a name or word that few stories share."""
    return next((t for t in themes if t.get("date") and _words(story) & _words(t["theme"])), None)


def apply(decisions: dict, now: datetime, dry: bool, state: dict) -> dict:
    """Put the strategist's checked decisions into the pipeline; what was done."""
    today = now.astimezone(reels.IST).date()
    done: dict = {}
    notes = decisions.get("writer_notes") or []
    if notes:
        book = playbook() | {
            "updated": now.isoformat(timespec="minutes"), "writer_notes": notes,
            "youtube": decisions.get("youtube", []), "experiments": decisions.get("experiments", []),
            "themes": decisions.get("new_stories", []), "festival_holds": decisions.get("festival_holds", []),
            "diagnosis": decisions.get("diagnosis", ""),
        }
        if not dry:
            PLAYBOOK.parent.mkdir(parents=True, exist_ok=True)
            PLAYBOOK.write_text(json.dumps(book, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        done["writer_notes"] = len(notes)
    first: list[str] = []
    themes = decisions.get("new_stories") or []
    readable = [row for row in reel_writer.backlog() if not reel_writer.held(row, today)]
    if themes and (len(readable) < FULL or any(_soon(t, today) for t in themes)) and state.get("refilled") != str(today):
        try:
            result = reel_backlog.refill(REFILL_COUNT, dry=dry, guidance=_guidance(themes, today))
            done["new_stories"] = result["added"]
            done["turned_down"] = len(result["turned_down"])
            for story in result.get("stories", []):
                if theme := _festival_of(story, [t for t in themes if _soon(t, today)]):
                    until = max(today, date.fromisoformat(theme["date"]) - timedelta(days=HOLD_DAYS))
                    if dry or reel_backlog.hold(story, until):
                        first.append(story)
                        done.setdefault("held", []).append(f"{story} until {until} ({theme['festival']})")
            if not dry:
                state["refilled"] = str(today)
        except Exception as err:
            log.exception("refill with the desk's themes failed")
            done["refill_error"] = str(err)[:200]
    for item in decisions.get("festival_holds") or []:
        rows = reel_writer.backlog()
        if not (row := next((r for r in rows if r["story"] == item["story"]), None)):
            continue
        part = reel_writer.part_of(row)
        group = [r for r in rows if (p := reel_writer.part_of(r)) and p["series"] == part["series"]] if part else [row]
        if part and min(reel_writer.part_of(r)["number"] for r in group) > 1:
            continue
        if any(r.get(reel_writer.FROM) for r in group):
            # A From date already set was planned with the series' length and the factory's pace in mind.
            first.append(group[0]["story"])
            continue
        until = date.fromisoformat(item["date"]) - timedelta(days=HOLD_DAYS + (len(group) - 1) // 2)
        if until > today and not dry:
            for r in group:
                reel_backlog.hold(r["story"], until)
        first.append(group[0]["story"])
        done.setdefault("held", []).append(
            f"{group[0]['story']}{f' and the rest of its series' if part else ''} until {max(until, today)} "
            f"({item['festival']})")
    wanted = list(dict.fromkeys(first + (decisions.get("priorities") or [])))[:MAX_PRIORITIES]
    if wanted:
        done["moved_up"] = wanted if dry else reel_backlog.reorder(wanted)
    return done


def cover_festivals(now: datetime, dry: bool, state: dict) -> dict:
    """Each festival in the next NEAR_DAYS days that no recent Reel is about gets a story: the backlog's own if one
    fits (a story told in one Reel), else a new one through refill's checks, tried once a day. It goes first, held
    until the day before the festival unless its row already has a date. What was done."""
    from . import scout

    today = now.astimezone(reels.IST).date()
    done: dict = {}
    recent = [story for _, (story, _) in sorted(_made().items(), key=lambda item: int(item[0][2:]))[-RECENT_MADE:]]
    tried = state.setdefault("festival_refills", {})
    for festival in scout.festivals(today, NEAR_DAYS):
        day, name = date.fromisoformat(festival["date"]), festival["festival"]
        theme = {"theme": festival["stories"], "festival": name, "date": festival["date"],
                 "why": f"{name} is in {(day - today).days} days and nothing is planned for it"}
        if day <= today or any(_festival_of(story, [theme]) for story in recent):
            continue
        story = next((r["story"] for r in reel_writer.backlog()
                      if not reel_writer.part_of(r) and _festival_of(r["story"], [theme])), None)
        if not story and tried.get(name) != str(today):
            try:
                result = reel_backlog.refill(2, dry=dry, guidance=_guidance([theme], today))
                story = next((s for s in result.get("stories", []) if _festival_of(s, [theme])), None)
                done["new_stories"] = done.get("new_stories", []) + result["added"]
            except Exception as err:
                log.exception("a story for %s failed", name)
                done["refill_error"] = str(err)[:200]
            if not dry:
                tried[name] = str(today)
        if not story:
            done.setdefault("uncovered", []).append(name)
            continue
        until = max(today, day - timedelta(days=1))
        row = next((r for r in reel_writer.backlog() if r["story"] == story), {})
        if not dry and row:
            if not row.get(reel_writer.FROM):
                reel_backlog.hold(story, until)
            reel_backlog.reorder([story])
        done.setdefault("held", []).append(f"{story} until {row.get(reel_writer.FROM) or until} ({name})")
    return done
