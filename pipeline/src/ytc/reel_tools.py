"""What a brand Reel is made with, on the Mac now and in a studio run on Modal later: finding open-access pictures
(Cleveland's CC0, the Met's public domain, and Wikimedia Commons files whose license allows commercial reuse without
share-alike), laying candidates and a spec's crops out on sheets to look at, fetching the source texts, rendering on
the brand's Gemini project, and checking a render.

    python -m ytc.reel_tools art <query> [--met] [--limit N]
    python -m ytc.reel_tools commons <query> [--limit N]
    python -m ytc.reel_tools credit "<File title>"...
    python -m ytc.reel_tools sheet <out.jpg> <commons:<File title> | cma:<id> | met:<id>>...
    python -m ytc.reel_tools crops <short.yaml> <out.jpg>
    python -m ytc.reel_tools fetch <out dir> <url>... [--find word,word]
    python -m ytc.reel_tools render <short.yaml>
    python -m ytc.reel_tools qa <reel folder> <out.jpg>
    python -m ytc.reel_tools status [free slots to list]
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

from . import ff, llm, plates, visuals

Image.MAX_IMAGE_PIXELS = None
CLEVELAND = "https://openaccess-api.clevelandart.org/api/artworks/"
MET = "https://collectionapi.metmuseum.org/public/collection/v1"
# The Met retired v1's search on 1 Oct 2026 (410 Gone); v1.1's returns one page of IDs. Objects are still on v1.
MET_SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1.1/search"
# A standard Wikimedia thumbnail width: others are refused with HTTP 400.
THUMB_WIDTH = 500
CACHE = Path(tempfile.gettempdir()) / "reel_pictures"
# The Hugging Face models the Mac's Reel tools load (Whisper for the speech check, SigLIP, the depth model): not in
# /tmp, which macOS empties at every restart.
MODEL_CACHE = str(Path.home() / ".cache" / "ytc-hf")


# --- finding pictures ----------------------------------------------------------------------------------------------

def cleveland(query: str, limit: int = 25) -> list[dict]:
    """Cleveland's CC0 artworks with images, sized as the print JPEG a spec links to (not the far larger TIFF)."""
    found = visuals._get(CLEVELAND, params={"q": query, "has_image": 1, "cc0": 1, "limit": limit,
                                            "fields": "id,accession_number,title,creation_date,culture,images,url"}).json()
    hits = []
    for art in found.get("data", []):
        images = art.get("images") or {}
        image = images.get("print") or {}
        hits.append({"ref": f"cma:{art['id']}", "title": art["title"], "date": art.get("creation_date"),
                     "culture": (art.get("culture") or [""])[0], "width": image.get("width"),
                     "height": image.get("height"), "url": image.get("url"), "page": art.get("url"),
                     "thumb": (images.get("web") or {}).get("url"),
                     "license": "CC0", "allowed": True, "source": "The Cleveland Museum of Art"})
    return hits


def met(query: str, limit: int = 25, workers: int = 8) -> list[dict]:
    """The Met's public-domain objects with images (its API gives no image sizes)."""
    ids = visuals._get(MET_SEARCH, params={"hasImages": "true", "q": query, "limit": limit}).json().get("objectIDs") or []

    def fetch(object_id: int) -> dict:
        try:
            return visuals._get(f"{MET}/objects/{object_id}").json()
        except requests.HTTPError:
            # The search index still lists some withdrawn objects.
            return {}

    with ThreadPoolExecutor(workers) as pool:
        objects = list(pool.map(fetch, ids))
    return [{"ref": f"met:{o['objectID']}", "title": o["title"], "date": o.get("objectDate"), "culture": o.get("culture"),
             "width": None, "height": None, "url": o["primaryImage"], "page": o.get("objectURL"),
             "thumb": o.get("primaryImageSmall"),
             "license": "Public domain", "allowed": True, "source": "The Metropolitan Museum of Art"}
            for o in objects if o.get("isPublicDomain") and o.get("primaryImage")]


