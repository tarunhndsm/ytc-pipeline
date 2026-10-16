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

The same project runs the owner's news desk (pipeline/src/ytc/news, which only the clone has): `news` is one desk
cycle, with the desk's state kept between runs as a tar in the NEWS_RELEASE draft release. Its keys come from the
"news-desk" context, which only that job gets; Ankahi's keys stay out of its environment (NEWS_HIDE).

It also runs Days of Odd's watchdog (monitor/monitor.py on the clone): `watchdog`, every 3 hours, pushes the studio's
new alerts and a morning summary to the owner's phone, with only the keys of the "days-of-odd" context (WATCH_KEYS).

    python -m ytc.reel_host reels [--dry]
    python -m ytc.reel_host factory [--force] [--until ISO]
    python -m ytc.reel_host selftest                          # the keys, the clone, the releases and ffmpeg
    python -m ytc.reel_host news [--dry] [--test]             # a news desk cycle; --test renders a kept Short
    python -m ytc.reel_host watchdog                          # Days of Odd's monitor, alerting the owner's phone
    python -m ytc.reel_host upload content/instagram/ak011    # from the Mac: a render (or its youtube/ cut)
    python -m ytc.reel_host publish                           # from the Mac: the public copy, now
    python -m ytc.reel_host news-seed                         # from the Mac: keep the Mac's news desk state
    python -m ytc.reel_host news-test news/out/<short>        # from the Mac: keep a made Short for `news --test`
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
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import cloud, llm, reel_cloud, reel_factory, reel_media, reel_sources

log = logging.getLogger(__name__)

CONTENT = cloud.CHECKOUT / "content" / "instagram"
FACTORY = "content/instagram/automation/factory.json"
ALERTS = "content/instagram/automation/alerts.json"
BACKLOG = "content/instagram/backlog.md"
SOURCES = "content/instagram/sources"
LOGS = Path(tempfile.gettempdir()) / "reel-logs"
# A run that can't start tries again, this many times this many seconds apart: GitHub refuses a clone or a release
# for a moment now and then, and a run that gives up alerts the owner.
START_TRIES, START_PAUSE = 3, 60
NEEDED = ("YTC_DEPLOY_KEY", "AK_MEDIA_TOKEN", "IG_GEMINI_API_KEY", "IG_BUFFER_API_KEY", "IG_BUFFER_CHANNEL_ID",
          "IG_BUFFER_YOUTUBE_CHANNEL_ID", "IG_BUFFER_FACEBOOK_CHANNEL_ID", "IG_YOUTUBE_TOKEN", "CLOUDINARY_URL",
          "YTC_NTFY_TOPIC", "YTC_CONTACT")
PUBLIC_REPO = "tarunhndsm/ytc-pipeline"
SHARED = ("pipeline", ".circleci")
PRIVATE = "pipeline/src/ytc/news/"
NEWS_RELEASE = "news desk"
NEWS_TEST = "news test"
# The desk's state, as paths under news/. A state is never replaced in place: each run adds a new one and then drops
# all but the newest NEWS_STATES, so a run that dies mid-upload leaves the last one whole.
NEWS_KEEP = ("state/local.json", "state/voice.json", "state/cycles.jsonl", "state/run.log", "voices/designed.json",
             "voices/audition.json")
NEWS_STATES = 3
NEWS_MINUTES = 50
NEWS_HIDE = ("IG_", "AK_", "YTC_DEPLOY_KEY", "YTC_CONTACT", "CIRCLECI_TOKEN")
WATCH_KEYS = ("BUFFER_API_KEY", "BUFFER_YOUTUBE_CHANNEL_ID", "CLOUDINARY_URL", "YTC_NTFY_TOPIC")
# What the watchdog has already sent; .circleci/config.yml's watchdog job caches this folder between runs.
WATCH_OUT = Path.home() / "watchdog"
# The monitor's report names the owner's accounts, so it goes here instead of CircleCI's public log.
WATCH_RELEASE = "watchdog logs"
WATCH_MINUTES = 15


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


def _started(begin):
    """What `begin` returns, tried again START_PAUSE later when it fails, up to START_TRIES times."""
    for attempt in range(1, START_TRIES + 1):
        try:
            return begin()
        except Exception:
            if attempt == START_TRIES:
                raise
            log.warning("the run couldn't start (try %d of %d)", attempt, START_TRIES, exc_info=True)
            time.sleep(START_PAUSE)


