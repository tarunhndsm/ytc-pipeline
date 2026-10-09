"""Find candidate pictures for a Short's beats the way an archivist would: by what they show.

Each beat names its subjects as English Wikipedia articles. Through Wikidata those lead to the subject's own
picture, its Commons category, files tagged as depicting it, and period categories for places ("San Francisco
in the 1870s"). Keyword searches, the topic article's own pictures, and other open archives (Openverse,
Wellcome Collection, the Met, the Art Institute of Chicago) fill in around them.

Every candidate carries what the credit line and the license check need, and only openly licensed pictures
come back: public domain, CC0, or CC BY (with credit).
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from .visuals import _COMMONS_OK, _get, _plain, _plain_query, commons_ok

log = logging.getLogger(__name__)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
ENWIKI_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
MEDIA_LIST = "https://en.wikipedia.org/api/rest_v1/page/media-list/"
OPENVERSE = "https://api.openverse.org/v1/images/"
WELLCOME = "https://api.wellcomecollection.org/catalogue/v2/images"
AIC = "https://api.artic.edu/api/v1/artworks/search"
MET = "https://collectionapi.metmuseum.org/public/collection/v1"
# The Met retired v1's search on 1 Oct 2026 (410 Gone); v1.1's returns one page of IDs. Objects are still on v1.
MET_SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1.1/search"
# A standard Wikimedia thumbnail width; the picker's model sees these, and the ranker a smaller copy.
THUMB = 500
# Shown full screen or as a card at this size; smaller real photos (down to SMALL_SIDE) only as a framed card.
FULL_SIDE = 1200
SMALL_SIDE = 600
# Commons asks API clients to keep parallel requests modest.
WORKERS = 4
PER_SEARCH = 30
FORMATS = ("image/jpeg", "image/png", "image/tiff", "image/webp")
# Flickr Commons scans marked "no known copyright restrictions" are public domain in the US only when published
# before this year's cutoff (95 years), so they're accepted only with a date that old.
NKCR = re.compile(r"no restrictions|no known copyright", re.I)
NKCR_BEFORE = 1931
# Openverse lets anonymous clients make about 20 requests a minute.
OPENVERSE_GAP = 3.2
_openverse_lock = threading.Lock()
_openverse_last = [0.0]
EXTMETA = "LicenseShortName|LicenseUrl|ImageDescription|DateTimeOriginal|Artist|ObjectName|Categories|Credit"


def year_of(text: str | None) -> int | None:
    """The first plausible year in a date or title (1000-2029)."""
    match = re.search(r"(?<!\d)(1[0-9]{3}|20[0-2][0-9])(?!\d)", text or "")
    return int(match.group(1)) if match else None


def beat_year(beat: dict) -> int | None:
    """The year a beat takes place, if the writer gave a sensible one."""
    try:
        year = int(beat.get("year") or 0)
    except (TypeError, ValueError):
        return None
    return year if 1 <= year <= 2100 else None


def _license_ok(license_name: str, year: int | None, credit: str = "") -> bool:
    name = (license_name or "").strip()
    if _COMMONS_OK.match(name):
        return commons_ok(name, credit)
    return bool(NKCR.search(name)) and year is not None and year < NKCR_BEFORE


def _commons_candidate(page: dict, route: str) -> dict | None:
    info = (page.get("imageinfo") or [{}])[0]
    meta = info.get("extmetadata", {})
    value = lambda key: (meta.get(key) or {}).get("value")  # noqa: E731
    license_name = (value("LicenseShortName") or "").strip()
    date = _plain(value("DateTimeOriginal")) or ""
    title = page.get("title", "")
    year = year_of(date) or year_of(title)
    width, height = info.get("width", 0), info.get("height", 0)
    if (
        page.get("missing") is not None
        or info.get("mime") not in FORMATS
        or max(width, height) < SMALL_SIDE
        or min(width, height) < 300
        or not _license_ok(license_name, year, _plain(value("Credit")) or "")
    ):
        return None
    return {
        "key": f"commons:{title}",
        "origin": "commons",
        "file": title,
        "title": title.removeprefix("File:"),
        "thumb": info.get("thumburl") or info.get("url"),
        "original": info.get("url"),
        "width": width,
        "height": height,
        "license": license_name,
        "license_url": value("LicenseUrl"),
        "artist": (_plain(value("Artist")) or "")[:200] or None,
        "description": (_plain(value("ImageDescription")) or "")[:240],
        "categories": (value("Categories") or "")[:300],
        "date": date[:60],
        "year": year,
        "page": info.get("descriptionurl"),
        "route": route,
    }


def _commons_pages(params: dict) -> list[dict]:
    found = _get(COMMONS_API, params={"action": "query", "format": "json", "prop": "imageinfo",
                                      "iiprop": "url|size|mime|extmetadata", "iiurlwidth": THUMB,
                                      "iiextmetadatafilter": EXTMETA, **params}).json()
    pages = found.get("query", {}).get("pages", {})
    pages = list(pages.values()) if isinstance(pages, dict) else pages
    return sorted(pages, key=lambda p: p.get("index", 0))


def commons_search(query: str, route: str = "search", limit: int = PER_SEARCH) -> list[dict]:
    """Openly licensed bitmaps for a Commons search (CirrusSearch syntax allowed)."""
    try:
        pages = _commons_pages({"generator": "search", "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6,
                                "gsrlimit": limit})
    except Exception as err:  # a failed search leaves the other routes
        log.info("Commons search failed for %r: %s", query, err)
        return []
    return [c for p in pages if (c := _commons_candidate(p, route))]


def commons_files(titles: list[str], route: str) -> list[dict]:
    """The given File: pages that pass the license and size rules, in the given order."""
    out: list[dict] = []
    titles = list(dict.fromkeys(t if t.startswith("File:") else f"File:{t}" for t in titles if t))
    for start in range(0, len(titles), 40):
        try:
            pages = _commons_pages({"titles": "|".join(titles[start:start + 40]), "redirects": 1})
        except Exception as err:
            log.info("Commons file lookup failed: %s", err)
            continue
        by_title = {p.get("title"): p for p in pages}
        for title in titles[start:start + 40]:
            if (page := by_title.get(title)) and (c := _commons_candidate(page, route)):
                out.append(c)
    return out


def resolve(titles: list[str]) -> dict[str, dict]:
    """English Wikipedia articles by the titles given (after redirects): canonical title, Wikidata id, free
    lead image. Titles that aren't articles are left out."""
    out: dict[str, dict] = {}
    wanted = list(dict.fromkeys(t.strip() for t in titles if t and t.strip()))
    for start in range(0, len(wanted), 50):
        batch = wanted[start:start + 50]
        try:
            found = _get(ENWIKI_API, params={"action": "query", "format": "json", "formatversion": 2, "redirects": 1,
                                             "titles": "|".join(batch), "prop": "pageprops|pageimages",
                                             "ppprop": "wikibase_item|disambiguation", "piprop": "name",
                                             "pilicense": "free"}).json()["query"]
        except Exception as err:
            log.info("Wikipedia lookup failed: %s", err)
            continue
        renamed = {r["from"]: r["to"] for r in found.get("normalized", []) + found.get("redirects", [])}
        pages = {p["title"]: p for p in found.get("pages", []) if not p.get("missing") and not p.get("invalid")}
        for title in batch:
            name = title
            for _ in range(3):
                name = renamed.get(name, name)
            page = pages.get(name)
            if not page or "disambiguation" in page.get("pageprops", {}):
                continue
            out[title] = {"title": page["title"], "qid": page.get("pageprops", {}).get("wikibase_item"),
                          "lead": f"File:{page['pageimage']}" if page.get("pageimage") else None}
    return out