def commons(query: str, limit: int = 20) -> list[dict]:
    """Commons files for a search, each `allowed` only if its license permits commercial reuse without share-alike."""
    found = visuals._get(visuals._COMMONS_API, params={
        "action": "query", "format": "json", "generator": "search", "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": 6, "gsrlimit": limit, "prop": "imageinfo", "iiprop": "size|url|extmetadata",
        "iiurlwidth": THUMB_WIDTH, "iiextmetadatafilter": "LicenseShortName|DateTimeOriginal|Artist|Credit"}).json()
    hits = []
    for page in sorted(found.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0)):
        info = page["imageinfo"][0]
        meta = info.get("extmetadata", {})
        license_name = meta.get("LicenseShortName", {}).get("value", "").strip()
        hits.append({"ref": f"commons:{page['title'][5:]}", "title": page["title"][5:], "width": info["width"],
                     "height": info["height"], "license": license_name,
                     "allowed": visuals.commons_ok(license_name, visuals._plain(meta.get("Credit", {}).get("value", ""))),
                     "date": visuals._plain(meta.get("DateTimeOriginal", {}).get("value", ""))[:40],
                     "artist": visuals._plain(meta.get("Artist", {}).get("value", ""))[:90],
                     # Commons appends tracking parameters (?utm_source=...) to its file addresses.
                     "url": info["url"].split("?")[0], "page": info.get("descriptionurl"),
                     "thumb": info.get("thumburl"), "source": "Wikimedia Commons"})
    return hits


def credits(titles: list[str]) -> list[dict]:
    """Who made each Commons file and under what license, for a Reel's credits."""
    found = visuals._get(visuals._COMMONS_API, timeout=60, params={
        "action": "query", "format": "json", "titles": "|".join(f"File:{t}" for t in titles), "prop": "imageinfo",
        "iiprop": "extmetadata|url", "iiextmetadatafilter": "Artist|Credit|LicenseShortName|DateTimeOriginal"}).json()
    out = []
    for page in found["query"]["pages"].values():
        info = page.get("imageinfo", [{}])[0]
        meta = {k: visuals._plain(v.get("value", "")) for k, v in info.get("extmetadata", {}).items()}
        license_name = meta.get("LicenseShortName", "").strip()
        out.append({"title": page["title"][5:], "artist": meta.get("Artist", "")[:90],
                    "credit": meta.get("Credit", "")[:90], "license": license_name,
                    "allowed": visuals.commons_ok(license_name, meta.get("Credit", "")),
                    "date": meta.get("DateTimeOriginal", "")[:30], "page": info.get("descriptionurl", "")})
    return out


# --- sheets to look at ---------------------------------------------------------------------------------------------

def _preview(ref: str) -> tuple[str, bytes]:
    kind, key = ref.split(":", 1)
    if kind == "commons":
        found = visuals._get(visuals._COMMONS_API, params={"action": "query", "format": "json", "titles": f"File:{key}",
                                                           "prop": "imageinfo", "iiprop": "url|size",
                                                           "iiurlwidth": THUMB_WIDTH}).json()
        info = next(iter(found["query"]["pages"].values()))["imageinfo"][0]
        return f"{key[:48]} {info['width']}x{info['height']}", visuals._get(info["thumburl"]).content
    if kind == "cma":
        art = visuals._get(f"{CLEVELAND}{key}").json()["data"]
        size = art["images"].get("print") or art["images"]["full"]
        return (f"CMA {key} {art['title'][:40]} {size['width']}x{size['height']}",
                visuals._get(art["images"]["web"]["url"]).content)
    obj = visuals._get(f"{MET}/objects/{key}").json()
    return f"MET {key} {obj['title'][:44]}", visuals._get(obj["primaryImageSmall"]).content