def publish(source: Path, token: str) -> str:
    """Make the public repo the committed pipeline/ and .circleci/ of `source` (a checkout of this repo), without the
    news desk, with README.md from pipeline/ and docs/ from pipeline/pages/. Pushed with AK_MEDIA_TOKEN, sent as a
    header (as actions/checkout does) so it shows in no URL, argument or error."""
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
        # GitHub Pages serves docs/: the YouTube sign-in's home page and privacy policy, which Google requires.
        if (target / "pipeline" / "pages").is_dir():
            shutil.copytree(target / "pipeline" / "pages", target / "docs")
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
    """Drop the renders of Reels whose slot passed KEEP ago, unless their YouTube cut is still to go out; a Reel that
    may go out again one day keeps its render in reel_repost's stock."""
    from . import reel_repost

    gone = []
    for reel, assets in stored_media(store).items():
        due = reel_cloud._due(CONTENT / reel)
        if due and due < now - reel_cloud.KEEP and not _cut_waits(assets, CONTENT / reel):
            try:
                reel_repost.keep(store, CONTENT / reel)
            except Exception as err:
                log.warning("couldn't stock %s's render before dropping it; trying next run: %s", reel, err)
                continue
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

    def begin() -> tuple[reel_media.Store, list[str]]:
        prepare()
        store = reel_media.Store()
        return store, bring_media(store)

    try:
        store, brought = _started(begin)
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
    def begin() -> reel_media.Store:
        prepare()
        return reel_media.Store()

    try:
        store = _started(begin)
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
    # The source pages a run read are kept for the next, which may be refused them.
    paths = [FACTORY, SOURCES] + ([BACKLOG] if done.get("refilled") else [])
    if "sent_back" in done:
        # Its old render's manifest and review leave git now: the reels run would otherwise take that render again.
        paths.append(f"content/instagram/{reel}")
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
            message = f"Reel factory: {reel}, {done.get('story', '')}: " + (
                "rendered again on CircleCI without the pictures the judge faulted; the next reel run books it"
                if saved["reels"][reel].get("rescue") else
                "written, pictured, rendered and checked by QA on CircleCI; the next reel run judges it")
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
    "selftest" draft, dropped again), ffmpeg, and which routes to the writer's source pages answer."""
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
    found["sources"] = reel_sources.check()
    found["published"] = publish(cloud.CHECKOUT, os.environ["AK_MEDIA_TOKEN"])
    return found


# --- the news desk ---------------------------------------------------------------------------------------------

def _news_alert(message: str) -> None:
    """Push to the owner's phone; only the hour's first run does, so a desk that keeps failing every quarter of an
    hour sends one alert an hour."""
    if (topic := os.environ.get("YTC_NTFY_TOPIC")) and datetime.now(timezone.utc).minute < 15:
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=message[:900].encode("utf-8"), timeout=30,
                          headers={"Title": "The news desk needs attention", "Priority": "high", "Tags": "warning"})
        except requests.RequestException as err:
            log.warning("ntfy push failed: %s", err)


def _news_states(store: reel_media.Store) -> list[str]:
    """The kept states' names, newest first."""
    return sorted((name for name in store.assets(NEWS_RELEASE) if name.startswith("state-")), reverse=True)


def bring_news(store: reel_media.Store, folder: Path) -> str:
    """Unpack the newest kept state into news/. A missing state is a fault, not a first run: a desk that forgot what
    it had posted would post it again."""
    if not (names := _news_states(store)):
        raise RuntimeError(f'no news desk state in the "{NEWS_RELEASE}" draft release of {reel_media.REPO}')
    with tempfile.TemporaryDirectory() as tmp:
        packed = Path(tmp) / names[0]
        store.get(NEWS_RELEASE, names[0], packed)
        with tarfile.open(packed) as tar:
            tar.extractall(folder, members=[m for m in tar.getmembers() if m.name in NEWS_KEEP], filter="data")
    return names[0]


def keep_news(store: reel_media.Store, folder: Path) -> str:
    cycles = folder / "state" / "cycles.jsonl"
    if cycles.exists():
        lines = cycles.read_text(encoding="utf-8").splitlines(keepends=True)
        cycles.write_text("".join(lines[-3000:]), encoding="utf-8")
    name = f"state-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        packed = Path(tmp) / name
        with tarfile.open(packed, "w:gz") as tar:
            for path in NEWS_KEEP:
                if (folder / path).is_file():
                    tar.add(folder / path, arcname=path)
        store.put(NEWS_RELEASE, name, packed)
    for old in _news_states(store)[NEWS_STATES:]:
        store.remove(NEWS_RELEASE, old)
    return name


