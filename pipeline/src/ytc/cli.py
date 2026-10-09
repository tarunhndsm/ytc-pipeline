"""Command-line entry point for the Shorts pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[2]
CONTENT_ROOT = PIPELINE_ROOT.parent / "content"


def _load_env(path: Path) -> None:
    """Read KEY=VALUE lines into the environment without overriding variables already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> None:
    parser = argparse.ArgumentParser(prog="ytc", description="YouTube Shorts production pipeline")
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("make", help="render a Short from a spec file")
    make.add_argument("spec", type=Path)
    make.add_argument("--out", type=Path)
    make.add_argument(
        "--where",
        choices=("auto", "cloud", "local"),
        default="auto",
        help="cloud renders on Modal; auto uses Modal when MODAL_TOKEN_ID is set and falls back to this Mac",
    )
    check = commands.add_parser(
        "check", help="report loudness, assets, review frames, and misheard words for a rendered Short"
    )
    check.add_argument("video", type=Path)
    phonemes = commands.add_parser("phonemes", help="print how the narrator will pronounce some text")
    phonemes.add_argument("text")
    describe = commands.add_parser("describe", help="print the YouTube description a rendered Short will get")
    describe.add_argument("spec", type=Path)
    publish = commands.add_parser("publish", help="schedule a rendered Short on YouTube through Buffer")
    publish.add_argument("spec", type=Path)
    publish.add_argument("--at", type=datetime.fromisoformat, help="ISO time with offset; default is the next free slot")
    commands.add_parser("sync", help="refresh publish records from Buffer and free hosted media of live posts")
    commands.add_parser("channels", help="list the channels connected to Buffer")
    auth = commands.add_parser("auth", help="one-time sign-in for YouTube analytics and playlists")
    auth.add_argument("--brand", action="store_true", help="the अनकही कथाएँ channel instead")
    commands.add_parser("stats", help="save channel and per-Short analytics to analytics/<date>.json")
    commands.add_parser("playlists", help="add every live Short to its series playlist")
    auto = commands.add_parser("auto", help="one unattended studio run: sync, daily routine, produce, publish")
    auto.add_argument("--produce", type=int, help="make this many Shorts (default: whatever keeps a week waiting)")
    auto.add_argument("--no-publish", action="store_true", help="don't schedule anything in Buffer")
    auto.add_argument("--daily", choices=("auto", "yes", "no"), default="auto", help="the once-a-day routine")
    auto.add_argument("--trigger", default="manual")
    produce = commands.add_parser("produce", help="research, write, render, review, and host one Short")
    produce.add_argument("--topic", required=True)
    produce.add_argument("--series", required=True)
    produce.add_argument("--id", help="episode id (default: the next free epNNN)")
    produce.add_argument("--at", help="anniversary date (YYYY-MM-DD) to publish on")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "urllib3", "trafilatura", "primp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _load_env(PIPELINE_ROOT / ".env")
    try:
        _run(args)
    except RuntimeError as err:
        raise SystemExit(f"error: {err}") from err


def _make(spec: Path, out: Path | None, where: str) -> Path:
    if where == "cloud" or (where == "auto" and os.environ.get("MODAL_TOKEN_ID")):
        try:
            from .cloud import render

            return render(spec, out)
        except Exception as err:  # any cloud failure: quota, network, image build
            if where == "cloud":
                raise
            logging.getLogger("ytc").warning("cloud render failed (%s); rendering on this machine instead", err)
    from .render import make_short

    return make_short(spec, out)


def _run(args: argparse.Namespace) -> None:
    if args.command == "make":
        print(_make(args.spec, args.out, args.where))
    elif args.command == "check":
        from .check import check as check_video

        print(json.dumps(check_video(args.video), indent=2))
    elif args.command == "phonemes":
        from .tts import phonemes

        print(phonemes(args.text))
    elif args.command == "describe":
        from .publish import description
        from .spec import ShortSpec

        manifest = json.loads((args.spec.parent / "work" / "manifest.json").read_text(encoding="utf-8"))
        print(description(ShortSpec.load(args.spec), manifest))
    elif args.command == "publish":
        from .publish import schedule

        print(json.dumps(schedule(args.spec, args.at), indent=2))
    elif args.command == "sync":
        from .publish import sync

        print(json.dumps(sync(CONTENT_ROOT / "episodes"), indent=2))
    elif args.command == "channels":
        from .publish import channels

        print(json.dumps(channels(), indent=2))
    elif args.command == "auth":
        from .youtube import authorize

        print(f"Saved sign-in to {authorize(brand=args.brand)}")
    elif args.command == "stats":
        from .youtube import save_stats

        print(save_stats(PIPELINE_ROOT.parent / "analytics"))
    elif args.command == "playlists":
        from .youtube import file_into_playlists

        print("\n".join(file_into_playlists(CONTENT_ROOT / "episodes")) or "every live Short is already in its playlist")
    elif args.command == "auto":
        from .auto import main as run_studio

        raise SystemExit(run_studio(args.produce, not args.no_publish, args.daily, args.trigger))
    elif args.command == "produce":
        from .studio import produce

        print(json.dumps(produce(args.topic, args.series, args.id, at=args.at), indent=2))
