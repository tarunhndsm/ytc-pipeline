"""The brands the growth desk watches: where each one's numbers come from, where its desk keeps its files, and which
module holds its levers (what the desk may change in its pipeline)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import reels, youtube


@dataclass(frozen=True)
class Brand:
    key: str
    name: str
    # What the brand is and who watches it, for the strategist.
    about: str
    # Environment variables: the Buffer API key, and each platform's Buffer channel id.
    buffer_key: str
    channels: dict[str, str]
    # The environment variable holding the YouTube channel's sign-in (JSON), where it lives on disk, and the folder of
    # the channel's daily analytics.
    youtube_token: str | None
    youtube_token_file: Path | None
    youtube_stats: Path | None
    # The desk's own files, committed to the private repo.
    out: Path
    # ytc.growth.<levers>: the brand's pipeline hooks (features, backlog, apply).
    levers: str
    # Searches for the Shorts drawing the most views in the brand's niche this week (YouTube, Hindi, India).
    niche: tuple[str, ...] = ()
    # A hosted media URL's Reel id, for posts whose record the brand doesn't keep.
    reel_in_url: re.Pattern | None = None
    repost_mark: str = ""
    lines: tuple[str, ...] = field(default_factory=tuple)
    # Run on the brand's own Gemini project, never the studio's keys (llm.use_brand_project).
    brand_project: bool = False
    # The phone push's title (ntfy headers are Latin-1, so no Devanagari).
    title: str = "Growth desk"
    # How much each platform counts in a Reel's overall score.
    weights: dict[str, float] = field(default_factory=dict)


ANKAHI = Brand(
    key="ankahi",
    name="अनकही कथाएँ • Ankahi Kathayein (@ankahi.kathayein)",
    about=(
        "An Instagram, Facebook and YouTube page of lesser-known, sourced stories from the Ramayana, the Mahabharata "
        "and the Bhagavata Purana, told in Hindi in one-minute Reels over old Pahari and Rajasthani paintings, for "
        "Indian families. A pipeline writes, voices, pictures, judges and books three or four new Reels a day from a "
        "backlog of stories; each Reel may end on a quiz question the next Reel answers. Instagram is the main "
        "platform (most views come from non-followers through the Reels feed); YouTube Shorts and Facebook get the "
        "same Reels."
    ),
    buffer_key="IG_BUFFER_API_KEY",
    channels={"instagram": "IG_BUFFER_CHANNEL_ID", "facebook": "IG_BUFFER_FACEBOOK_CHANNEL_ID",
              "youtube": "IG_BUFFER_YOUTUBE_CHANNEL_ID"},
    youtube_token="IG_YOUTUBE_TOKEN",
    youtube_token_file=youtube.BRAND_TOKEN_FILE,
    youtube_stats=reels.REELS / "stats" / "youtube",
    out=reels.REELS / "automation" / "growth",
    levers="ankahi",
    niche=("पौराणिक कथा", "रामायण की अनसुनी कहानी", "महाभारत की अनसुनी कहानी", "हनुमान जी की कथा", "कृष्ण लीला कथा",
           "शिव पुराण कथा"),
    reel_in_url=re.compile(r"/ankahikathayein/(?:repost-)?(ak\d+)"),
    repost_mark="/repost-",
    lines=(
        "Only stories the three texts tell, from the translations the pipeline reads; never invent or exaggerate.",
        "Nothing political, nothing comparing castes or communities, nothing about another faith, no story whose "
        "point is violence or death.",
        "No engagement bait: never ask viewers to like, share, follow or tag to get something; a quiz question the "
        "next Reel answers is fine.",
        "No clickbait a Reel doesn't pay off: the title and hook promise only what the story delivers.",
        "Old Indian paintings only (public domain or open licence), no modern pictures, nudity or figures of "
        "another faith; synthetic media is disclosed.",
    ),
    brand_project=True,
    title="Ankahi Kathayein growth desk",
    weights={"instagram": 3.0, "facebook": 1.0, "youtube": 1.0},
)

BRANDS = {brand.key: brand for brand in (ANKAHI,)}