def _news_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(NEWS_HIDE)}
    return env | {"PYTHONPATH": str(cloud.CHECKOUT / "pipeline" / "src"), "PYTHONUNBUFFERED": "1"}


def _last_cycle(cycles: Path, since: datetime) -> dict:
    """What the run's cycle did, without the editor's reasoning (this goes to CircleCI's public log)."""
    try:
        line = json.loads(cycles.read_text(encoding="utf-8").splitlines()[-1])
    except (OSError, ValueError, IndexError):
        return {}
    if datetime.fromisoformat(line["at"]) < since:
        return {}
    out = {k: line[k] for k in ("at", "items", "held", "skipped") if k in line}
    if choice := line.get("choice"):
        out["choice"] = {k: choice.get(k) for k in ("pick", "virality", "risk")}
    if result := line.get("result"):
        out["result"] = {k: result[k] for k in ("skipped", "failed", "timings") if k in result}
        out["result"]["made"] = bool(result.get("video"))
    return out


def news(dry: bool = False) -> dict:
    """One news desk cycle (ytc.news.run --once) on the clone, from the newest kept state. Unless dry, it posts what it
    makes and keeps the state it leaves; a dry run posts nothing and keeps nothing."""
    started = datetime.now(timezone.utc)
    folder = cloud.CHECKOUT / "news"
    try:
        prepare()
        store = reel_media.Store()
        brought = bring_news(store, folder)
    except Exception as err:
        _news_alert(f"The news desk's run on CircleCI couldn't start: {err}")
        raise
    command = [sys.executable, "-m", "ytc.news.run", "--once", *([] if dry else ["--publish"])]
    code, _ = _step(command, _news_env(), LOGS / "news.log", NEWS_MINUTES * 60)
    done = {"brought": brought, "exit": code, "cycle": _last_cycle(folder / "state" / "cycles.jsonl", started)}
    _keep_log(store, "news logs", LOGS / "news.log")
    if dry:
        _keep_log(store, "news logs", folder / "state" / "run.log")
    else:
        try:
            done["kept"] = keep_news(store, folder)
        except Exception as err:
            _news_alert(f"The news desk's run on CircleCI couldn't keep its state, so the next run may repeat it: {err}")
            raise
    if code:
        _news_alert(f"The news desk's run on CircleCI stopped with exit code {code}; its log is in the "
                    f'"news logs" draft release of {reel_media.REPO}.')
    return done


def news_test() -> dict:
    """Render the Short kept in NEWS_TEST here (ytc.news.selftest), with no Gemini call and nothing posted."""
    prepare()
    store = reel_media.Store()
    tars = [name for name in store.assets(NEWS_TEST) if name.endswith(".tar")]
    if not tars:
        raise RuntimeError(f'nothing in the "{NEWS_TEST}" draft release of {reel_media.REPO}')
    short = tars[0].removesuffix(".tar")
    parent = Path(tempfile.mkdtemp())
    store.bring_folder(NEWS_TEST, short, parent)
    command = [sys.executable, "-m", "ytc.news.selftest", str(parent / short)]
    code, done = _step(command, _news_env(), LOGS / "news-test.log", NEWS_MINUTES * 60)
    _keep_log(store, "news logs", LOGS / "news-test.log")
    return done | {"short": short, "exit": code}


def news_test_upload(folder: Path, store: reel_media.Store) -> str:
    """From the Mac: keep a made Short's narration, package, project and render for `news --test`."""
    with tempfile.TemporaryDirectory() as tmp:
        packed = Path(tmp) / f"{folder.name}.tar"
        with tarfile.open(packed, "w") as tar:
            for part in ("package.json", "voice.wav", "short.mp4", "project"):
                tar.add(folder / part, arcname=f"{folder.name}/{part}")
        for old in store.assets(NEWS_TEST):
            store.remove(NEWS_TEST, old)
        store.put(NEWS_TEST, packed.name, packed)
    return packed.name


# --- Days of Odd's watchdog --------------------------------------------------------------------------------------