def _claim_values(claims: dict, pid: str) -> list:
    return [c["mainsnak"]["datavalue"]["value"] for c in claims.get(pid, [])
            if c.get("mainsnak", {}).get("datavalue") and c.get("rank") != "deprecated"]


def entities(qids: list[str]) -> dict[str, dict]:
    """Wikidata items: their picture (P18), Commons category, coordinates, and a year."""
    out: dict[str, dict] = {}
    wanted = list(dict.fromkeys(q for q in qids if q))
    for start in range(0, len(wanted), 50):
        try:
            found = _get(WIKIDATA_API, params={"action": "wbgetentities", "format": "json", "ids": "|".join(wanted[start:start + 50]),
                                               "props": "claims|sitelinks", "sitefilter": "commonswiki"}).json()
        except Exception as err:
            log.info("Wikidata lookup failed: %s", err)
            continue
        for qid, item in found.get("entities", {}).items():
            claims = item.get("claims", {})
            category = next(iter(_claim_values(claims, "P373")), None)
            if not category:
                link = item.get("sitelinks", {}).get("commonswiki", {}).get("title", "")
                category = link.removeprefix("Category:") if link.startswith("Category:") else None
            coords = next(iter(_claim_values(claims, "P625")), None)
            years = [year_of(v.get("time", "")[1:5]) for pid in ("P585", "P580", "P571", "P569")
                     for v in _claim_values(claims, pid) if isinstance(v, dict)]
            out[qid] = {
                "images": [f"File:{v}" for v in _claim_values(claims, "P18")][:3],
                "category": category,
                "coords": (coords["latitude"], coords["longitude"]) if isinstance(coords, dict) else None,
                "year": next((y for y in years if y), None),
                "human": any(v.get("id") == "Q5" for v in _claim_values(claims, "P31") if isinstance(v, dict)),
            }
    return out


