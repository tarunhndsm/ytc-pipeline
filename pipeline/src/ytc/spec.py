"""The YAML contract for one Short: narration beats, visuals, voice, captions, and metadata."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Motion = Literal["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down", "none"]
# "url" is a picture chosen by eye and fetched as is (credit included); "card" is a designed title card.
Source = Literal["commons", "met", "aic", "nasa", "pexels", "pixabay", "ai", "file", "color", "url", "card"]


class _Model(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True, extra="forbid")


class Card(_Model):
    """A designed title card for a line no picture can show: a date and place, a number, or a quote."""

    kind: Literal["dateline", "fact", "quote"] = "fact"
    big: str
    small: str = ""


class Visual(_Model):
    source: Source = "color"
    fallbacks: list[Source] = []
    query: str | None = None
    prompt: str | None = None
    path: str | None = None
    url: str | None = None
    # For a "url" picture: source, title, credit (the author), license, and url (the picture's page).
    credit: dict[str, str | None] = {}
    card: Card | None = None
    color: str = "#101820"
    motion: Motion = "zoom_in"
    # Which part of the image the vertical crop keeps: 0 is the left or top edge, 1 the right or bottom.
    focus_x: float = Field(0.5, ge=0, le=1)
    focus_y: float = Field(0.5, ge=0, le=1)
    # The part of the picture that shows exactly what the line names (left, top, right, bottom, each 0 to 1):
    # a long beat cuts to it for a second, closer shot.
    box: list[float] = []
    # The only part of the picture to use (left, top, right, bottom, each 0 to 1), e.g. one tall scene from a wide
    # painting so it fills the screen; focus and box then refer to this part.
    crop: list[float] = []
    # Show an earlier beat's image again (1 is the first beat), e.g. for the loop line.
    reuse: int | None = None

    @model_validator(mode="after")
    def _box_is_a_box(self) -> Visual:
        for name in ("box", "crop"):
            b = getattr(self, name)
            if b and not (len(b) == 4 and 0 <= b[0] < b[2] <= 1 and 0 <= b[1] < b[3] <= 1):
                raise ValueError(f"visual.{name} must be [left, top, right, bottom] within 0 to 1, got {b}")
        return self


class Beat(_Model):
    text: str
    visual: Visual = Field(default_factory=Visual)
    emphasis: list[str] = []
    sfx: Literal["whoosh", "pop", "riser", "none"] = "none"
    pause_after: float = 0.12
    # Where this line's captions sit (0 is the top of the frame, 1 the bottom), for a picture with a face where
    # they would otherwise go; empty keeps the caption style's height.
    caption_y: float | None = Field(None, ge=0.15, le=0.85)
    # A Reel's lines around its story (reel_writer): "answer" gives the previous Reel's question its answer, "quiz"
    # asks the viewers this one's.
    role: Literal["story", "answer", "quiz"] = "story"
    # A few words in a box like the hook's over this line, and the box's top as a fraction of the height (empty:
    # the hook's height).
    label: str = ""
    label_y: float | None = Field(None, ge=0.08, le=0.5)


class Voice(_Model):
    engine: Literal["kokoro", "gemini"] = "kokoro"
    # A Kokoro voice, or a Gemini TTS voice name such as "Algenib".
    voice: str = "af_heart"
    # Kokoro reads at this speed; a Gemini take is played this much faster at the same pitch.
    speed: float = Field(1.0, gt=0.5, le=2.0)
    # Kokoro's language code; "h" is Hindi.
    lang_code: str = "a"
    # Gemini only: the TTS model (empty: the newest that answers), and performance notes in English, which it
    # follows without reading them out. The notes give tone, not events: told "rising awe when Surya blazes", a
    # take narrated Surya blazing before the script got there. An English script can't carry them (every take read
    # them aloud), so for English they go in the part's style field instead.
    model: str = ""
    direction: str = ""
    # Gemini only: the language the take is read in, such as "en-US"; empty leaves it to the model. A "voice_..."
    # name is a voice designed in the project rather than a prebuilt one.
    language: str = ""
    # Gemini only: words the voice reads from a respelling while the captions keep the real spelling, e.g.
    # {जह्नु: जहनु} for a name it says wrong. Told in the direction how to say जह्नु, three takes dropped or
    # invented lines around it.
    say: dict[str, str] = {}
    # Seconds before the first word. A Reel needs enough for its cover frame to show the hook before any caption.
    lead_in: float = Field(0.15, ge=0, le=1)
    # Gemini only: a silence inside the take longer than this many seconds is cut down to it (0 keeps the take's
    # own pauses). A model reading a script leaves up to a second between lines; a Short is edited tighter.
    max_pause: float = Field(0.0, ge=0, le=2)


class Music(_Model):
    # A file beside the spec, "tanpura" for the drone the pipeline synthesizes (drone.py), or "bed" (or
    # "bed:<mood>", a key of bed.MOODS) for a bed it composes for this Short alone (bed.py).
    path: str | None = None
    gain_db: float = -24.0
    # Seconds the music fades out over at the very end, rather than stopping dead.
    fade_out: float = Field(0.0, ge=0, le=5)


class CaptionStyle(_Model):
    font: str = "Montserrat Black"
    font_file: str = "Montserrat-Black.ttf"
    size: int = 128
    # Clear of the Shorts player's like and comment buttons down the right edge.
    max_width: float = 0.74
    primary: str = "#FFFFFF"
    highlight: str = "#FFD400"
    accent: str = "#00E5FF"
    outline: int = 9
    words_per_line: int = 3
    uppercase: bool = True
    y: float = 0.60
    # The on-screen hook's font; Anton has no Devanagari, so Hindi Reels name one that does.
    hook_font: str = "Anton"
    hook_font_file: str = "Anton-Regular.ttf"
    hook_size: int = 112
    # The top of the hook's box, as a fraction of the height: lower it when the first picture has a face near the top.
    hook_y: float = Field(0.15, ge=0.08, le=0.5)


class Quiz(_Model):
    """The question a Reel asks its viewers to answer in the comments; the next Reel gives the answer."""

    question: str
    answer: str
    # Spellings a right answer in the comments may use, in Devanagari and in Latin letters.
    accepted: list[str] = []
    # The source's words that settle the answer.
    evidence: str = ""


class Answered(_Model):
    """The earlier Reel whose question this one answers, which goes out before it on every channel."""

    reel: str
    question: str
    answer: str


class Part(_Model):
    """One part of a story told over several Reels."""

    series: str
    number: int = Field(ge=1)
    of: int = Field(ge=2)


class ShortSpec(_Model):
    id: str
    title: str
    series: str | None = None
    description: str = ""
    # A few words on screen over the first beat, a teaser that adds to the spoken hook.
    hook_text: str = ""
    quiz: Quiz | None = None
    answers: Answered | None = None
    # The viewers who answered that question right, by channel, as each channel names them (ytc.shoutouts).
    shoutouts: dict[Literal["instagram", "youtube", "facebook"], list[str]] = {}
    part: Part | None = None
    sources: list[str] = []
    hashtags: list[str] = []
    tags: list[str] = []
    category_id: str = "27"
    synthetic_media: bool = False
    # Pictures that fill the screen move in depth (depth.py), near parts against far ones. Needs torch and
    # transformers, which the YouTube studio's image doesn't have.
    depth_motion: bool = False
    # "bold" (Days of Odd's format 2): pictures graded vivid rather than muted, and a wide one cropped to fill the
    # screen around its subject rather than shown whole as a framed print.
    look: Literal["archive", "bold"] = "archive"
    # Seconds the Short runs on after the last word.
    tail: float = Field(0.35, ge=0, le=3)
    voice: Voice = Field(default_factory=Voice)
    music: Music = Field(default_factory=Music)
    captions: CaptionStyle = Field(default_factory=CaptionStyle)
    beats: list[Beat]

    @model_validator(mode="after")
    def _reuse_points_back(self) -> ShortSpec:
        for number, beat in enumerate(self.beats, start=1):
            reuse = beat.visual.reuse
            if reuse is not None and not 1 <= reuse < number:
                raise ValueError(f"beat {number}: visual.reuse must name an earlier beat (1 to {number - 1})")
        return self

    @classmethod
    def load(cls, path: Path) -> ShortSpec:
        return cls.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
