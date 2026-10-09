"""@ankahi.kathayein's Reels on CircleCI's free plan, so they keep going out while the Mac is closed, with no card on
file (reel_cloud ran them on Modal until its free credit ran out).

CircleCI runs this from PUBLIC_REPO on the owner's personal GitHub account, a copy of this repo's committed pipeline/
and .circleci/ without the news desk, which each `reels` run brings up to date (publish; GitHub Actions can't, with
this repo's account out of private-repo minutes). Each run clones this private repo with a deploy key and works on the
clone with the clone's code, as reel_cloud's runs do:

- `reels` (every hour at 15 past) is reel_cloud.reel_run: judge, book, mirror, keep the YouTube channel, push.
- `factory` (every hour at 40 past) is reel_cloud.factory_run: make a Reel when the calendar runs short. CircleCI stops
  a job at one hour, so with --until a step that can't finish in time waits for the next run; a Reel takes about two.

What git leaves out waits in a private repo's draft releases (reel_media): each Reel's render, a half-made Reel
between factory runs, and the runs' logs, which stay off CircleCI's public pages. The factory's state and when each alert last
went out are files in git (FACTORY, ALERTS).

    python -m ytc.reel_host reels [--dry]
    python -m ytc.reel_host factory [--force] [--until ISO]
    python -m ytc.reel_host selftest                          # the keys, the clone, the releases and ffmpeg
    python -m ytc.reel_host upload content/instagram/ak011    # from the Mac: a render (or its youtube/ cut)
    python -m ytc.reel_host publish                           # from the Mac: the public copy, now
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import cloud, llm, reel_cloud, reel_factory, reel_media

log = logging.getLogger(__name__)

CONTENT = cloud.CHECKOUT / "content" / "instagram"
FACTORY = "content/instagram/automation/factory.json"
ALERTS = "content/instagram/automation/alerts.json"
BACKLOG = "content/instagram/backlog.md"
LOGS = Path(tempfile.gettempdir()) / "reel-logs"
NEEDED = ("YTC_DEPLOY_KEY", "AK_MEDIA_TOKEN", "IG_GEMINI_API_KEY", "IG_BUFFER_API_KEY", "IG_BUFFER_CHANNEL_ID",
          "IG_BUFFER_YOUTUBE_CHANNEL_ID", "IG_BUFFER_FACEBOOK_CHANNEL_ID", "IG_YOUTUBE_TOKEN", "CLOUDINARY_URL",
          "YTC_NTFY_TOPIC", "YTC_CONTACT")
PUBLIC_REPO = "tarunhndsm/ytc-pipeline"
SHARED = ("pipeline", ".circleci")
PRIVATE = "pipeline/src/ytc/news/"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def prepare() -> None:
    """A fresh clone of the private repo (reel_cloud._clone)."""
    # CircleCI's checkout leaves its own key for the public repo, which GitHub would take first and then refuse.
    os.environ["GIT_SSH_COMMAND"] = "ssh -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes"
    reel_cloud._clone()


def publish(source: Path, token: str) -> str:
    """Make the public repo the committed pipeline/ and .circleci/ of `source` (a checkout of this repo), without the
    news desk, with README.md from pipeline/. Pushed with AK_MEDIA_TOKEN, sent as a header (as actions/checkout does)
    so it shows in no URL, argument or error."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    # Without the global config: CircleCI's checkout rewrites GitHub's https URLs to SSH, with a key for another repo.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
           "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}
    url = f"https://github.com/{PUBLIC_REPO}.git"
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "public"

        def git(*args: str, cwd: Path = target) -> subprocess.CompletedProcess:
            return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)

        if (cloned := git("clone", "-q", "--depth", "1", url, str(target), cwd=Path(tmp))).returncode:
            raise RuntimeError(f"couldn't clone the public repo: {cloned.stderr.strip()[:300]}")
        for item in target.iterdir():
            if item.name != ".git":
                shutil.rmtree(item) if item.is_dir() else item.unlink()
        archive = subprocess.run(["git", "-C", str(source), "archive", "--format=tar", "HEAD", "--", *SHARED],
                                 capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(target, members=[m for m in tar.getmembers() if not m.name.startswith(PRIVATE)],
                           filter="data")
        shutil.copy2(target / "pipeline" / "README.md", target / "README.md")
        git("add", "-A")
        if git("diff", "--cached", "--quiet").returncode == 0:
            return "up to date"
        sha = git("rev-parse", "--short", "HEAD", cwd=source).stdout.strip()
        git("-c", "user.name=days-of-odd", "-c", "user.email=sync@days-of-odd.invalid", "commit", "-q", "-m",
            f"From days-of-odd {sha}")
        if (pushed := git("push", "-q", "origin", "HEAD:main")).returncode:
            raise RuntimeError(f"couldn't push to the public repo: {pushed.stderr.strip()[:300]}")
    return f"published {sha}"


# --- renders in the releases ------------------------------------------------------------------------------------

def stored_media(store: reel_media.Store) -> dict[str, set[str]]:
    """Each Reel with a "media" release, and the names of its assets."""
    return {name.split(" ", 1)[1]: set(store.assets(name)) for name in sorted(store.releases())
            if name.startswith("media ")}


def upload(folder: Path, store: reel_media.Store) -> list[str]:
    """Put a rendered Reel's (or its YouTube cut's) media and manifest in its "media" release."""
    from . import instagram

    folder = folder.resolve()
    cut = folder.name == instagram.CUT
    reel = folder.parent.name if cut else folder.name
    names = [name.format(id=reel) for name in (*reel_cloud.MEDIA, reel_cloud.RENDER)]
    if missing := [name for name in names if not (folder / name).exists()]:
        raise RuntimeError(f"{folder} has no {', '.join(missing)}: render it first")
    for name in names:
        store.put(f"media {reel}", reel_media.asset_name(name, f"{instagram.CUT}." if cut else ""), folder / name)
    return names


def _cut_waits(assets: set[str], folder: Path) -> bool:
    """Whether the releases have a Reel's YouTube cut that its Short isn't yet (reel_cloud._cut_waits)."""
    from . import instagram

    record = instagram.youtube_record(folder) or {}
    return f"{instagram.CUT}.{folder.name}.mp4" in assets and not (record.get("cut") and not record.get("withdrawn"))


def _bring_render(store: reel_media.Store, reel: str, folder: Path, prefix: str) -> bool:
    """Download a render when git has its folder as rendered, with the same manifest (reel_cloud._copy_render)."""
    release, label, manifest = f"media {reel}", f"{reel} {prefix}".strip(" ."), folder / reel_cloud.RENDER
    if not manifest.exists() or store.read(release, reel_media.asset_name(reel_cloud.RENDER, prefix)) != manifest.read_bytes():
        log.info("%s: the folder in git isn't the render in the releases yet", label)
        return False
    names = [name.format(id=reel) for name in reel_cloud.MEDIA]
    have = store.assets(release)
    if missing := [name for name in names if reel_media.asset_name(name, prefix) not in have]:
        log.info("%s: the releases have no %s", label, ", ".join(missing))
        return False
    for name in names:
        store.get(release, reel_media.asset_name(name, prefix), folder / name)
    return True


def bring_media(store: reel_media.Store) -> list[str]:
    """Download the render of each Reel still to be judged or booked, and each YouTube cut still to go out."""
    from . import instagram

    brought = []
    for reel, assets in stored_media(store).items():
        folder = CONTENT / reel
        if not folder.is_dir():
            continue
        if not (folder / instagram.RECORD).exists() and not reel_factory._judged_and_failed(folder) \
                and _bring_render(store, reel, folder, ""):
            brought.append(reel)
        if _cut_waits(assets, folder) and _bring_render(store, reel, folder / instagram.CUT, f"{instagram.CUT}."):
            brought.append(f"{reel} ({instagram.CUT})")
    return brought


def prune(store: reel_media.Store, now: datetime) -> list[str]:
    """Drop the renders of Reels whose slot passed KEEP ago, unless their YouTube cut is still to go out."""
    gone = []
    for reel, assets in stored_media(store).items():
        due = reel_cloud._due(CONTENT / reel)
        if due and due < now - reel_cloud.KEEP and not _cut_waits(assets, CONTENT / reel):
            store.drop(f"media {reel}")
            gone.append(reel)
    return gone


def _keep_log(store: reel_media.Store, release: str, path: Path) -> None:
    if not path.is_file() or not path.stat().st_size:
        return  # GitHub refuses an empty asset (400)
    try:
        store.put(release, path.name, path)
    except Exception as err:
        log.warning("couldn't keep %s: %s", path.name, err)


def _step(command: list[str], env: dict, log_file: Path, timeout: int) -> tuple[int, dict]:
    """Run a run's step on the clone, its log in a file; the exit code (124 when stopped) and its last JSON line."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("w") as err:
        try:
            run = subprocess.run(command, cwd=cloud.CHECKOUT / "pipeline", env=env, stdout=subprocess.PIPE, stderr=err,
                                 text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, {}
    print(run.stdout.strip()[:4000], flush=True)
    return run.returncode, reel_cloud._last_json(run.stdout)


def _env(**extra: str) -> dict:
    return llm.use_brand_project({**os.environ, "PYTHONPATH": str(cloud.CHECKOUT / "pipeline" / "src"),
                                  "PYTHONUNBUFFERED": "1", **extra})


# --- the runs ----------------------------------------------------------------------------------------------------

def reels(dry: bool = False) -> dict:
    """reel_cloud.reel_run on CircleCI."""
    started = datetime.now(timezone.utc)
    try:
        prepare()
        store = reel_media.Store()
        brought = bring_media(store)
    except Exception as err:
        reel_cloud._alert(f"The Reels' run on CircleCI couldn't start: {err}"[:900])
        raise
    command = [sys.executable, "-c", "from ytc import reel_cloud; reel_cloud.main()", "work", *(["--dry"] if dry else [])]
    code, done = _step(command, _env(), LOGS / "reels.log", reel_cloud.RUN_MINUTES * 60)
    _keep_log(store, "reels logs", LOGS / "reels.log")
    done |= {"brought": brought, "exit": code}
    if dry:
        return done
    try:
        done["published"] = publish(cloud.CHECKOUT, os.environ["AK_MEDIA_TOKEN"])
    except Exception as err:
        log.exception("updating the public copy failed")
        done.setdefault("errors", []).append(f"couldn't update the public copy CircleCI runs from: {err}"[:300])
    memory = _read(cloud.CHECKOUT / ALERTS)
    reel_cloud._tell_owner(code, done, memory, "CircleCI",
                           f'app.circleci.com > ytc-pipeline, and the "reels logs" draft release in {reel_media.REPO}')
    _write(cloud.CHECKOUT / ALERTS, memory)
    try:
        done["saved"] = cloud._save(reel_cloud._message(started, done, "CircleCI"))
    except Exception as err:
        reel_cloud._alert(f"The Reels' run on CircleCI couldn't save what it did: {err}"[:900])
        raise
    done["pruned"] = prune(store, started)
    return done


def _keep_wip(store: reel_media.Store, reel: str, done: dict) -> None:
    if (CONTENT / reel).is_dir():
        try:
            store.keep_folder(f"wip {reel}", CONTENT / reel)
        except Exception as err:
            done["errors"].append(f"couldn't keep {reel}'s half-made folder: {err}"[:300])


def factory(force: bool = False, until: datetime | None = None) -> dict:
    """reel_cloud.factory_run on CircleCI: one Reel's next steps, then it ships or waits in the releases."""
    try:
        prepare()
        store = reel_media.Store()
    except Exception as err:
        reel_cloud._alert(f"The Reel factory on CircleCI couldn't start: {err}"[:900])
        raise
    saved = _read(cloud.CHECKOUT / FACTORY)
    for key in ("reels", "made", "alerts"):
        saved.setdefault(key, {})
    resumed = [reel for reel, mine in saved["reels"].items() if not mine.get("pushed") and not mine.get("set_aside")
               and store.bring_folder(f"wip {reel}", reel, CONTENT)]
    state_file = Path(tempfile.gettempdir()) / "factory-state.json"
    stored_file = Path(tempfile.gettempdir()) / "factory-stored.json"
    _write(state_file, saved)
    stored_file.write_text(json.dumps([reel for reel, assets in stored_media(store).items() if f"{reel}.mp4" in assets]),
                           encoding="utf-8")
    command = [sys.executable, "-c", "from ytc import reel_factory; reel_factory.main()", "work", "--state",
               str(state_file), "--logs", str(LOGS), "--stored", str(stored_file), *(["--force"] if force else []),
               *(["--until", until.isoformat()] if until else [])]
    # A step stopped at `until` takes 10 s to die, and the step's own wrap-up follows.
    limit = int((until - datetime.now(timezone.utc)).total_seconds()) + 360 if until else reel_cloud.FACTORY_MINUTES * 60
    code, done = _step(command, _env(YTC_RANK_HERE="1"), LOGS / "factory.log", max(limit, 120))
    saved = _read(state_file) or saved
    done |= {"resumed": resumed, "exit": code, "errors": []}
    if code:
        done["errors"].append("the factory's step ran out of time" if code == 124
                              else f"the factory's step failed (exit {code})")
    reel, result = done.get("reel"), done.get("result")
    paths = [FACTORY]
    message = f"Reel factory on CircleCI: {reel} {result}" + (f" ({done['why']})" if done.get("why") else "") \
        if reel else "Reel factory on CircleCI: state"
    if reel and result == "made":
        try:
            upload(CONTENT / reel, store)
            # Kept until the push lands: a run that can't push leaves the next run a finished Reel to ship.
            store.keep_folder(f"wip {reel}", CONTENT / reel)
            saved["reels"][reel]["pushed"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            today = f"{datetime.now(reel_cloud.IST):%Y-%m-%d}"
            saved["made"][today] = saved["made"].get(today, 0) + 1
            paths += [f"content/instagram/{reel}", BACKLOG]
            message = (f"Reel factory: {reel}, {done.get('story', '')}: written, pictured, rendered and checked by QA "
                       "on CircleCI; the next reel run judges it")
        except Exception as err:
            log.exception("shipping %s failed", reel)
            done["errors"].append(f"{reel} was made but not shipped: {err}"[:300])
            _keep_wip(store, reel, done)
    elif reel and result == "set aside":
        # The story's row moved on this clone: pushed, or every run would start the same story again.
        paths.append(BACKLOG)
        store.drop(f"wip {reel}")
    elif reel and done.get("steps"):
        _keep_wip(store, reel, done)
    if reel or code:
        for path in sorted(LOGS.glob("*.log")):
            _keep_log(store, reel_media.LOGS, path)
    reel_cloud._factory_alerts(saved, done)
    _write(cloud.CHECKOUT / FACTORY, saved)
    try:
        done["pushed"] = reel_cloud._push(paths, message[:300])
    except Exception as err:
        reel_cloud._alert(f"The Reel factory on CircleCI couldn't push: {err}"[:900])
        raise
    if reel and result == "made" and saved["reels"][reel].get("pushed"):
        store.drop(f"wip {reel}")
    return done


def selftest() -> dict:
    """What the runs need: the keys (by name), the clone and its right to push, a round trip through the releases (a
    "selftest" draft, dropped again) and ffmpeg."""
    import imageio_ffmpeg

    found: dict = {"missing": [key for key in NEEDED if not os.environ.get(key)]}
    prepare()
    found["clone"] = cloud._git("log", "-1", "--format=%h %cs").stdout.strip()
    store = reel_media.Store()
    found["releases"] = sorted(store.releases())
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.txt"
        probe.write_text(f"selftest {datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")
        store.put("selftest", probe.name, probe)
        found["round_trip"] = store.read("selftest", probe.name) == probe.read_bytes()
        store.drop("selftest")
    found["ffmpeg"] = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-version"], capture_output=True,
                                     text=True).stdout.split("\n", 1)[0]
    found["published"] = publish(cloud.CHECKOUT, os.environ["AK_MEDIA_TOKEN"])
    return found


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ytc.reel_host", description="@ankahi.kathayein's Reels on CircleCI")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("reels", help="judge, book and mirror (reel_cloud's reel_run)").add_argument(
        "--dry", action="store_true", help="change nothing")
    making = commands.add_parser("factory", help="make a Reel when the calendar runs short")
    making.add_argument("--force", action="store_true", help="make a Reel even when the supply is enough")
    making.add_argument("--until", type=datetime.fromisoformat, help="start no step that can't finish by then")
    commands.add_parser("selftest", help="check the keys, the clone, the releases and ffmpeg")
    commands.add_parser("upload", help="from the Mac: put rendered Reels' media in the releases").add_argument(
        "folders", type=Path, nargs="+")
    commands.add_parser("publish", help="from the Mac: bring the public copy up to this checkout's last commit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    from .cli import PIPELINE_ROOT, _load_env

    _load_env(PIPELINE_ROOT / ".env")
    if args.command == "upload":
        store = reel_media.Store()
        for folder in args.folders:
            print(f"{folder}: {', '.join(upload(folder, store))}")
        return
    if args.command == "publish":
        print(publish(PIPELINE_ROOT.parent, os.environ["AK_MEDIA_TOKEN"]))
        return
    if not os.environ.get("CIRCLECI"):
        raise SystemExit(f"`{args.command}` runs on CircleCI only: it puts the deploy key in ~/.ssh")
    if args.command == "reels":
        result = reels(args.dry)
    elif args.command == "factory":
        result = factory(args.force, args.until)
    else:
        result = selftest()
    print(json.dumps(result, indent=1, ensure_ascii=False))
    if result.get("exit") or result.get("missing") or result.get("round_trip") is False:
        sys.exit(1)


if __name__ == "__main__":
    main()
