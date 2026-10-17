"""The strategist and its critic: models that read the analyst's, listener's and scout's findings and decide what the
pipeline should change, and fixed rules that keep their decisions inside the brand's lines.

The strategist runs on the strongest model with quota to spare: Qwen3.5-397B, open weights, on OVH's endpoint, which
needs no key; then Gemini Flash-Lite and Gemma. It leaves Gemini Flash to the judge, whose daily quota is the
scarcest. The critic runs on a different model where it can, so one model's blind spots don't pass unchecked.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date

from .. import llm
from . import scout

log = logging.getLogger(__name__)

THINKERS = ("ovh:Qwen3.5-397B-A17B", *llm.FLASH_LITE, *llm.GEMMA)
CRITICS = (*llm.GEMMA, *llm.FLASH_LITE, "ovh:Qwen3.5-397B-A17B")
LIMITS = {"writer_notes": 6, "priorities": 5, "new_stories": 4, "festival_holds": 3, "youtube": 3, "experiments": 2,
          "owner_actions": 4, "audience": 5, "what_works": 5, "what_doesnt": 5}
NOTE_CHARS = 260
# Words that put a decision outside the brand's lines (politics, communities, other faiths, bait, invention).
OFF_LIMITS = re.compile(
    r"politic|elect(ion|ed)|\bcaste|communal|muslim|islam|christian|church|mosque|bollywood|celebrit|giveaway"
    r"|tag (a|your) friend|like (and|&|or) (share|subscribe)|follow (us )?for|subscribe (for|to see)|comment (yes|1|❤)"
    r"|clickbait|exaggerat|invent|fake|mislead|guarantee|shock(ing)? truth|you won.t believe", re.I)
# Writer notes about when posts go out: the writer can't act on them.
POSTING = re.compile(r"\b\d{1,2}:\d{2}\b|\bslots?\b|post(ing)? (at|time)|schedul", re.I)
_STRING_LIST = {"type": "array", "items": {"type": "string"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string"},
        "what_works": _STRING_LIST,
        "what_doesnt": _STRING_LIST,
        "audience": _STRING_LIST,
        "writer_notes": _STRING_LIST,
        "priorities": _STRING_LIST,
        "new_stories": {"type": "array", "items": {"type": "object", "properties": {
            "theme": {"type": "string"}, "why": {"type": "string"}, "festival": {"type": "string"},
            "date": {"type": "string"}}, "required": ["theme", "why", "festival", "date"]}},
        "festival_holds": {"type": "array", "items": {"type": "object", "properties": {
            "story": {"type": "string"}, "festival": {"type": "string"}}, "required": ["story", "festival"]}},
        "youtube": _STRING_LIST,
        "experiments": {"type": "array", "items": {"type": "object", "properties": {
            "idea": {"type": "string"}, "measure": {"type": "string"}}, "required": ["idea", "measure"]}},
        "owner_actions": _STRING_LIST,
        "alert": {"type": "string"},
    },
    "required": ["diagnosis", "what_works", "what_doesnt", "audience", "writer_notes", "priorities", "new_stories",
                 "festival_holds", "youtube", "experiments", "owner_actions", "alert"],
}
# The critic only strikes items out: a rewrite by a weaker model (Flash-Lite, 17 Oct) turned six specific writer notes
# into two generic ones and invented festival dates.
STRUCK = ("what_works", "what_doesnt", "writer_notes", "priorities", "new_stories", "festival_holds", "youtube",
          "experiments", "owner_actions")
CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "strike": {"type": "array", "items": {"type": "object", "properties": {
            "field": {"type": "string", "enum": list(STRUCK)}, "number": {"type": "integer"},
            "why": {"type": "string"}}, "required": ["field", "number", "why"]}},
        "drop_alert": {"type": "boolean"},
        "review": _STRING_LIST,
    },
    "required": ["strike", "drop_alert", "review"],
}

PROMPT = """You run growth for {name}. {about}

You are the strategist of an automated growth desk that runs every day with no one at the keyboard. Your decisions
change what the pipeline makes next; rules check them, they are logged, and the owner reads them. Decide from the
numbers below, not from habit, and name the evidence. Where it is thin (few posts, young posts, one hit), say so and
prefer small, safe moves and experiments to big swings. Today is {today}.

