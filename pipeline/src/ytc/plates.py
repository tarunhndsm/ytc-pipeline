"""The still frames the camera moves over (1.12 times the video's size, so there is room to move):

- cover: a tall picture filling the screen, cropped around its subject;
- card: a wide, square, or small picture shown whole, as a print over a blurred, darkened copy of itself;
- detail: a closer crop around the part of a picture the line names;
- designed: a title card for a date and place, a number, or a quote.
"""

from __future__ import annotations

import math
import random
import re
import textwrap
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from .spec import Card

W, H = 1080, 1920
OVERSCAN = 1.12
PW, PH = 2 * round(W * OVERSCAN / 2), 2 * round(H * OVERSCAN / 2)
FONTS = Path(__file__).resolve().parents[2] / "assets" / "fonts"
PAPER = (239, 232, 218)
INK = (244, 239, 230)
ACCENT = (255, 212, 0)
MUTED = (201, 194, 182)
NIGHT = (16, 22, 29)
# A picture this tall (or taller) for its width, and this many pixels high, fills the screen; anything else is
# shown whole as a card.
COVER_RATIO = 0.8
COVER_MIN_HEIGHT = 1200
# The card's picture: its widest, its tallest, and where its middle sits, so it clears the captions (at 60% of
# the height) even at the camera's closest.
CARD_WIDTH = 1030
CARD_HEIGHT = 920
CARD_CENTER_Y = 0.36
# A designed card's text sits a little higher, and never reaches below this.
TEXT_CENTER_Y = 0.33
TEXT_BOTTOM = 0.52
# Small photos are shown at up to this much of their own size, so they read as small prints, not blurry posters.
CARD_MAX_UPSCALE = 1.7
# A close-up needs at least this many of the picture's own pixels in height, or it would be too soft.
DETAIL_MIN_HEIGHT = 760
# The bold look (ShortSpec.look) fills the screen with a wide or square picture only from this many pixels high.
FILL_MIN_HEIGHT = 1000


def grade(image: Image.Image) -> Image.Image:
    """Slightly muted and warm, so photographs, engravings, and paintings from different archives sit together."""
    image = ImageEnhance.Color(image.convert("RGB")).enhance(0.9)
    red, green, blue = image.split()
    red = red.point(lambda v: min(255, round(v * 1.02 + 2)))
    blue = blue.point(lambda v: round(v * 0.965))
    return Image.merge("RGB", (red, green, blue))


def vivid(image: Image.Image) -> Image.Image:
    """The bold look's grade: each channel's levels stretched, so a yellowed print's paper goes white and its ink
    black, and colors a little richer, where grade() mutes and warms them. (Stretching brightness alone, the same
    for every channel, made yellowed paper yellower.)"""
    image = ImageOps.autocontrast(image.convert("RGB"), cutoff=(0.5, 0.5))
    return ImageEnhance.Color(image).enhance(1.08)


def region(image: Image.Image, box: list[float]) -> Image.Image:
    width, height = image.size
    return image.crop((round(box[0] * width), round(box[1] * height), round(box[2] * width), round(box[3] * height)))


def layout(image: Image.Image) -> str:
    width, height = image.size
    return "cover" if width / height <= COVER_RATIO and height >= COVER_MIN_HEIGHT else "card"


def _crop_to(image: Image.Image, ratio: float, cx: float, cy: float) -> Image.Image:
    """The largest crop of the given width/height ratio centred as near (cx, cy) as the edges allow."""
    width, height = image.size
    if width / height > ratio:
        cw, ch = round(height * ratio), height
    else:
        cw, ch = width, round(width / ratio)
    left = min(max(round(cx * width - cw / 2), 0), width - cw)
    top = min(max(round(cy * height - ch / 2), 0), height - ch)
    return image.crop((left, top, left + cw, top + ch))


def cover(image: Image.Image, focus: tuple[float, float] = (0.5, 0.5)) -> Image.Image:
    return _crop_to(image.convert("RGB"), PW / PH, *focus).resize((PW, PH), Image.Resampling.LANCZOS)


