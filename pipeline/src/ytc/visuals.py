"""Fetch or generate each beat's visual, recording where every asset came from."""

from __future__ import annotations

import base64
import html
import io
import json
import logging
import os
import random
import re
import shutil
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import requests
from PIL import Image, ImageColor, ImageDraw, ImageOps

from .spec import Beat

log = logging.getLogger(__name__)

_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
_TAG = re.compile(r"<[^>]+>")
# Share-alike, non-commercial and no-derivatives licenses are excluded on purpose. Ported CC BY
# licenses ("CC BY 3.0 au") allow the same commercial reuse as the international ones.
_COMMONS_OK = re.compile(r"^(public domain|pd\b|pd-|cc0|cc by \d(\.\d)?( [a-z]{2})?$|cc-by-\d(\.\d)?(-[a-z]{2})?$)", re.I)
MIN_SIDE = 1200


def commons_ok(license_name: str, credit: str = "") -> bool:
    """Whether a Commons file's license lets us reuse it. A CC0 on a scan from the Internet Archive was set by whoever
    scanned a publisher's book (Gita Press and Tej Kumar Book Depot prints came this way), not by its rights holder."""
    license_name = license_name.strip()
    return bool(_COMMONS_OK.match(license_name)) and not (license_name.upper().startswith("CC0") and "archive.org" in credit)
# Wikimedia serves thumbnails only at these widths (https://w.wiki/GHai) and throttles automated downloads
# of originals, hard from cloud servers.
_THUMB_STEPS = (960, 1280, 1920, 3840)
_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_SEARCH_SYNTAX = re.compile(r"\b(?:intitle|incategory|insource|deepcat):|\bfiletype:\S+|(?<!\w)-\S+", re.I)
# Words that say nothing about an artwork's subject.
_GENERIC = frozenset(
    "about after and before by circa during for from great historic historical history into near new "
    "of old over the under with century drawing engraving etching illustration image lithograph painting "
    "photo photograph picture portrait print scene view woodcut".split()
)
_RENDER_ONLY = ("motion", "focus_x", "focus_y", "reuse", "box", "crop", "card")
# A picture the picker chose by looking at it may be smaller than MIN_SIDE: it's then shown whole, as a card.
URL_MIN_SIDE = 600


@dataclass
class Asset:
    path: Path
    kind: Literal["video", "image", "card"]
    credit: dict
    key: tuple[str, str] | None = None


def _user_agent() -> str:
    # Wikimedia and the Art Institute throttle clients whose User-Agent carries no contact details.
    contact = os.environ.get("YTC_CONTACT", "personal project")
    return os.environ.get("YTC_USER_AGENT") or f"ytc-shorts-pipeline/0.1 ({contact})"


def _get(url: str, headers: dict | None = None, timeout: float = 30, **kwargs) -> requests.Response:
    ua = _user_agent()
    merged = {"User-Agent": ua, "AIC-User-Agent": ua, **(headers or {})}
    for attempt in range(4):
        resp = requests.get(url, headers=merged, timeout=timeout, **kwargs)
        if resp.status_code not in (429, 503) or attempt == 3:
            break
        retry_after = resp.headers.get("Retry-After", "")
        wait = float(retry_after) if retry_after.isdigit() else 2.0 * 2**attempt
        if wait > 60:
            break
        log.info("%s returned %d, retrying in %.0fs", urllib.parse.urlsplit(url).netloc, resp.status_code, wait)
        time.sleep(wait)
    resp.raise_for_status()
    return resp


def _download(url: str, path: Path) -> Path:
    path.write_bytes(_get(url, timeout=180).content)
    return path


def _download_image(url: str, path: Path, min_side: int = 0) -> Path:
    # Saving drops the EXIF orientation, so a phone photo is turned upright first.
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(_get(url, timeout=180).content)))
    if max(image.size) < min_side:
        raise LookupError(f"{url} is only {image.width}x{image.height}")
    image.convert("RGB").save(path, quality=95)
    return path


def _plain(value: str | None) -> str | None:
    return html.unescape(_TAG.sub("", value)).strip() if value else value