The brand's lines, which nothing may cross:
{lines}

What you decide:
- diagnosis: two or three sentences on how the channels are doing and the single biggest lever, with numbers.
- what_works, what_doesnt: up to 5 each, short, each with its evidence (index, reach, n).
- audience: up to 5 things commenters ask for, praise or complain about (empty if there are no comments).
- writer_notes: up to 6 notes, each under 240 characters, that the script writer reads before every Reel: what our
  viewers respond to in hooks, titles, the moment to open on, pacing, endings and the quiz. They replace the current
  notes, so keep the ones that still hold. Make each concrete and checkable (name the pattern and an example title
  that showed it), never generic ("make the hook better").
- priorities: up to 5 stories from the backlog below, by their exact story title, to make next, best first; [] when
  the order is right. A series already under way stays first whatever you choose.
- new_stories: up to 4 themes for new backlog stories that fit what is working or a festival in the next three
  weeks: theme (which figure and what kind of moment), why (the evidence), festival (exactly as named in the list of
  festivals below, or "") and date ("" — the calendar's date is used). A planner turns them into sourced stories that
  must pass the pipeline's checks, and a festival's stories wait in the backlog until three days before it; [] when
  the backlog already has what the evidence calls for.
- festival_holds: up to 3 stories already in the backlog (exact story title) that belong to a festival in the list
  below and would otherwise go out weeks early: story and festival. The story, or its whole series, waits until just
  before the festival and then goes first. [] when none.
- youtube: up to 3 notes for YouTube Shorts (titles, timing, what to try), for the owner.
- experiments: up to 2 things to try in the next week, each with how we will know it worked.
- owner_actions: only what a person outside the pipeline must do, when the data shows a need: sign in again,
  fix a post that failed, decide on a format change the evidence supports. Never pipeline steps (booking,
  scheduling, reordering, dating backlog rows), which the desk and the pipeline do themselves. [] otherwise.
- alert: one sentence for the owner's phone if a person must act today: a sign-in or key that failed, posts failing
  to publish, nothing posted anywhere for two days. Otherwise "". The pipeline makes Reels as fast as its free
  quotas allow; a thin calendar alone is not an alert.

The format's fixed parts are the owner's to change, not yours: the quiz question a Reel ends on (the next Reel
answers it), stories told as a series, the posting times on Instagram and Facebook, and no reposts (the owner
turned reposts off for good). Notes must work within them; put a change to one in experiments or owner_actions
with its evidence.

How to read the numbers: "index" is a post's reach over what a post of its age usually gets on its platform (1 is
typical, 3 is three times that); reposts are left out of comparisons. Instagram reach is views, Facebook's is
impressions. A YouTube average view percentage over 100 means viewers rewatch. The page is young, so a post's age
and the page's momentum when it went out move together: the September Reels went out while the page was taking off,
and a feature that came in later (the quiz ending, for one) can look worse only because of when it came in. Treat
such a gap as a hypothesis to test, not a rule.

{brief}

The writer's current notes:
{notes}

The backlog, in the order the pipeline will make it ("from" holds a row until that date; "series" marks a part of a
story told in several Reels):
{backlog}

Festivals ahead:
{festivals}

The Shorts in our niche drawing the most views this week (YouTube, Hindi, India), and the figures their titles name
most:
{niche}

