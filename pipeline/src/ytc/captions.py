"""Word-by-word animated captions (ASS, burned in) and a plain SRT track for YouTube."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterator
from functools import cache
from itertools import combinations
from pathlib import Path

from fontTools.ttLib import TTFont
from PIL import ImageFont

from .spec import CaptionStyle
from .tts import Word

WIDTH, HEIGHT = 1080, 1920
# Devanagari ends a sentence with a danda (U+0964), or two (U+0965) at the end of a verse.
_STRIP = ",.;:\"\u201c\u201d()[]\u0964\u0965\u2026"
_SENTENCE_END = ".!?\u0964\u0965\u2026"
# Hindi words that lean on a neighbour. A chunk that starts on a postposition, an auxiliary or जी, or ends on
# a negation, a title or a number, splits a phrase and flashes half of it alone.
_HI_NO_START = frozenset(unicodedata.normalize("NFC", w) for w in (
    "का की के को से में पर ने तक लिए साथ बाद द्वारा है हैं था थी थे हो गया गई गए दिया दी दिए लिया ली रहा रही रहे "
    "सका सकी जी").split())
_HI_NO_END = frozenset(unicodedata.normalize("NFC", w) for w in (
    "नहीं न मत श्री महर्षि ऋषि राजा भगवान देवी एक दो तीन चार पाँच पांच छह छः सात आठ नौ दस बीस पचास साठ सौ "
    "हज़ार हजार लाख करोड़").split())
# English words that lean on the next one, so a hook's line never ends on them ("SAVED BY THE / ASSASSIN'S").
_EN_NO_END = frozenset("a an the of by to in at on for from with and or but his her its their after before".split())
_SPLIT_PHRASE = 10.0  # the cost of a break inside a Hindi phrase; a chunk one word off its size costs 1


def _ass_color(hex_color: str) -> str:
    value = hex_color.lstrip("#")
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H00{blue}{green}{red}".upper()


def _ass_time(t: float) -> str:
    cs = int(round(max(t, 0.0) * 100))
    hours, cs = divmod(cs, 360000)
    minutes, cs = divmod(cs, 6000)
    seconds, cs = divmod(cs, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cs:02d}"


def _srt_time(t: float) -> str:
    ms = int(round(max(t, 0.0) * 1000))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def _display(text: str, uppercase: bool) -> str:
    cleaned = text.strip().strip(_STRIP).replace("{", "(").replace("}", ")").replace("\\", "")
    return cleaned.upper() if uppercase else cleaned


def _key(text: str) -> str:
    return text.strip().strip(_STRIP + "!?").lower()


@cache
def _measure_font(font_file: Path, size: int) -> ImageFont.FreeTypeFont:
    # libass sizes a font so usWinAscent + usWinDescent equals the ASS Fontsize.
    font = TTFont(font_file, lazy=True)
    os2 = font["OS/2"]
    em = size * font["head"].unitsPerEm / (os2.usWinAscent + os2.usWinDescent)
    return ImageFont.truetype(str(font_file), em)


def _split_cost(before: str, after: str) -> float:
    """What a break between two words costs: a pause (comma, colon) invites one, a phrase forbids it."""
    if before.rstrip()[-1:] in ",:;":
        return -1.0
    split = (unicodedata.normalize("NFC", _key(after)) in _HI_NO_START
             or unicodedata.normalize("NFC", _key(before)) in _HI_NO_END
             or _key(before) in _EN_NO_END
             or _key(before).isdigit())
    return _SPLIT_PHRASE if split else 0.0


def _phrase_chunks(
    words: list[Word], max_words: int, fits: Callable[[list[Word]], bool] | None
) -> Iterator[list[Word]]:
    """Hindi chunks: each sentence of a beat is split where the total cost is least, counting (max_words - size)²
    per chunk plus _split_cost at every break, so chunks stay near max_words without cutting a phrase."""
    start = 0
    for i, word in enumerate(words):
        if i + 1 < len(words) and words[i + 1].beat == word.beat and word.text[-1:] not in _SENTENCE_END:
            continue
        sentence = words[start:i + 1]
        start = i + 1
        cost = [0.0] + [float("inf")] * len(sentence)
        back = [0] * (len(sentence) + 1)
        for end in range(1, len(sentence) + 1):
            for size in range(1, min(max_words, end) + 1):
                first = end - size
                if size > 1 and fits is not None and not fits(sentence[first:end]):
                    break
                total = cost[first] + (max_words - size) ** 2
                if first:
                    total += _split_cost(sentence[first - 1].text, sentence[first].text)
                if total < cost[end]:
                    cost[end], back[end] = total, first
        cuts, end = [], len(sentence)
        while end:
            cuts.append((back[end], end))
            end = back[end]
        for first, end in reversed(cuts):
            yield sentence[first:end]


def _chunks(
    words: list[Word], max_words: int, fits: Callable[[list[Word]], bool] | None = None
) -> Iterator[list[Word]]:
    if any("\u0900" <= ch <= "\u097f" for w in words for ch in w.text):
        yield from _phrase_chunks(words, max_words, fits)
        return
    chunk: list[Word] = []
    for word in words:
        if chunk and (
            len(chunk) >= max_words
            or word.beat != chunk[-1].beat
            or chunk[-1].text[-1:] in _SENTENCE_END
            or (fits is not None and not fits([*chunk, word]))
        ):
            yield chunk
            chunk = []
        chunk.append(word)
    if chunk:
        yield chunk


def _hook_lines(words: list[str], font: ImageFont.FreeTypeFont) -> list[str]:
    """As few lines as fit 70% of the width, cut where the widest line is narrowest, so a last word is not left
    alone ("सोना नहीं, सच चाहिए / था"); a cut after a comma is worth a little extra width, and one inside a
    phrase (_split_cost) is taken only when nothing else fits."""
    lines = [""]
    for word in words:
        trial = f"{lines[-1]} {word}".strip()
        if lines[-1] and font.getlength(trial) > WIDTH * 0.7:
            lines.append(word)
        else:
            lines[-1] = trial
    best, best_cost = lines, float("inf")
    for cuts in combinations(range(1, len(words)), len(lines) - 1):
        trial = [" ".join(words[a:b]) for a, b in zip((0, *cuts), (*cuts, len(words)))]
        widest = max(font.getlength(line) for line in trial)
        cost = widest + WIDTH * 0.08 * sum(_split_cost(words[cut - 1], words[cut]) for cut in cuts)
        if widest <= WIDTH * 0.7 and cost < best_cost:
            best, best_cost = trial, cost
    return best


def _hook_event(text: str, end: float, fonts_dir: Path, style: CaptionStyle) -> str:
    """The on-screen teaser over the first beat: a few words in a dark box near the top, above the player's
    buttons and clear of the captions."""
    font = _measure_font(fonts_dir / style.hook_font_file, style.hook_size)
    body = "\\N".join(_hook_lines(_display(text, True).split(), font)[:3])
    # On screen from the first frame: a feed starts playing there, and swiping is decided in the first second.
    return (f"Dialogue: 1,{_ass_time(0)},{_ass_time(max(end, 1.5))},Hook,,0,0,0,,"
            f"{{\\an8\\pos({WIDTH // 2},{int(HEIGHT * style.hook_y)})\\fad(0,200)}}{body}")


# A label is set smaller than the hook, since a question to the viewers runs to a dozen words.
LABEL_SCALE = 0.72


def _label_events(labels: list[tuple[str, float, float, float | None]], fonts_dir: Path,
                  style: CaptionStyle) -> list[str]:
    """A box like the hook's over the lines that have a label; one box across lines in a row with the same label,
    so a question stays on screen while the viewers are asked to answer it."""
    merged: list[list] = []
    for text, start, end, y in labels:
        if merged and merged[-1][0] == text and abs(merged[-1][2] - start) < 0.05:
            merged[-1][2] = end
        else:
            merged.append([text, start, end, y])
    font = _measure_font(fonts_dir / style.hook_font_file, int(style.hook_size * LABEL_SCALE))
    events = []
    for text, start, end, y in merged:
        body = "\\N".join(_hook_lines(_display(text, True).split(), font)[:4])
        top = int(HEIGHT * (style.hook_y if y is None else y))
        events.append(f"Dialogue: 1,{_ass_time(start)},{_ass_time(end)},Label,,0,0,0,,"
                      f"{{\\an8\\pos({WIDTH // 2},{top})\\fad(150,150)}}{body}")
    return events


def write_ass(
    words: list[Word],
    style: CaptionStyle,
    path: Path,
    total: float,
    emphasis: dict[int, set[str]],
    fonts_dir: Path,
    hook: tuple[str, float] | None = None,
    beat_y: dict[int, float] | None = None,
    labels: list[tuple[str, float, float, float | None]] | None = None,
) -> None:
    """Captions, and the hook's on-screen text (its words, and when it leaves) if there is one. `beat_y` moves
    the captions of the beats it names to another height (a fraction of the frame); `labels` are boxes like the
    hook's (text, start, end, and the box's top, None for the hook's)."""
    primary, highlight, accent = (_ass_color(c) for c in (style.primary, style.highlight, style.accent))
    # libass lays out letter-spaced text glyph by glyph, which tears a Devanagari syllable from its vowel signs.
    hook_spacing = 0 if hook and any(unicodedata.category(c).startswith("M") for c in hook[0]) else 1
    label_spacing = 0 if any(unicodedata.category(c).startswith("M") for text, *_ in labels or [] for c in text) else 1
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {WIDTH}\nPlayResY: {HEIGHT}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{style.font},{style.size},{primary},{primary},&H00000000,&H78000000,"
        f"0,0,0,0,100,100,0,0,1,{style.outline},4,5,60,60,0,1\n"
        # BorderStyle 3 draws a box in the outline color behind each line.
        f"Style: Hook,{style.hook_font},{style.hook_size},{highlight},{highlight},&H38000000,&H00000000,"
        f"0,0,0,0,100,100,{hook_spacing},0,3,18,0,8,60,60,0,1\n"
        f"Style: Label,{style.hook_font},{int(style.hook_size * LABEL_SCALE)},{highlight},{highlight},&H38000000,"
        f"&H00000000,0,0,0,0,100,100,{label_spacing},0,3,16,0,8,60,60,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    font = _measure_font(fonts_dir / style.font_file, style.size)
    max_width = WIDTH * style.max_width

    def width(chunk: list[Word]) -> float:
        return font.getlength(" ".join(_display(w.text, style.uppercase) for w in chunk)) + 2 * style.outline

    x = WIDTH // 2
    chunks = list(_chunks(words, style.words_per_line, lambda c: width(c) <= max_width))
    events: list[str] = []
    for ci, chunk in enumerate(chunks):
        y = int(HEIGHT * (beat_y or {}).get(chunk[0].beat, style.y))
        next_start = chunks[ci + 1][0].start if ci + 1 < len(chunks) else total
        chunk_end = min(next_start, chunk[-1].end + 1.0, total)
        scale = min(100, int(100 * max_width / width(chunk)))
        for wi, word in enumerate(chunk):
            end = chunk[wi + 1].start if wi + 1 < len(chunk) else chunk_end
            parts = []
            for wj, other in enumerate(chunk):
                text = _display(other.text, style.uppercase)
                if wj == wi:
                    color = highlight
                elif _key(other.text) in emphasis.get(other.beat, set()):
                    color = accent
                else:
                    parts.append(text)
                    continue
                parts.append(f"{{\\c{color}}}{text}{{\\c{primary}}}")
            tags = f"\\an5\\pos({x},{y})\\fscx{scale}\\fscy{scale}"
            if wi == 0:
                start_scale = int(scale * 0.86)
                tags += f"\\fscx{start_scale}\\fscy{start_scale}\\t(0,90,\\fscx{scale}\\fscy{scale})"
            events.append(
                f"Dialogue: 0,{_ass_time(word.start)},{_ass_time(end)},Cap,,0,0,0,,{{{tags}}}{' '.join(parts)}"
            )
    if hook and hook[0].strip():
        events.append(_hook_event(hook[0], hook[1], fonts_dir, style))
    if labels:
        events += _label_events([label for label in labels if label[0].strip()], fonts_dir, style)
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def write_srt(words: list[Word], path: Path, max_words: int = 7) -> None:
    cues = []
    for index, chunk in enumerate(_chunks(words, max_words), start=1):
        text = " ".join(w.text.strip() for w in chunk)
        cues.append(f"{index}\n{_srt_time(chunk[0].start)} --> {_srt_time(chunk[-1].end)}\n{text}\n")
    path.write_text("\n".join(cues), encoding="utf-8")