def _plain_query(query: str) -> str:
    """The query without Commons search syntax, for sources that don't understand it."""
    return " ".join(_SEARCH_SYNTAX.sub(" ", query).replace('"', " ").replace("_", " ").split())


def _terms(query: str) -> list[str]:
    words = re.findall(r"[^\W\d_]{3,}|\d{3,4}", _plain_query(query).lower())
    return list(dict.fromkeys(w for w in words if w not in _GENERIC))


def _matches(terms: list[str], *fields: str | None) -> int:
    """How many query terms appear in the fields, allowing plurals."""
    haystack = " ".join(f for f in fields if f).lower()
    stems = (t[:-1] if t.endswith("s") and not t.endswith("ss") and len(t) > 3 else t for t in terms)
    return sum(1 for stem in stems if re.search(rf"\b{re.escape(stem)}(?:s|es)?\b", haystack))


def _needed(terms: list[str]) -> int:
    # Museum searches rank their whole collection rather than filtering it, so most of the subject has to match.
    return len(terms) if len(terms) <= 2 else -(-2 * len(terms) // 3)


def _nasa(query: str, out: Path, used: set) -> Asset:
    found = _get("https://images-api.nasa.gov/search", params={"q": _plain_query(query), "media_type": "image"}).json()
    items = [it for it in found["collection"]["items"] if ("nasa", it["data"][0]["nasa_id"]) not in used]
    if not items:
        raise LookupError(f"NASA library has no unused images for {query!r}")
    item = items[0]
    meta = item["data"][0]
    renditions = _get(item["href"]).json()
    url = next((u for u in renditions if u.endswith("~orig.jpg")), None) or next(
        (u for u in renditions if u.endswith("~large.jpg")), None
    )
    if url is None:
        raise LookupError(f"NASA item {meta['nasa_id']} has no JPEG rendition")
    _download(url.replace("http://", "https://"), out.with_suffix(".jpg"))
    return Asset(
        out.with_suffix(".jpg"),
        "image",
        {
            "source": "NASA Image and Video Library",
            "title": meta.get("title"),
            "credit": meta.get("secondary_creator") or meta.get("center") or "NASA",
            "url": f"https://images.nasa.gov/details/{urllib.parse.quote(meta['nasa_id'])}",
        },
        ("nasa", meta["nasa_id"]),
    )


def _commons_url(title: str, info: dict) -> str:
    """The largest standard thumbnail smaller than the original, or the original when it is small."""
    step = next((s for s in reversed(_THUMB_STEPS) if s < info["width"]), None)
    if step is None:
        return info["url"]
    found = _get(
        _COMMONS_API,
        params={"action": "query", "format": "json", "titles": title, "prop": "imageinfo", "iiprop": "url", "iiurlwidth": step},
    ).json()
    page = next(iter(found.get("query", {}).get("pages", {}).values()), {})
    return (page.get("imageinfo") or [{}])[0].get("thumburl") or info["url"]


def _commons(query: str, out: Path, used: set) -> Asset:
    found = _get(
        _COMMONS_API,
        params={
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": 6,
            "gsrlimit": 30,
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
        },
    ).json()
    pages = sorted(found.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    candidates = []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        license_name = meta.get("LicenseShortName", {}).get("value", "")
        if (
            ("commons", page["title"]) not in used
            and info.get("mime") in ("image/jpeg", "image/png")
            and max(info.get("width", 0), info.get("height", 0)) >= MIN_SIDE
            and commons_ok(license_name, _plain(meta.get("Credit", {}).get("value", "")))
        ):
            candidates.append((page, info, meta, license_name))
    if not candidates:
        raise LookupError(f"Commons has no reusable images for {query!r}")
    page, info, meta, license_name = candidates[0]
    _download_image(_commons_url(page["title"], info), out.with_suffix(".jpg"))
    return Asset(
        out.with_suffix(".jpg"),
        "image",
        {
            "source": "Wikimedia Commons",
            "title": page["title"].removeprefix("File:"),
            "credit": _plain(meta.get("Artist", {}).get("value")) or "Unknown author",
            "license": license_name,
            "url": info.get("descriptionurl"),
        },
        ("commons", page["title"]),
    )


def _met(query: str, out: Path, used: set) -> Asset:
    base = "https://collectionapi.metmuseum.org/public/collection/v1"
    terms = _terms(query)
    # The Met retired v1's search on 1 Oct 2026 (410 Gone); v1.1's returns one page of IDs. Objects are still on v1.
    ids = _get("https://collectionapi.metmuseum.org/public/collection/v1.1/search",
               params={"hasImages": "true", "q": _plain_query(query), "limit": 30}).json().get("objectIDs") or []
    candidates = []
    for object_id in ids[:30]:
        if ("met", str(object_id)) in used:
            continue
        try:
            obj = _get(f"{base}/objects/{object_id}").json()
        except requests.HTTPError:
            # The search index still lists some withdrawn objects.
            continue
        if not (obj.get("isPublicDomain") and obj.get("primaryImage")):
            continue
        tags = " ".join(tag.get("term", "") for tag in obj.get("tags") or [])
        fields = ("title", "objectName", "artistDisplayName", "culture", "period", "objectDate", "city", "country")
        hits = _matches(terms, tags, *(obj.get(f) for f in fields))
        if hits >= _needed(terms):
            candidates.append((hits, obj))
            if hits == len(terms):
                break
    for _, obj in sorted(candidates, key=lambda c: -c[0]):
        try:
            _download_image(obj["primaryImage"], out.with_suffix(".jpg"), MIN_SIDE)
        except LookupError as exc:
            log.info("%s: skipping Met object %s (%s)", out.name, obj["objectID"], exc)
            continue
        return Asset(
            out.with_suffix(".jpg"),
            "image",
            {
                "source": "The Metropolitan Museum of Art (CC0)",
                "title": obj.get("title"),
                "credit": obj.get("artistDisplayName") or obj.get("culture") or "Unknown artist",
                "license": "CC0",
                "date": obj.get("objectDate"),
                "url": obj.get("objectURL"),
            },
            ("met", str(obj["objectID"])),
        )
    raise LookupError(f"The Met has no public-domain image matching {query!r}")


def _aic(query: str, out: Path, used: set) -> Asset:
    terms = _terms(query)
    found = _get(
        "https://api.artic.edu/api/v1/artworks/search",
        params={
            "q": _plain_query(query),
            "limit": 30,
            "fields": "id,title,image_id,thumbnail,artist_display,date_display,place_of_origin,"
            "term_titles,subject_titles,classification_titles",
            "query[term][is_public_domain]": "true",
        },
    ).json()
    works = []
    for work in found.get("data", []):
        size = work.get("thumbnail") or {}
        if (
            not work.get("image_id")
            or ("aic", str(work["id"])) in used
            or max(size.get("width") or 0, size.get("height") or 0) < MIN_SIDE
        ):
            continue
        hits = _matches(
            terms,
            work.get("title"),
            work.get("artist_display"),
            work.get("date_display"),
            work.get("place_of_origin"),
            *(work.get("term_titles") or []),
            *(work.get("subject_titles") or []),
            *(work.get("classification_titles") or []),
        )
        if hits >= _needed(terms):
            works.append((hits, work))
    if not works:
        raise LookupError(f"Art Institute of Chicago has no public-domain image matching {query!r}")
    work = max(works, key=lambda c: c[0])[1]
    _download_image(f"https://www.artic.edu/iiif/2/{work['image_id']}/full/1686,/0/default.jpg", out.with_suffix(".jpg"))
    return Asset(
        out.with_suffix(".jpg"),
        "image",
        {
            "source": "Art Institute of Chicago (CC0)",
            "title": work.get("title"),
            "credit": work.get("artist_display"),
            "license": "CC0",
            "date": work.get("date_display"),
            "url": f"https://www.artic.edu/artworks/{work['id']}",
        },
        ("aic", str(work["id"])),
    )


def _pexels(query: str, out: Path, used: set, rng: random.Random, seconds: float) -> Asset:
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        raise RuntimeError("PEXELS_API_KEY is not set")
    found = _get(
        "https://api.pexels.com/videos/search",
        params={"query": query, "orientation": "portrait", "per_page": 30},
        headers={"Authorization": key},
    ).json()
    videos = [v for v in found.get("videos", []) if ("pexels", str(v["id"])) not in used]
    candidates = [v for v in videos if v.get("duration", 0) >= seconds] or videos
    if not candidates:
        raise LookupError(f"Pexels has no unused portrait videos for {query!r}")
    video = rng.choice(candidates[:6])
    files = [
        f
        for f in video["video_files"]
        if f.get("file_type") == "video/mp4" and f.get("width") and f.get("height") and f["height"] > f["width"]
    ]
    if not files:
        raise LookupError(f"Pexels video {video['id']} has no portrait MP4 rendition")
    files.sort(key=lambda f: abs(f["height"] - 1920))
    _download(files[0]["link"], out.with_suffix(".mp4"))
    return Asset(
        out.with_suffix(".mp4"),
        "video",
        {"source": "Pexels", "credit": video["user"]["name"], "url": video["url"]},
        ("pexels", str(video["id"])),
    )


def _pixabay(query: str, out: Path, used: set, rng: random.Random, seconds: float) -> Asset:
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        raise RuntimeError("PIXABAY_API_KEY is not set")
    found = _get(
        "https://pixabay.com/api/videos/", params={"key": key, "q": query, "per_page": 30, "safesearch": "true"}
    ).json()
    hits = [h for h in found.get("hits", []) if ("pixabay", str(h["id"])) not in used]
    candidates = [h for h in hits if h.get("duration", 0) >= seconds] or hits
    if not candidates:
        raise LookupError(f"Pixabay has no unused videos for {query!r}")
    hit = rng.choice(candidates[:6])
    rendition = next((hit["videos"][k] for k in ("large", "medium", "small") if hit["videos"].get(k, {}).get("url")), None)
    if rendition is None:
        raise LookupError(f"Pixabay video {hit['id']} has no downloadable rendition")
    _download(rendition["url"], out.with_suffix(".mp4"))
    return Asset(
        out.with_suffix(".mp4"),
        "video",
        {"source": "Pixabay", "credit": hit.get("user"), "url": hit.get("pageURL")},
        ("pixabay", str(hit["id"])),
    )


def _ai(prompt: str, out: Path, seed: int) -> Asset:
    provider = os.environ.get("YTC_IMAGE_PROVIDER", "cloudflare")
    path = out.with_suffix(".jpg")
    if provider == "cloudflare":
        account, token = os.environ["CLOUDFLARE_ACCOUNT_ID"], os.environ["CLOUDFLARE_API_TOKEN"]
        resp = requests.post(
            f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/black-forest-labs/flux-1-schnell",
            headers={"Authorization": f"Bearer {token}"},
            json={"prompt": prompt, "steps": 8, "seed": seed},
            timeout=180,
        )
        resp.raise_for_status()
        Image.open(io.BytesIO(base64.b64decode(resp.json()["result"]["image"]))).convert("RGB").save(path, quality=95)
        model = "FLUX.1-schnell via Cloudflare Workers AI"
    else:
        raise ValueError(f"unknown image provider {provider!r}")
    return Asset(path, "image", {"source": "AI generated", "model": model, "prompt": prompt, "seed": seed})


def _file(path_text: str, out: Path, base_dir: Path) -> Asset:
    src = (base_dir / path_text).expanduser().resolve()
    kind: Literal["video", "image"] = "video" if src.suffix.lower() in _VIDEO_SUFFIXES else "image"
    dest = out.with_suffix(src.suffix.lower())
    shutil.copyfile(src, dest)
    return Asset(dest, kind, {"source": "local file", "path": str(src)})


def _gradient(hex_color: str, out: Path) -> Asset:
    top = ImageColor.getrgb(hex_color)
    bottom = tuple(int(c * 0.3) for c in top)
    image = Image.new("RGB", (1080, 1920))
    draw = ImageDraw.Draw(image)
    for y in range(1920):
        f = y / 1919
        draw.line([(0, y), (1079, y)], fill=tuple(int(top[i] * (1 - f) + bottom[i] * f) for i in range(3)))
    image.save(out.with_suffix(".png"))
    return Asset(out.with_suffix(".png"), "image", {"source": "generated gradient", "color": hex_color})


def _url(beat: Beat, out: Path) -> Asset:
    """A picture the picker already chose, license checked, with its credit."""
    visual = beat.visual
    if not visual.url:
        raise LookupError("visual.url is required for source 'url'")
    path = _download_image(visual.url, out.with_suffix(".jpg"), URL_MIN_SIDE)
    credit = {k: v for k, v in visual.credit.items() if v}
    return Asset(path, "image", credit, (credit.get("source") or "url", visual.credit.get("url") or visual.url))


def _fresh(beat: Beat, out: Path, base_dir: Path, used: set, rng: random.Random, seconds: float) -> Asset:
    visual = beat.visual
    query = visual.query or beat.text
    for source in [visual.source, *visual.fallbacks]:
        try:
            match source:
                case "url":
                    return _url(beat, out)
                case "card":
                    if not visual.card:
                        raise LookupError("visual.card is required for source 'card'")
                    # Drawn by the renderer; nothing to download.
                    return Asset(out.with_suffix(".card"), "card", {"source": "designed card"})
                case "nasa":
                    return _nasa(query, out, used)
                case "commons":
                    return _commons(query, out, used)
                case "met":
                    return _met(query, out, used)
                case "aic":
                    return _aic(query, out, used)
                case "pexels":
                    return _pexels(query, out, used, rng, seconds)
                case "pixabay":
                    return _pixabay(query, out, used, rng, seconds)
                case "ai":
                    return _ai(visual.prompt or query, out, rng.randrange(1 << 31))
                case "file":
                    if not visual.path:
                        raise LookupError("visual.path is required for source 'file'")
                    return _file(visual.path, out, base_dir)
                case "color":
                    return _gradient(visual.color, out)
        except (requests.RequestException, LookupError, RuntimeError, KeyError, OSError, ValueError) as exc:
            log.warning("%s: %s failed (%s)", out.name, source, exc)
    raise LookupError(f"{out.name}: every visual source failed for {query!r}")


def _search_view(visual: dict) -> dict:
    return {k: v for k, v in visual.items() if k not in _RENDER_ONLY}


def _cached(beat: Beat, out: Path) -> Asset | None:
    """The beat's earlier download, if its search settings haven't changed since."""
    sidecar = out.with_suffix(".json")
    if not sidecar.exists():
        return None
    cached = json.loads(sidecar.read_text(encoding="utf-8"))
    path = Path(cached["path"])
    if not path.exists():
        # The episode folder may have moved, e.g. into a cloud render container.
        path = out.parent / path.name
    if _search_view(cached["visual"]) != _search_view(beat.visual.model_dump()) or not path.exists():
        return None
    return Asset(path, cached["kind"], cached["credit"], tuple(cached["key"]) if cached.get("key") else None)


def cached_keys(beats: list[Beat], out_dir: Path) -> set[tuple[str, str]]:
    """Images that beats will keep from earlier renders, so a re-fetched beat can't take one of them."""
    keys = set()
    for index, beat in enumerate(beats):
        asset = None if beat.visual.reuse else _cached(beat, out_dir / f"beat{index:02d}")
        if asset and asset.key:
            keys.add(asset.key)
    return keys


def fetch(
    beat: Beat,
    index: int,
    out_dir: Path,
    base_dir: Path,
    used: set[tuple[str, str]],
    rng: random.Random,
    seconds: float,
) -> Asset:
    """Return the beat's asset, reusing the cached download while the beat's search settings are unchanged."""
    out = out_dir / f"beat{index:02d}"
    wanted = beat.visual.model_dump()
    cached = _cached(beat, out)
    if cached:
        if cached.key:
            used.add(cached.key)
        return cached
    asset = _fresh(beat, out, base_dir, used, rng, seconds)
    if asset.key:
        used.add(asset.key)
    out.with_suffix(".json").write_text(
        json.dumps({"visual": wanted, "path": str(asset.path), "kind": asset.kind, "credit": asset.credit, "key": asset.key}, indent=2),
        encoding="utf-8",
    )
    return asset