def article_media(title: str) -> list[dict]:
    """The pictures an article's editors chose, with their captions."""
    try:
        items = _get(MEDIA_LIST + urllib.parse.quote(title.replace(" ", "_"), safe="")).json().get("items", [])
    except Exception as err:
        log.info("media list failed for %r: %s", title, err)
        return []
    captions = {}
    for item in items:
        if item.get("type") == "image" and not item.get("title", "").lower().endswith((".svg", ".gif")):
            captions["File:" + item["title"].split(":", 1)[-1].replace("_", " ")] = (item.get("caption") or {}).get("text", "")
    found = commons_files(list(captions), "article")
    for candidate in found:
        candidate["caption"] = captions.get(candidate["file"], "")[:200]
    return found


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def period_categories(pairs: list[tuple[str, int]]) -> dict[tuple[str, int], list[str]]:
    """Commons categories that scope a place to the beat's time ("Philadelphia in the 1810s", "1932 in Western
    Australia", "Strasbourg in the 16th century"), for each (category, year) pair, found in one lookup."""
    names: dict[str, tuple[str, int]] = {}
    for category, year in dict.fromkeys(pairs):
        century = _ordinal(year // 100 + 1)
        for name in (f"{category} in the {year // 10 * 10}s", f"{year} in {category}", f"{category} in the {century} century"):
            names[f"Category:{name}"] = (category, year)
    out: dict[tuple[str, int], list[str]] = {}
    titles = list(names)
    for start in range(0, len(titles), 50):
        try:
            found = _get(COMMONS_API, params={"action": "query", "format": "json", "formatversion": 2, "prop": "categoryinfo",
                                              "titles": "|".join(titles[start:start + 50])}).json()["query"]
        except Exception as err:
            log.info("period category lookup failed: %s", err)
            continue
        renamed = {n["to"]: n["from"] for n in found.get("normalized", [])}
        for page in found.get("pages", []):
            files = (page.get("categoryinfo") or {}).get("files", 0)
            original = renamed.get(page["title"], page["title"])
            if not page.get("missing") and files >= 3 and original in names:
                out.setdefault(names[original], []).append(page["title"].removeprefix("Category:"))
    return out


def openverse(query: str, limit: int = 20) -> list[dict]:
    """Openverse, limited to CC0, the public domain mark, and CC BY."""
    with _openverse_lock:
        time.sleep(max(0.0, _openverse_last[0] + OPENVERSE_GAP - time.monotonic()))
        _openverse_last[0] = time.monotonic()
    try:
        results = _get(OPENVERSE, params={"q": _plain_query(query), "license": "cc0,pdm,by", "page_size": min(limit, 20),
                                          "mature": "false"}).json().get("results", [])
    except Exception as err:
        log.info("Openverse search failed for %r: %s", query, err)
        return []
    out = []
    for r in results:
        width, height = r.get("width") or 0, r.get("height") or 0
        if r.get("license") not in ("cc0", "pdm", "by") or max(width, height) < SMALL_SIDE or not r.get("url"):
            continue
        # Flickr's own copies of Commons files come back through Commons itself.
        if r.get("source") == "wikimedia":
            continue
        license_name = {"cc0": "CC0", "pdm": "Public domain"}.get(r["license"], f"CC BY {r.get('license_version') or ''}".strip())
        out.append({
            "key": f"openverse:{r['id']}", "origin": "openverse", "title": (r.get("title") or "Untitled")[:120],
            "thumb": r.get("thumbnail") or r["url"], "original": r["url"], "width": width, "height": height,
            "license": license_name, "license_url": r.get("license_url"), "artist": r.get("creator"),
            "description": " ".join(t.get("name", "") for t in (r.get("tags") or [])[:12])[:240], "categories": "",
            "date": "", "year": None, "page": r.get("foreign_landing_url"), "route": "openverse",
            "source_name": f"{(r.get('source') or 'Openverse').capitalize()} via Openverse",
        })
    return out


def wellcome(query: str, limit: int = 20) -> list[dict]:
    """Wellcome Collection images (medicine and science), public domain, CC0, or CC BY only."""
    try:
        results = _get(WELLCOME, params={"query": _plain_query(query), "pageSize": limit,
                                         "locations.license": "pdm,cc-0,cc-by"}).json().get("results", [])
    except Exception as err:
        log.info("Wellcome search failed for %r: %s", query, err)
        return []
    out = []
    for r in results:
        location = next((l for l in r.get("locations", []) if (l.get("license") or {}).get("id") in ("pdm", "cc-0", "cc-by")), None)
        if not location or not location.get("url", "").endswith("/info.json"):
            continue
        base = location["url"].removesuffix("/info.json")
        license_id = location["license"]["id"]
        ratio = float(r.get("aspectRatio") or 1.0)
        out.append({
            "key": f"wellcome:{r['id']}", "origin": "wellcome", "title": (r.get("source", {}).get("title") or "Untitled")[:120],
            "thumb": f"{base}/full/{THUMB},/0/default.jpg", "original": f"{base}/full/!1920,1920/0/default.jpg",
            # The IIIF service scales to the size asked for; the real size is checked when the picture is fetched.
            "width": 1920 if ratio >= 1 else round(1920 * ratio), "height": round(1920 / ratio) if ratio >= 1 else 1920,
            "license": {"pdm": "Public domain", "cc-0": "CC0", "cc-by": "CC BY 4.0"}[license_id],
            "license_url": location["license"].get("url"), "artist": location.get("credit") or "Wellcome Collection",
            "description": "", "categories": "", "date": "", "year": year_of(r.get("source", {}).get("title")),
            "page": f"https://wellcomecollection.org/works/{r.get('source', {}).get('id')}", "route": "wellcome",
            "source_name": "Wellcome Collection",
        })
    return out


def aic(query: str, limit: int = 15) -> list[dict]:
    """Public-domain artworks at the Art Institute of Chicago (CC0)."""
    try:
        works = _get(AIC, params={"q": _plain_query(query), "limit": limit, "query[term][is_public_domain]": "true",
                                  "fields": "id,title,image_id,thumbnail,artist_display,date_display"}).json().get("data", [])
    except Exception as err:
        log.info("AIC search failed for %r: %s", query, err)
        return []
    out = []
    for w in works:
        size = w.get("thumbnail") or {}
        width, height = size.get("width") or 0, size.get("height") or 0
        if not w.get("image_id") or max(width, height) < FULL_SIDE:
            continue
        base = f"https://www.artic.edu/iiif/2/{w['image_id']}"
        out.append({
            "key": f"aic:{w['id']}", "origin": "aic", "title": (w.get("title") or "Untitled")[:120],
            "thumb": f"{base}/full/{THUMB},/0/default.jpg", "original": f"{base}/full/1686,/0/default.jpg",
            "width": width, "height": height, "license": "CC0", "license_url": None, "artist": w.get("artist_display"),
            "description": "", "categories": "", "date": w.get("date_display") or "", "year": year_of(w.get("date_display")),
            "page": f"https://www.artic.edu/artworks/{w['id']}", "route": "aic", "source_name": "Art Institute of Chicago (CC0)",
        })
    return out


def met(query: str, limit: int = 8) -> list[dict]:
    """Public-domain objects at the Metropolitan Museum of Art (CC0)."""
    try:
        ids = _get(MET_SEARCH, params={"q": _plain_query(query), "hasImages": "true", "limit": limit}).json().get(
            "objectIDs") or []
    except Exception as err:
        log.info("Met search failed for %r: %s", query, err)
        return []
    out = []
    for object_id in ids[:limit]:
        try:
            obj = _get(f"{MET}/objects/{object_id}").json()
        except Exception:
            continue
        if not (obj.get("isPublicDomain") and obj.get("primaryImage")):
            continue
        out.append({
            "key": f"met:{object_id}", "origin": "met", "title": (obj.get("title") or "Untitled")[:120],
            "thumb": obj.get("primaryImageSmall") or obj["primaryImage"], "original": obj["primaryImage"],
            # The Met doesn't list sizes; its primary images are large, and the size is checked when fetched.
            "width": 1600, "height": 1600, "license": "CC0", "license_url": None,
            "artist": obj.get("artistDisplayName") or obj.get("culture"), "description": obj.get("objectName") or "",
            "categories": "", "date": obj.get("objectDate") or "", "year": year_of(obj.get("objectDate")),
            "page": obj.get("objectURL"), "route": "met", "source_name": "The Metropolitan Museum of Art (CC0)",
        })
    return out


def _run(tasks: list[tuple]) -> list[list[dict]]:
    """Run (function, *args) tasks, WORKERS at a time, keeping their order."""
    with ThreadPoolExecutor(WORKERS) as pool:
        futures = [pool.submit(fn, *args) for fn, *args in tasks]
        out = []
        for future in futures:
            try:
                out.append(future.result())
            except Exception as err:
                log.info("a picture search failed: %s", err)
                out.append([])
        return out


def gather(beats: list[dict], numbers: list[int], topic_titles: list[str], general: list[str]) -> tuple[dict[int, list[dict]], list[dict]]:
    """Candidates for each numbered beat, and a pool for the story in general.

    A beat is a script beat: text, visual (what the viewer should see), subjects (Wikipedia titles), year, and
    queries (keyword searches). Each file appears once: under the first beat that found it.
    """
    wanted_titles = [t for n in numbers for t in (beats[n - 1].get("subjects") or [])[:3]] + topic_titles
    articles = resolve(wanted_titles)
    items = entities([a["qid"] for a in articles.values() if a.get("qid")])

    tasks: list[tuple] = []
    owners: list[int] = []  # the beat each task searches for; 0 is the general pool

    def add(owner: int, fn, *args) -> None:
        tasks.append((fn, *args))
        owners.append(owner)

    pairs: list[tuple[str, int]] = []
    for n in numbers:
        beat = beats[n - 1]
        year = beat_year(beat)
        files: list[str] = []
        for title in (beat.get("subjects") or [])[:3]:
            article = articles.get(title)
            if not article:
                continue
            item = items.get(article.get("qid") or "", {})
            files += item.get("images", []) + ([article["lead"]] if article.get("lead") else [])
            if category := item.get("category"):
                add(n, commons_search, f'incategory:"{category}"', "category")
                if year and not item.get("human"):
                    pairs.append((category, year))
            if article.get("qid"):
                add(n, commons_search, f"haswbstatement:P180={article['qid']}", "depicts")
        if files:
            add(n, commons_files, files, "subject")
        for query in (beat.get("queries") or [])[:3]:
            add(n, commons_search, query, "search", 15)
    periods = period_categories(pairs) if pairs else {}
    for n in numbers:
        beat = beats[n - 1]
        for title in (beat.get("subjects") or [])[:3]:
            item = items.get((articles.get(title) or {}).get("qid") or "", {})
            for category in periods.get((item.get("category"), beat_year(beat) or 0), [])[:2]:
                add(n, commons_search, f'incategory:"{category}"', "period")
    for title in topic_titles[:2]:
        if article := articles.get(title):
            add(0, article_media, article["title"])
    for query in general[:8]:
        add(0, commons_search, query, "search", 12)

    results = _run(tasks)
    seen: set[str] = set()
    per_beat: dict[int, list[dict]] = {n: [] for n in numbers}
    pool: list[dict] = []
    # Entity routes before keyword searches, so a file both found stays tagged with the stronger route.
    order = {"subject": 0, "article": 1, "category": 2, "depicts": 3, "period": 4, "search": 5}
    ranked = sorted(zip(owners, results), key=lambda pair: min((order.get(c["route"], 9) for c in pair[1]), default=9))
    for owner, found in ranked:
        for candidate in found:
            if candidate["key"] in seen:
                continue
            seen.add(candidate["key"])
            (per_beat[owner] if owner else pool).append(candidate)
    log.info("candidates: %s, general %d (%d searches)", {n: len(c) for n, c in per_beat.items()}, len(pool), len(tasks))
    return per_beat, pool


def extras(beats: list[dict], numbers: list[int], seen: set[str]) -> dict[int, list[dict]]:
    """More candidates from other open archives for beats the Commons routes left weak."""
    tasks, owners = [], []
    for n in numbers:
        beat = beats[n - 1]
        query = (beat.get("queries") or [beat.get("visual", "")])[0]
        subject = next(iter(beat.get("subjects") or []), "") or query
        for fn, text in ((openverse, query), (wellcome, subject), (aic, subject), (met, subject)):
            tasks.append((fn, text))
            owners.append(n)
    out: dict[int, list[dict]] = {n: [] for n in numbers}
    for owner, found in zip(owners, _run(tasks)):
        for candidate in found:
            if candidate["key"] not in seen:
                seen.add(candidate["key"])
                out[owner].append(candidate)
    log.info("extra candidates from other archives: %s", {n: len(c) for n, c in out.items()})
    return out


def full_url(candidate: dict) -> str:
    """The URL the renderer downloads: a Commons thumbnail at the largest standard width below the original
    (Wikimedia throttles automated downloads of originals), else the source's own large rendition."""
    if candidate.get("origin") != "commons":
        return candidate["original"]
    width, height = candidate["width"], candidate["height"]
    steps = (960, 1280, 1920, 2560)
    # Portrait pictures need more width to fill a tall frame.
    target = 1920 if height > width else 2560
    step = max((s for s in steps if s < width and s <= target), default=None)
    if step is None and candidate.get("original", "").lower().endswith((".jpg", ".jpeg", ".png")):
        return candidate["original"]
    step = step or 960
    try:
        found = _get(COMMONS_API, params={"action": "query", "format": "json", "titles": candidate["file"], "prop": "imageinfo",
                                          "iiprop": "url", "iiurlwidth": step}).json()
        page = next(iter(found.get("query", {}).get("pages", {}).values()), {})
        return (page.get("imageinfo") or [{}])[0].get("thumburl") or candidate["original"]
    except Exception as err:
        log.info("couldn't get a %d px copy of %s: %s", step, candidate["file"], err)
        return candidate["original"]
