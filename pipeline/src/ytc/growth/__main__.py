"""python -m ytc.growth run [--brand ankahi] [--dry] [--think | --no-think] [--scout]

--dry reads, analyses and decides but changes nothing in the repo or the pipeline, writes the report to the temp
folder and pushes nothing. --think runs the strategist however recently it ran; --no-think skips it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os

from .. import llm
from . import desk
from .brands import BRANDS


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ytc.growth", description="run a brand's growth desk")
    parser.add_argument("command", choices=("run",))
    parser.add_argument("--brand", default="ankahi", choices=sorted(BRANDS))
    parser.add_argument("--dry", action="store_true", help="read and decide, but change nothing")
    thinking = parser.add_mutually_exclusive_group()
    thinking.add_argument("--think", action="store_true", help="run the strategist now")
    thinking.add_argument("--no-think", action="store_true", help="skip the strategist")
    parser.add_argument("--scout", action="store_true", help="search the niche now")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if not os.environ.get("CIRCLECI"):
        from ..cli import PIPELINE_ROOT, _load_env

        _load_env(PIPELINE_ROOT / ".env")
    brand = BRANDS[args.brand]
    if brand.brand_project:
        llm.use_brand_project()
    summary = desk.run(brand, dry=args.dry, think=True if args.think else False if args.no_think else None,
                       scout_now=args.scout)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