def _watch_alert(message: str) -> None:
    """Push to the owner's phone at most once a day: a watchdog that can't run would otherwise say so every 3 hours."""
    path, today = WATCH_OUT / "alerted.json", datetime.now(timezone.utc).date().isoformat()
    if not (topic := os.environ.get("YTC_NTFY_TOPIC")) or _read(path).get("date") == today:
        return
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=message[:900].encode("utf-8"), timeout=30,
                      headers={"Title": "Days of Odd's watchdog needs attention", "Priority": "high", "Tags": "warning"})
    except requests.RequestException as err:
        log.warning("ntfy push failed: %s", err)
        return
    _write(path, {"date": today})


def watchdog() -> dict:
    """monitor/monitor.py on the clone: it sees only WATCH_KEYS, keeps what it sent in WATCH_OUT, and its report goes
    to the WATCH_RELEASE draft release. The public log gets the counts."""
    try:
        _started(prepare)
    except Exception as err:
        _watch_alert(f"Days of Odd's watchdog on CircleCI couldn't clone the repo, so nothing watches the studio: {err}")
        raise
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "CIRCLECI", *WATCH_KEYS) if os.environ.get(k)}
    report = LOGS / "watchdog.log"
    report.parent.mkdir(parents=True, exist_ok=True)
    WATCH_OUT.mkdir(parents=True, exist_ok=True)
    with report.open("w") as out:
        try:
            code = subprocess.run([sys.executable, str(cloud.CHECKOUT / "monitor" / "monitor.py")], stdout=out,
                                  stderr=subprocess.STDOUT, timeout=WATCH_MINUTES * 60,
                                  env=env | {"MONITOR_OUT": str(WATCH_OUT), "PYTHONUTF8": "1"}).returncode
        except subprocess.TimeoutExpired:
            code = 124
    _keep_log(reel_media.Store(), WATCH_RELEASE, report)
    last = _read(WATCH_OUT / "last.json")
    levels = [level for level, _ in last.get("problems", [])]
    done = {"exit": code, "checked_at": last.get("checked_at"), "alerts": levels.count("alert"),
            "warnings": levels.count("warn"), "summary_sent": _read(WATCH_OUT / "digest.json").get("date")}
    if code:
        _watch_alert(f"Days of Odd's watchdog on CircleCI stopped with exit code {code}; its report is in the "
                     f'"{WATCH_RELEASE}" draft release of {reel_media.REPO}.')
    return done


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ytc.reel_host", description="@ankahi.kathayein's Reels on CircleCI")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("reels", help="judge, book and mirror (reel_cloud's reel_run)").add_argument(
        "--dry", action="store_true", help="change nothing")
    making = commands.add_parser("factory", help="make a Reel when the calendar runs short")
    making.add_argument("--force", action="store_true", help="make a Reel even when the supply is enough")
    making.add_argument("--until", type=datetime.fromisoformat, help="start no step that can't finish by then")
    commands.add_parser("selftest", help="check the keys, the clone, the releases and ffmpeg")
    desk = commands.add_parser("news", help="one cycle of the news desk")
    desk.add_argument("--dry", action="store_true", help="post nothing and keep no state")
    desk.add_argument("--test", action="store_true", help="render the kept test Short instead, with no Gemini call")
    commands.add_parser("watchdog", help="Days of Odd's monitor, alerting the owner's phone")
    commands.add_parser("upload", help="from the Mac: put rendered Reels' media in the releases").add_argument(
        "folders", type=Path, nargs="+")
    commands.add_parser("publish", help="from the Mac: bring the public copy up to this checkout's last commit")
    commands.add_parser("news-seed", help="from the Mac: keep the Mac's news desk state as the newest one")
    commands.add_parser("news-test", help="from the Mac: keep a made Short for `news --test`").add_argument(
        "folder", type=Path)
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
    if args.command == "news-seed":
        print(keep_news(reel_media.Store(), PIPELINE_ROOT.parent / "news"))
        return
    if args.command == "news-test":
        print(news_test_upload(args.folder.resolve(), reel_media.Store()))
        return
    if not os.environ.get("CIRCLECI"):
        raise SystemExit(f"`{args.command}` runs on CircleCI only: it puts the deploy key in ~/.ssh")
    if args.command == "reels":
        result = reels(args.dry)
    elif args.command == "factory":
        result = factory(args.force, args.until)
    elif args.command == "news":
        result = news_test() if args.test else news(args.dry)
    elif args.command == "watchdog":
        result = watchdog()
    else:
        result = selftest()
    print(json.dumps(result, indent=1, ensure_ascii=False))
    if result.get("exit") or result.get("missing") or result.get("round_trip") is False:
        sys.exit(1)


if __name__ == "__main__":
    main()