def fill(image: Image.Image, focus: tuple[float, float], box: list[float]) -> Image.Image | None:
    """A wide or square picture cropped to fill the screen around its subject (the box, else the focus), or None
    when the crop would be too soft, would cut the boxed subject, or would leave out most of a wide picture
    whose subject is the whole of it (no box)."""
    width, height = image.size
    ratio = PW / PH
    crop_height = height if width / height > ratio else round(width / ratio)
    if crop_height < FILL_MIN_HEIGHT:
        return None
    if box:
        if (box[2] - box[0]) * width > crop_height * ratio * 1.04:
            return None
        center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    elif width > height:
        return None
    else:
        center = focus
    return _crop_to(image.convert("RGB"), ratio, *center).resize((PW, PH), Image.Resampling.LANCZOS)


def _backdrop(image: Image.Image, focus: tuple[float, float]) -> Image.Image:
    small = _crop_to(image.convert("RGB"), PW / PH, *focus).resize((PW // 4, PH // 4), Image.Resampling.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(9))
    small = ImageEnhance.Color(small).enhance(0.6)
    small = ImageEnhance.Brightness(small).enhance(0.42)
    return small.resize((PW, PH), Image.Resampling.BICUBIC)


def card(image: Image.Image, focus: tuple[float, float] = (0.5, 0.5), tilt: float = 0.0) -> Image.Image:
    """The whole picture as a bordered print with a soft shadow, over a blurred, darkened copy of itself."""
    image = image.convert("RGB")
    plate = _backdrop(image, focus)
    scale = min(CARD_WIDTH / image.width, CARD_HEIGHT / image.height, CARD_MAX_UPSCALE)
    picture = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
    border = 12
    framed = ImageOps.expand(picture, border=border, fill=PAPER)
    if tilt:
        framed = framed.convert("RGBA").rotate(tilt, resample=Image.Resampling.BICUBIC, expand=True)
    else:
        framed = framed.convert("RGBA")
    shadow = Image.new("RGBA", (framed.width + 120, framed.height + 120), (0, 0, 0, 0))
    mask = framed.getchannel("A").point(lambda a: int(a * 0.6))
    shadow.paste((0, 0, 0, 255), (60, 74), mask)
    shadow = shadow.filter(ImageFilter.GaussianBlur(22))
    cx, cy = PW // 2, round(PH * CARD_CENTER_Y)
    plate = plate.convert("RGBA")
    plate.alpha_composite(shadow, (cx - shadow.width // 2, cy - shadow.height // 2))
    plate.alpha_composite(framed, (cx - framed.width // 2, cy - framed.height // 2))
    return plate.convert("RGB")


def detail(image: Image.Image, box: list[float]) -> Image.Image | None:
    """A screen-shaped crop around the box, or None when the picture has too few pixels there."""
    image = image.convert("RGB")
    width, height = image.size
    x0, y0, x1, y1 = box
    bw, bh = (x1 - x0) * width, (y1 - y0) * height
    ratio = PW / PH
    # Grow the box to the screen's shape, with a little room around the subject.
    ch = max(bh * 1.15, bw * 1.15 / ratio, DETAIL_MIN_HEIGHT)
    cw = ch * ratio
    if cw > width:
        cw, ch = width, width / ratio
    if ch > height:
        ch, cw = height, height * ratio
    if ch < DETAIL_MIN_HEIGHT or (cw * ch) > 0.7 * width * height:
        return None
    cx, cy = (x0 + x1) / 2 * width, (y0 + y1) / 2 * height
    left = min(max(cx - cw / 2, 0), width - cw)
    top = min(max(cy - ch / 2, 0), height - ch)
    crop = image.crop((round(left), round(top), round(left + cw), round(top + ch)))
    return crop.resize((PW, PH), Image.Resampling.LANCZOS)


@cache
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


def _night(seed: str) -> Image.Image:
    """A dark, slightly uneven ground with fine grain and a vignette, the same for the same seed."""
    rng = random.Random(seed)
    base = Image.new("RGB", (PW // 8, PH // 8), NIGHT)
    noise = Image.effect_noise((PW // 8, PH // 8), 18).convert("RGB")
    base = Image.blend(base, noise, 0.06).filter(ImageFilter.GaussianBlur(3)).resize((PW, PH), Image.Resampling.BICUBIC)
    grain = Image.effect_noise((PW, PH), 22).convert("RGB")
    plate = Image.blend(base, grain, 0.035)
    vignette = Image.new("L", (PW, PH), 0)
    draw = ImageDraw.Draw(vignette)
    cx, cy = PW / 2 + rng.uniform(-40, 40), PH * CARD_CENTER_Y
    for step in range(40):
        r = max(PW, PH) * (1 - step / 40)
        draw.ellipse((cx - r, cy - r * 1.2, cx + r, cy + r * 1.2), fill=int(150 * step / 40))
    vignette = vignette.filter(ImageFilter.GaussianBlur(60))
    return Image.composite(plate, Image.new("RGB", (PW, PH), (6, 9, 12)), vignette.point(lambda v: min(255, v + 105)))


def _balanced(text: str, count: int) -> list[str] | None:
    """The text wrapped into exactly count lines as evenly as possible, or None if it can't be."""
    best = None
    for width in range(max(len(text), 1), 0, -1):
        lines = textwrap.wrap(text, width=width, break_long_words=False) or [text]
        if len(lines) > count:
            break
        if len(lines) == count:
            best = lines
    return best


def _fit(text: str, font_name: str, max_width: int, max_size: int, max_lines: int, min_size: int = 60,
         max_height: float | None = None, spacing: float = 1.08) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """The largest size (up to max_size) at which the text wraps evenly into at most max_lines lines, each at
    most max_width pixels, and at most max_height pixels in all."""
    for size in range(max_size, min_size - 1, -6):
        font = _font(font_name, size)
        for count in range(1, max_lines + 1):
            lines = _balanced(text, count)
            if lines and all(font.getlength(line) <= max_width for line in lines) and (
                    max_height is None or _height(lines, font, spacing) <= max_height):
                return font, lines
    font = _font(font_name, min_size)
    return font, (_balanced(text, max_lines) or textwrap.wrap(text, width=max(8, len(text) // max_lines + 1)))[:max_lines]


def _draw_lines(draw: ImageDraw.ImageDraw, lines: list[str], font, top: float, fill, spacing: float = 1.08, tracking: int = 0) -> float:
    ascent, descent = font.getmetrics()
    step = (ascent + descent) * spacing
    for line in lines:
        if tracking:
            width = sum(font.getlength(ch) for ch in line) + tracking * (len(line) - 1)
            x = (PW - width) / 2
            for ch in line:
                draw.text((x, top), ch, font=font, fill=fill)
                x += font.getlength(ch) + tracking
        else:
            draw.text(((PW - font.getlength(line)) / 2, top), line, font=font, fill=fill)
        top += step
    return top


def _height(lines: list[str], font, spacing: float = 1.08) -> float:
    ascent, descent = font.getmetrics()
    return (ascent + descent) * spacing * len(lines)


def designed(spec: Card, seed: str = "") -> Image.Image:
    plate = _night(seed or spec.big)
    draw = ImageDraw.Draw(plate)
    big, small = " ".join(spec.big.split()), " ".join(spec.small.split())
    center = PH * TEXT_CENTER_Y

    def first_line(total: float) -> float:
        return min(center - total / 2, PH * TEXT_BOTTOM - total)

    if spec.kind == "dateline":
        # "November 1932": the month rides above a large year.
        match = re.fullmatch(r"(.*?)[\s,]*\b(\d{3,4}(?:\s*(?:BC|BCE|AD|CE))?)", big)
        lead, year = (match.group(1).strip(), match.group(2)) if match else ("", big)
        big_font, big_lines = _fit(year, "DMSerifDisplay-Regular.ttf", 1000, 420, 2)
        lead_font, lead_lines = _fit(lead.upper(), "Montserrat-ExtraBold.ttf", 960, 78, 1, 44) if lead else (None, [])
        small_font, small_lines = _fit(small.upper(), "Montserrat-ExtraBold.ttf", 960, 66, 2, 40)
        gap = 70
        total = (_height(lead_lines, lead_font) + 10 if lead else 0) + _height(big_lines, big_font, 1.0) \
            + (gap + _height(small_lines, small_font) if small else 0)
        top = first_line(total)
        if lead:
            top = _draw_lines(draw, lead_lines, lead_font, top, INK, 1.0, tracking=10) + 10
        bottom = _draw_lines(draw, big_lines, big_font, top, INK, 1.0)
        if small:
            rule_y = bottom + gap / 2 - 4
            draw.rectangle((PW / 2 - 130, rule_y, PW / 2 + 130, rule_y + 7), fill=ACCENT)
            _draw_lines(draw, small_lines, small_font, bottom + gap, ACCENT, 1.1, tracking=6)
    elif spec.kind == "quote":
        small_font, small_lines = _fit(f"— {small}" if small else "", "Montserrat-ExtraBold.ttf", 900, 52, 2, 36)
        mark, mark_height = _font("DMSerifDisplay-Regular.ttf", 340), 190
        room = PH * TEXT_BOTTOM - 260 - mark_height - (90 + _height(small_lines, small_font) if small else 0)
        quote_font, quote_lines = _fit(big.strip("\"“”'"), "DMSerifDisplay-Italic.ttf", 960, 132, 6, 56, room, 1.12)
        total = mark_height + _height(quote_lines, quote_font, 1.12) + (90 + _height(small_lines, small_font) if small else 0)
        top = first_line(total)
        draw.text(((PW - mark.getlength("“")) / 2, top - 70), "“", font=mark, fill=ACCENT)
        bottom = _draw_lines(draw, quote_lines, quote_font, top + mark_height, INK, 1.12)
        if small:
            _draw_lines(draw, small_lines, small_font, bottom + 90, ACCENT, 1.1)
    else:
        big_font, big_lines = _fit(big.upper(), "Anton-Regular.ttf", 1000, 300, 3, 90)
        small_font, small_lines = _fit(small, "Montserrat-ExtraBold.ttf", 940, 60, 3, 40)
        gap = 60
        total = _height(big_lines, big_font, 1.02) + (gap + _height(small_lines, small_font) if small else 0)
        top = first_line(total)
        bottom = _draw_lines(draw, big_lines, big_font, top, INK, 1.02)
        if small:
            draw.rectangle((PW / 2 - 90, bottom + gap / 2 - 4, PW / 2 + 90, bottom + gap / 2 + 3), fill=ACCENT)
            _draw_lines(draw, small_lines, small_font, bottom + gap, MUTED, 1.12)
    return plate


def tilt_for(index: int) -> float:
    """A slight, alternating tilt for cards, so consecutive prints don't sit identically."""
    return (0.9 if index % 2 else -0.9) * (1 if index % 4 < 2 else 0.6)


def trim(image: Image.Image) -> Image.Image:
    """The picture without the plain white or black margins many archive scans have (up to 12% a side)."""
    import numpy as np

    gray = np.asarray(image.convert("L"), dtype=np.float32)
    height, width = gray.shape

    def plain(line) -> bool:
        return line.std() < 6 and (line.mean() > 225 or line.mean() < 30)

    top = 0
    while top < height * 0.12 and plain(gray[top]):
        top += 1
    bottom = height
    while bottom > height * 0.88 and plain(gray[bottom - 1]):
        bottom -= 1
    left = 0
    while left < width * 0.12 and plain(gray[top:bottom, left]):
        left += 1
    right = width
    while right > width * 0.88 and plain(gray[top:bottom, right - 1]):
        right -= 1
    if (top, left, bottom, right) == (0, 0, height, width):
        return image
    return image.crop((left, top, right, bottom))


def open_image(path: Path, bold: bool = False) -> Image.Image:
    """A downloaded picture, upright, without scan margins, and graded (vivid in the bold look)."""
    image = Image.open(path)
    image = ImageOps.exif_transpose(image)
    image = trim(image.convert("RGB"))
    return vivid(image) if bold else grade(image)


def ratio_ok(image: Image.Image) -> bool:
    width, height = image.size
    return 1 / 3.2 <= width / height <= 3.2 and not math.isnan(width / height)