def _grid(cells: list[tuple[str, Image.Image | None]], out: Path, cell: tuple[int, int], cols: int) -> None:
    cols = min(cols, len(cells))
    rows = -(-len(cells) // cols)
    canvas = Image.new("RGB", (cols * cell[0], rows * cell[1]), "white")
    draw, font = ImageDraw.Draw(canvas), ImageFont.load_default()
    for n, (label, image) in enumerate(cells):
        x, y = (n % cols) * cell[0], (n // cols) * cell[1]
        if image is not None:
            image.thumbnail((cell[0] - 8, cell[1] - 34))
            canvas.paste(image, (x + 4, y + 4))
        draw.text((x + 4, y + cell[1] - 26), label, fill="red" if "CARD" in label else "black", font=font)
    canvas.save(out, quality=88)


def sheet(refs: list[str], out: Path) -> list[str]:
    """Candidate pictures side by side, numbered, to choose between by looking."""
    def fetch(ref: str) -> tuple[str, Image.Image | None]:
        try:
            label, data = _preview(ref)
            return label, Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as err:
            return f"FAILED {ref[:40]}: {err}"[:70], None

    with ThreadPoolExecutor(6) as pool:
        cells = list(pool.map(fetch, refs))
    _grid([(f"{n}. {label}", image) for n, (label, image) in enumerate(cells, start=1)], out, (360, 560), 4)
    return [f"{n}. {label}" for n, (label, _) in enumerate(cells, start=1)]


def _picture(url: str) -> Image.Image:
    """A spec's picture as the renderer opens it: scan margins trimmed, which a crop box is relative to."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / hashlib.sha1(url.encode()).hexdigest()
    if not path.exists():
        path.write_bytes(visuals._get(url, timeout=180).content)
    return plates.open_image(path)


def crops(spec_path: Path, out: Path) -> list[dict]:
    """Every beat's picture as the spec crops it, marking the ones the renderer will show whole as a small card."""
    import yaml

    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    rows, cells = [], []
    for n, beat in enumerate(spec["beats"], start=1):
        visual = beat.get("visual", {})
        if "url" not in visual:
            rows.append({"beat": n, "reuse": visual.get("reuse")})
            cells.append((f"{n}: reuse {visual.get('reuse')}", None))
            continue
        image = _picture(visual["url"])
        crop = plates.region(image, visual["crop"]) if visual.get("crop") else image
        layout = plates.layout(crop)
        rows.append({"beat": n, "width": crop.width, "height": crop.height, "ratio": round(crop.width / crop.height, 2),
                     "layout": layout, "trimmed": list(image.size), "text": beat["text"]})
        cells.append((f"{n}: {crop.width}x{crop.height} r{crop.width / crop.height:.2f} "
                      f"{'' if layout == 'cover' else 'CARD'}", crop.copy()))
    _grid(cells, out, (300, 480), 5)
    return rows


# --- sources, render, checks ---------------------------------------------------------------------------------------

def _text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style).*?</\1>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page))).strip()


def fetch(urls: list[str], out_dir: Path, words: list[str] | None = None) -> list[dict]:
    """Save each source page as plain text, with the passages around `words` for a quick look."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for url in urls:
        try:
            response = visuals._get(url, timeout=40)
        except requests.RequestException as err:
            pages.append({"url": url, "error": str(err)})
            continue
        text = _text(response.content.decode("utf-8", errors="replace"))
        path = out_dir / f"{re.sub(r'[^A-Za-z0-9]+', '_', url.split('//', 1)[-1])[-60:]}.txt"
        path.write_text(text, encoding="utf-8")
        passages = [text[max(0, m.start() - 220): m.start() + 260] for word in words or []
                    for m in list(re.finditer(re.escape(word), text, re.IGNORECASE))[:2]]
        pages.append({"url": url, "chars": len(text), "path": str(path), "passages": passages})
    return pages


def render(spec_path: Path) -> Path:
    """Render a Reel with its narration on the brand's own Gemini project, never the YouTube studio's."""
    os.environ.setdefault("HF_HOME", MODEL_CACHE)
    llm.use_brand_project()
    from .render import make_short

    return make_short(spec_path)


def qa(folder: Path, out: Path) -> dict:
    """A render's format and loudness, and a sheet of one frame from the middle of each shot."""
    video = folder / f"{folder.name}.mp4"
    manifest = json.loads((folder / "work" / "manifest.json").read_text(encoding="utf-8"))
    info = subprocess.run([ff.exe(), "-hide_banner", "-i", str(video)], capture_output=True, text=True).stderr
    loud = subprocess.run([ff.exe(), "-hide_banner", "-nostats", "-i", str(video), "-af", "loudnorm=print_format=json",
                           "-f", "null", "-"], capture_output=True, text=True).stderr
    stats = json.loads(loud[loud.rindex("{"):loud.rindex("}") + 1])
    times = [(shot["start"] + shot["end"]) / 2 for beat in manifest["beats"] for shot in (beat.get("shots") or [beat])]
    cells = []
    with tempfile.TemporaryDirectory() as tmp:
        for t in times:
            frame = Path(tmp) / f"{t:06.2f}.png"
            subprocess.run([ff.exe(), "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", str(frame)],
                           check=True)
            cells.append((f"{t:.1f} s", Image.open(frame).convert("RGB")))
    _grid(cells, out, (368, 680), 6)
    return {"streams": [line.strip() for line in info.splitlines() if re.search(r"Duration|Stream #", line)],
            "lufs": float(stats["input_i"]), "true_peak": float(stats["input_tp"]), "range": float(stats["input_lra"]),
            "frames": [round(t, 2) for t in times]}