Newest YouTube comments (Instagram and Facebook comments aren't readable yet):
{comments}
"""

CRITIC = """You check a growth strategist's decisions for {name} before they are applied. {about}

The brand's lines:
{lines}

The evidence the strategist saw:
{brief}

The backlog titles priorities may name:
{titles}

The decisions, each list item numbered from 1:
{decisions}

Strike out the items that should not be applied: a claim the evidence contradicts or a number that is wrong, a note
that crosses the brand's lines or removes a fixed part of the format (the quiz question, a series), a priority not in
the backlog titles, an owner action a person can't do or doesn't need to. Leave sound items alone, even if you would
word them differently; you can't rewrite anything. Set drop_alert when the alert isn't something a person must deal
with today. In "review", one line per strike, or [] when everything stands.
"""


def _table(rows: list[dict], keys: tuple[str, ...]) -> str:
    return "\n".join(" | ".join(str(row.get(k, "")) for k in keys) for row in rows) or "(none)"


def brief(analysis: dict) -> str:
    """The analyst's findings as compact text for a prompt."""
    parts = ["Platform trends (this week against the two weeks before):"]
    for platform, numbers in analysis["trend"].items():
        parts.append(f"- {platform}: " + ", ".join(f"{k} {v}" for k, v in numbers.items()))
    parts.append("\nBest Reels by index (geometric mean over platforms):")
    for row in analysis["best"]:
        parts.append(f"- {row['reel']} score {row['score']}: {row['title']} | hook: {row['hook'][:90]} | "
                     f"{row['scripture']} | figures {', '.join(row['figures'][:3])} | quiz {row['quiz']} | "
                     f"series {row['series']} | judge {row['judge']} | {json.dumps(row['platforms'], ensure_ascii=False)}")
    if analysis["worst"]:
        parts.append("\nWeakest Reels by index:")
        for row in analysis["worst"]:
            parts.append(f"- {row['reel']} score {row['score']}: {row['title']} | hook: {row['hook'][:90]} | "
                         f"{row['scripture']} | figures {', '.join(row['figures'][:3])} | "
                         f"{json.dumps(row['platforms'], ensure_ascii=False)}")
    if analysis["newest"]:
        parts.append("\nToo young to judge yet: " + "; ".join(
            f"{r['reel']} {r['platform']} {r['reach']:.0f} at {r['age_h']}h" for r in analysis["newest"]))
    parts.append("\nMedian index by feature (n posts; \"thin\" under 6, too few to act on alone), per platform:")
    for platform, features in analysis["features"].items():
        for feature, values in features.items():
            if values:
                parts.append(f"- {platform} / {feature}: " + ", ".join(
                    f"{value} {v['median']} (n={v['n']}{', thin' if v['n'] < 6 else ''})"
                    for value, v in list(values.items())[:8]))
    parts.append("\nProblems found:\n" + ("\n".join(f"- {p}" for p in analysis["problems"]) or "- none"))
    return "\n".join(parts)


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def festival(name: str, today: date) -> tuple[str, str]:
    """The festival a theme names and its date from the scout's calendar, or ("", "") when it names none ahead: the
    models' own festival dates were wrong (Govardhan Puja put on 25 Oct, 17 Oct)."""
    key = name.casefold().strip()
    if not key:
        return "", ""
    ahead = [(day, label, hindi) for day, label, hindi, _ in scout.FESTIVALS if date.fromisoformat(day) >= today]
    for day, label, hindi in ahead:
        if key in (label.casefold(), label.casefold().split(" (")[0]) or name.strip() == hindi:
            return label, day
    for day, label, hindi in ahead:
        if key in label.casefold() or label.casefold().split(" (")[0] in key or hindi in name:
            return label, day
    return "", ""


def guard(decisions: dict, titles: list[str], today: date) -> tuple[dict, list[str]]:
    """Keep only what fits the limits and the brand's lines: decisions, and what was dropped and why."""
    dropped: list[str] = []
    out: dict = {"diagnosis": _clean_text(decisions.get("diagnosis", ""))[:900],
                 "alert": _clean_text(decisions.get("alert", ""))[:220]}
    for field in ("what_works", "what_doesnt", "audience", "writer_notes", "youtube", "owner_actions"):
        kept = []
        for item in decisions.get(field) or []:
            text = _clean_text(item)
            if not text:
                continue
            if OFF_LIMITS.search(text):
                dropped.append(f"{field}: {text[:120]} (outside the brand's lines)")
                continue
            if field == "writer_notes" and POSTING.search(text):
                dropped.append(f"{field}: {text[:120]} (posting times aren't the writer's)")
                continue
            if field == "writer_notes" and len(text) > NOTE_CHARS:
                text = text[:NOTE_CHARS].rsplit(" ", 1)[0] + "…"
            kept.append(text)
        out[field] = kept[:LIMITS[field]]
    known = {t.strip().casefold(): t for t in titles}
    out["priorities"] = []
    for title in decisions.get("priorities") or []:
        if (match := known.get(_clean_text(title).casefold())) and match not in out["priorities"]:
            out["priorities"].append(match)
        else:
            dropped.append(f"priorities: {str(title)[:100]} (not in the backlog)")
    out["priorities"] = out["priorities"][:LIMITS["priorities"]]
    out["new_stories"] = []
    for theme in decisions.get("new_stories") or []:
        row = {k: _clean_text(theme.get(k, "")) for k in ("theme", "why", "festival", "date")}
        if not row["theme"] or OFF_LIMITS.search(row["theme"] + " " + row["why"]):
            dropped.append(f"new_stories: {row['theme'][:100]} (empty or outside the brand's lines)")
            continue
        row["festival"], row["date"] = festival(row["festival"], today)
        out["new_stories"].append(row)
    out["new_stories"] = out["new_stories"][:LIMITS["new_stories"]]
    out["festival_holds"] = []
    for item in decisions.get("festival_holds") or []:
        story = known.get(_clean_text(item.get("story", "")).casefold())
        name, day = festival(_clean_text(item.get("festival", "")), today)
        if story and day:
            out["festival_holds"].append({"story": story, "festival": name, "date": day})
        else:
            dropped.append(f"festival_holds: {str(item.get('story'))[:80]} (not in the backlog, or no festival ahead)")
    out["festival_holds"] = out["festival_holds"][:LIMITS["festival_holds"]]
    out["experiments"] = [{k: _clean_text(e.get(k, ""))[:240] for k in ("idea", "measure")}
                          for e in decisions.get("experiments") or []
                          if e.get("idea") and not OFF_LIMITS.search(str(e.get("idea")))][:LIMITS["experiments"]]
    return out, dropped


def decide(brand, analysis: dict, comments: list[dict], trends: dict, backlog: list[dict], notes: list[str],
           today: date) -> dict:
    """The strategist's decisions, checked by the critic and the rules, with which models made them."""
    lines = "\n".join(f"- {line}" for line in brand.lines)
    evidence = brief(analysis)
    niche = trends.get("niche", [])
    prompt = PROMPT.format(
        name=brand.name, about=brand.about, today=today.isoformat(), lines=lines, brief=evidence,
        notes="\n".join(f"- {n}" for n in notes) or "- none yet",
        backlog=_table([{**row, "n": i + 1} for i, row in enumerate(backlog)],
                       ("n", "story", "hook", "source", "series", "from")),
        festivals=_table(trends.get("festivals", []), ("date", "festival", "days_left", "stories")),
        niche=_table(niche[:20], ("views", "title", "channel", "published"))
        + f"\nFigures named most: {json.dumps(trends.get('niche_figures', {}), ensure_ascii=False)}",
        comments=_table(comments[:60], ("title", "likes", "text")),
    )
    first = llm.generate(prompt, schema=SCHEMA, models=THINKERS, temperature=0.4, purpose="growth strategist")
    listing = "\n".join(
        f"{field}:\n" + ("\n".join(f"{i}. {json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else item}"
                                   for i, item in enumerate(first.get(field) or [], 1)) or "(none)")
        for field in STRUCK) + f"\nalert: {first.get('alert') or '(none)'}"
    decided = {**first}
    review, critic_error = [], ""
    try:
        critique = llm.generate(
            CRITIC.format(name=brand.name, about=brand.about, lines=lines, brief=evidence,
                          titles="\n".join(f"- {row['story']}" for row in backlog), decisions=listing),
            schema=CRITIC_SCHEMA, models=CRITICS, temperature=0.1, purpose="growth critic")
        review = [_clean_text(r)[:300] for r in critique.get("review") or []]
        gone = {(s.get("field"), s.get("number")) for s in critique.get("strike") or []}
        for field in STRUCK:
            decided[field] = [item for i, item in enumerate(first.get(field) or [], 1) if (field, i) not in gone]
        if critique.get("drop_alert"):
            decided["alert"] = ""
    except Exception as err:
        critic_error = str(err)[:200]
        log.warning("the critic failed, keeping the strategist's decisions: %s", err)
    clean, dropped = guard(decided, [row["story"] for row in backlog], today)
    return {"decisions": clean, "dropped": dropped, "review": review, "critic_error": critic_error,
            "strategist_draft": first}
