# ytc-pipeline

The production pipeline behind two small storytelling channels. It writes short vertical videos, finds and ranks
public-domain pictures for them, narrates, renders and checks them, then schedules them on Instagram, YouTube and
Facebook.

This repository is a copy of the code. The stories, schedules, rendered videos and keys live in private repositories
that each run reads.

## What runs here

CircleCI ([`.circleci/config.yml`](.circleci/config.yml)) runs [`pipeline/src/ytc/reel_host.py`](pipeline/src/ytc/reel_host.py)
on two hourly schedules:

- `reels` judges new videos, books the ones that pass and mirrors them to the other platforms.
- `factory` makes the next video when fewer are waiting than the calendar needs. A job stops at one hour, so a video
  that needs longer carries on in the next run.

## Layout

- `pipeline/src/ytc/`: the Python package, installed with `uv sync` on Python 3.12.
- `pipeline/assets/fonts/`: the fonts the renders use, each with its SIL Open Font License.