def status(wanted: int = 3) -> list[str]:
    """One line per Reel (scheduled, or how far its review has got), then the next free posting slots."""
    from . import reel_cloud, reels

    lines = []
    for folder in sorted(p for p in reels.REELS.glob("ak*") if p.is_dir()):
        verdict = reels._read(folder / reels.REVIEW)
        if due := reel_cloud._due(folder):
            state = f"scheduled for {due.astimezone(reel_cloud.IST):%a %d %b %H:%M} IST"
        elif not (folder / f"{folder.name}.mp4").exists():
            state = "not rendered"
        elif (judged := reels.judgments(folder)) < reels.RUNS:
            state = f"rendered; {judged} of {reels.RUNS} judgments of this render"
        else:
            state = f"held: {why}" if (why := reels.unshippable(folder)) else "passed; waiting for a slot"
        lines.append(f"{folder.name}  {state}" + (f"  (review {verdict['total']})" if verdict else ""))
    slots = reel_cloud.free_slots(datetime.now(timezone.utc), wanted)
    return lines + ["next free slots: " + ", ".join(f"{s:%a %d %b %H:%M}" for s in slots)]


def _print_hits(hits: list[dict]) -> None:
    for hit in hits:
        size = f"{hit['width']}x{hit['height']}" if hit.get("width") else "size ?"
        mark = "" if hit["allowed"] else "  NOT ALLOWED"
        print(f"{hit['ref']} | {size} | {hit['license']}{mark} | {hit['title'][:100]} | {hit.get('date') or ''}\n"
              f"    {hit['url']}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ytc.reel_tools", description="tools for the brand's Reels")
    commands = parser.add_subparsers(dest="command", required=True)
    art = commands.add_parser("art", help="Cleveland (CC0) or, with --met, the Met (public domain)")
    art.add_argument("query", nargs="+")
    art.add_argument("--met", action="store_true")
    art.add_argument("--limit", type=int, default=25)
    found = commands.add_parser("commons", help="Wikimedia Commons, with each file's license")
    found.add_argument("query", nargs="+")
    found.add_argument("--limit", type=int, default=20)
    commands.add_parser("credit", help="who made each Commons file").add_argument("titles", nargs="+")
    candidates = commands.add_parser("sheet", help="candidate pictures on one numbered sheet")
    candidates.add_argument("out", type=Path)
    candidates.add_argument("refs", nargs="+")
    cropped = commands.add_parser("crops", help="each beat's crop on one sheet, cards marked")
    cropped.add_argument("spec", type=Path)
    cropped.add_argument("out", type=Path)
    sources = commands.add_parser("fetch", help="save source pages as text")
    sources.add_argument("out", type=Path)
    sources.add_argument("urls", nargs="+")
    sources.add_argument("--find", default="", help="comma-separated words to show passages around")
    commands.add_parser("render", help="render a Reel on the brand's Gemini project").add_argument("spec", type=Path)
    checked = commands.add_parser("qa", help="format, loudness and a frame from each shot")
    checked.add_argument("folder", type=Path)
    checked.add_argument("out", type=Path)
    commands.add_parser("status", help="every Reel's state and the next free slots").add_argument(
        "wanted", type=int, nargs="?", default=3)
    args = parser.parse_args()

    import logging

    from .cli import PIPELINE_ROOT, _load_env

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _load_env(PIPELINE_ROOT / ".env")
    if args.command == "art":
        _print_hits((met if args.met else cleveland)(" ".join(args.query), args.limit))
    elif args.command == "commons":
        _print_hits(commons(" ".join(args.query), args.limit))
    elif args.command == "credit":
        for row in credits(args.titles):
            print(json.dumps(row, ensure_ascii=False))
    elif args.command == "sheet":
        print("\n".join(sheet(args.refs, args.out)))
    elif args.command == "crops":
        for row in crops(args.spec, args.out):
            print(json.dumps(row, ensure_ascii=False))
    elif args.command == "fetch":
        for page in fetch(args.urls, args.out, [w for w in args.find.split(",") if w]):
            print(json.dumps(page, ensure_ascii=False, indent=1))
    elif args.command == "render":
        print(render(args.spec))
    elif args.command == "qa":
        print(json.dumps(qa(args.folder, args.out), indent=1))
    else:
        print("\n".join(status(args.wanted)))


if __name__ == "__main__":
    main()
