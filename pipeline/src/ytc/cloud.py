"""Run the studio on Modal's monthly credit instead of on a laptop or an Actions runner.

`studio_run` is the studio itself: four times a day it runs `ytc auto` on a fresh clone of the repo and pushes
what changed. `studio_episode` makes a whole Short (research to hosting) from a snapshot of the repo, so a run can
start several and exit instead of waiting; the next run collects what they left in the results volume.
`render_episode` renders one spec. `slot_watch` sends a post Buffer just failed again, minutes after its slot.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import modal
from modal.exception import NotFoundError

from .publish import AUDIENCE_TZ, SLOTS
from .spec import ShortSpec

PIPELINE_ROOT = Path(__file__).resolve().parents[2]
REMOTE_PIPELINE = Path("/root/pipeline")
REMOTE_EPISODE = Path("/root/episode")
# studio.py puts the repo root three folders above its own file: /root, with /root/pipeline/src/ytc/studio.py.
REMOTE_ROOT = REMOTE_PIPELINE.parent
APP_NAME = "days-of-odd"
# Episode folders the writer and reviewer learn from; older ones stay out of the snapshot to keep it small.
CONTEXT_EPISODES = 40
# A Gemini voice's kept take (tts._gemini): sent with every render and brought back, so a re-render for new
# pictures reuses the performance instead of spending the voice's daily quota on a new one.
TAKE = ("work/take.wav", "work/take.json")
# What comes back besides the Short itself: the review and credit files `ytc check` and `ytc publish` read.
RESULTS = (
    "work/assets",
    "work/captions.ass",
    "work/captions.srt",
    "work/manifest.json",
    "work/narration.wav",
    "work/speech.json",
    *TAKE,
)

# render.py finds its fonts relative to its own file, so the source keeps the pipeline/ layout. Modal's
# automatic source mount would put ytc at /root/ytc instead, shadowing that copy.
app = modal.App(APP_NAME, include_source=False)
cache = modal.Volume.from_name("days-of-odd-cache", create_if_missing=True)
outbox = modal.Volume.from_name("days-of-odd-outbox", create_if_missing=True)
# What a studio worker needs besides the image. Read from the deploying machine's environment (the Actions
# secrets, or pipeline/.env on the Mac), so rotating a key in GitHub reaches the workers on the next deploy.
STUDIO_ENV = ("YTC_CURSOR_API_KEY", "YTC_CURSOR_MODEL", "YTC_LLM_FIRST", "YTC_GEMINI_API_KEY", "YTC_MISTRAL_API_KEY",
              "YTC_OPENROUTER_API_KEY", "CLOUDINARY_URL", "YTC_CONTACT")
studio_secret = modal.Secret.from_dict({k: v for k in STUDIO_ENV if (v := os.environ.get(k))})
# Image sources throttle downloads that carry no contact details (visuals._user_agent); a Gemini voice reads the
# script on the render.
render_secret = modal.Secret.from_dict({k: v for k in ("YTC_CONTACT", "YTC_GEMINI_API_KEY") if (v := os.environ.get(k))})
# Everything a studio run uses, the deploy key it pushes with included; made from the Mac (OWNER-CHECKLIST.md).
run_secret = modal.Secret.from_name("days-of-odd-studio")


def _image(*apt: str) -> modal.Image:
    base = modal.Image.debian_slim(python_version="3.12")
    return (
        (base.apt_install(*apt) if apt else base)
        .uv_sync(str(PIPELINE_ROOT))
        .env({"HF_HOME": "/cache/huggingface", "PYTHONPATH": str(REMOTE_PIPELINE / "src"), "YTC_ON_MODAL": "1"})
        .add_local_dir(PIPELINE_ROOT / "assets", str(REMOTE_PIPELINE / "assets"))
        .add_local_dir(PIPELINE_ROOT / "src", str(REMOTE_PIPELINE / "src"), ignore=["**/__pycache__"])
    )


image = _image()
run_image = _image("git", "openssh-client")


def _pack(root: Path, names: list[str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in names:
            if (root / name).exists():
                tar.add(root / name, arcname=name)
    return buffer.getvalue()


def _unpack(data: bytes, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(root, filter="data")


@app.function(image=image, cpu=8.0, memory=8192, timeout=1800, volumes={"/cache": cache}, secrets=[render_secret])
def render_episode(bundle: bytes, spec_name: str) -> tuple[bytes, bytes]:
    from .check import write_speech
    from .render import make_short

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _unpack(bundle, REMOTE_EPISODE)
    video = make_short(REMOTE_EPISODE / spec_name)
    write_speech(REMOTE_EPISODE / "work")
    cache.commit()
    return video.read_bytes(), _pack(REMOTE_EPISODE, list(RESULTS))


@app.function(image=image, cpu=4.0, memory=4096, timeout=900, volumes={"/cache": cache})
def rank_pictures(texts: list[str], images: list[bytes]) -> dict:
    """rank.score on Modal, for studio workers too small to run the vision model; its weights stay in the cache."""
    from .rank import score

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = score(texts, images)
    cache.commit()
    return result


def _is_input(relative: Path, final: str) -> bool:
    if relative.parts[0] == "work":
        return (len(relative.parts) > 2 and relative.parts[1] == "assets") or relative.as_posix() in TAKE
    return relative.as_posix() != final


def render(spec_path: Path, out: Path | None = None) -> Path:
    """Render the Short on Modal, then unpack it and its review files next to the spec."""
    spec_path = spec_path.resolve()
    folder = spec_path.parent
    final = f"{ShortSpec.load(spec_path).id}.mp4"
    inputs = [
        path.relative_to(folder).as_posix()
        for path in sorted(folder.rglob("*"))
        if path.is_file() and _is_input(path.relative_to(folder), final)
    ]
    bundle = _pack(folder, inputs)
    if modal.is_local():
        with modal.enable_output(), app.run():
            video_bytes, results = render_episode.remote(bundle, spec_path.name)
    else:
        # Inside a studio worker, which already runs in the deployed app.
        video_bytes, results = render_episode.remote(bundle, spec_path.name)
    _unpack(results, folder)
    video = (out or folder / final).resolve()
    video.write_bytes(video_bytes)
    return video


# --- whole episodes ------------------------------------------------------------------------------------------


def _kept(relative: Path) -> bool:
    """The files git keeps from an episode folder (see .gitignore)."""
    if relative.suffix in (".mp4", ".wav"):
        return False
    if relative.parts[0] == "work":
        return len(relative.parts) == 2 and relative.name in ("manifest.json", "speech.json")
    return True


def _episode_files(root: Path, folder: Path) -> list[str]:
    return [p.relative_to(root).as_posix() for p in sorted(folder.rglob("*")) if p.is_file() and _kept(p.relative_to(folder))]


def context_bundle(root: Path, episode_id: str) -> bytes:
    """strategy/ and the recent episodes a worker writes and judges against, laid out as in the repo."""
    episodes = root / "content" / "episodes"
    folders = sorted(episodes.glob("ep[0-9][0-9][0-9]"))[-CONTEXT_EPISODES:]
    if episodes / episode_id not in folders:
        folders.append(episodes / episode_id)
    names = [p.relative_to(root).as_posix() for p in sorted((root / "strategy").rglob("*")) if p.is_file()]
    for folder in folders:
        names += _episode_files(root, folder)
    return _pack(root, names)


# A worker spends most of its time waiting on the language models and image sources, and Modal bills the CPU it
# reserves. Its memory also holds the Cursor SDK's Node bridge (about 190 MB).
@app.function(image=image, cpu=0.25, memory=1536, timeout=4 * 3600, volumes={"/outbox": outbox}, secrets=[studio_secret])
def studio_episode(context: bytes, job: dict) -> dict:
    """Make one Short from its topic, host it, and leave its folder in the outbox for the next studio run."""
    from . import llm, studio

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "urllib3", "trafilatura", "primp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _unpack(context, REMOTE_ROOT)
    episode_id = job["id"]
    llm.CURSOR_FALLBACK = job.get("cursor", True)
    studio.LAST_RESORT = job.get("last_resort", False)
    try:
        outcome = studio.produce(job["topic"], job["series"], episode_id, at=job.get("at"))
    except Exception as err:
        logging.exception("%s stopped before it was finished", episode_id)
        outcome = {"id": episode_id, "outcome": "unfinished", "topic": job["topic"],
                   "reason": f"{type(err).__name__}: {err}"[:600], "quota": isinstance(err, llm.OutOfQuota),
                   "overloaded": isinstance(err, llm.Overloaded)}
    outcome["renders"] = list(studio.renders)
    outcome["llm_calls"] = [{"model": c["model"], "purpose": c["purpose"]} for c in llm.calls]
    outcome["stopped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    folder = next((f for f in (studio.EPISODES / episode_id, studio.REJECTED / episode_id) if f.exists()), None)
    names = _episode_files(REMOTE_ROOT, folder) if folder else []
    # Named for this start, so a worker that was given up on can't have its files taken for a later one's.
    key = job.get("key", episode_id)
    Path("/outbox", f"{key}.tar").write_bytes(_pack(REMOTE_ROOT, names))
    # Written last: the collector takes the JSON's presence to mean the tar beside it is complete.
    Path("/outbox", f"{key}.json").write_text(json.dumps(outcome, default=str), encoding="utf-8")
    outbox.commit()
    return outcome


# --- the studio's runs ---------------------------------------------------------------------------------------

REPO_URL = "git@github.com:ttannu/days-of-odd.git"
# GitHub's published host key (https://api.github.com/meta), so nothing else can answer as github.com.
GITHUB_HOST_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
CHECKOUT = Path("/tmp/days-of-odd")
# 11:05, 17:05, 23:05, and 05:05 IST; the daily routine goes in the first run after 14:00 IST (auto.DAILY_FROM_HOUR).
SCHEDULE = "35 5,11,17,23 * * *"
# What a run changes. Code only changes through a person's commit, which a run's rebase keeps.
SAVED = ("content", "strategy", "reports", "analytics", "status")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(CHECKOUT), *args], capture_output=True, text=True)


def _alert(message: str) -> None:
    """Push to the owner's phone through ntfy, as monitor/monitor.py does; a run that can't save stops the channel."""
    if topic := os.environ.get("YTC_NTFY_TOPIC"):
        request = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"), method="POST",
                                         headers={"Title": "Days of Odd needs attention", "Priority": "high", "Tags": "warning"})
        try:
            urllib.request.urlopen(request, timeout=30).read()
        except Exception as err:
            logging.warning("ntfy push failed: %s", err)


def _checkout() -> None:
    ssh = Path.home() / ".ssh"
    ssh.mkdir(mode=0o700, exist_ok=True)
    (ssh / "id_ed25519").write_text(os.environ["YTC_DEPLOY_KEY"].strip() + "\n", encoding="utf-8")
    (ssh / "id_ed25519").chmod(0o600)
    (ssh / "known_hosts").write_text(GITHUB_HOST_KEY + "\n", encoding="utf-8")
    # A container Modal kept warm after a run still has that run's clone.
    shutil.rmtree(CHECKOUT, ignore_errors=True)
    done = subprocess.run(["git", "clone", "-q", "--depth", "20", REPO_URL, str(CHECKOUT)], capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"couldn't clone the repo: {done.stderr.strip()[:400]}")
    _git("config", "user.name", "Days of Odd studio")
    _git("config", "user.email", "studio@days-of-odd.invalid")
    # A run that couldn't save would repeat its work (start workers, schedule posts) on the next run's clone.
    if (done := _git("push", "--dry-run", "-q", "origin", "HEAD")).returncode:
        raise RuntimeError(f"the deploy key can't push to the repo: {done.stderr.strip()[:400]}")
    secrets = CHECKOUT / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / "client_secret.json").write_text(os.environ["YTC_GOOGLE_CLIENT"], encoding="utf-8")
    (secrets / "token.json").write_text(os.environ["YTC_GOOGLE_TOKEN"], encoding="utf-8")


def _save(message: str) -> str:
    for path in SAVED:
        if (CHECKOUT / path).exists():
            _git("add", "-A", path)
    if _git("diff", "--cached", "--quiet").returncode == 0:
        return "nothing changed"
    _git("commit", "-q", "-m", message)
    error = ""
    for attempt in range(5):
        # Autostash: a stray change outside SAVED would otherwise stop the rebase.
        if _git("pull", "-q", "--rebase", "--autostash", "-X", "theirs").returncode:
            _git("rebase", "--abort")
            error = "the rebase onto the repo's latest commit failed"
        elif (pushed := _git("push", "-q")).returncode:
            error = pushed.stderr.strip()[:400]
        else:
            return "pushed"
        time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"couldn't push the run's changes: {error}")


# A run that outlives this is stopped, and what it did so far is still saved: workers it started must not be
# started again by the next run.
RUN_MINUTES = 100


# One at a time: a second call waits for the first, which it has to start from.
@app.function(image=run_image, cpu=1.0, memory=2048, timeout=2 * 3600, schedule=modal.Cron(SCHEDULE),
              secrets=[run_secret], max_containers=1)
def studio_run(trigger: str = "schedule", args: list[str] | None = None) -> dict:
    """One studio run (`ytc auto`) on a fresh clone of the repo; what it changed is pushed back."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    started = datetime.now(timezone.utc)
    try:
        _checkout()
    except Exception as err:
        _alert(f"The studio's run on Modal couldn't start: {err}"[:900])
        raise
    env = {**os.environ, "PYTHONPATH": str(CHECKOUT / "pipeline" / "src"), "PYTHONUNBUFFERED": "1"}
    command = [sys.executable, "-c", "import ytc; ytc.main()", "auto", "--trigger", trigger, *(args or [])]
    try:
        code = subprocess.run(command, cwd=CHECKOUT / "pipeline", env=env, timeout=RUN_MINUTES * 60).returncode
    except subprocess.TimeoutExpired:
        code = 124
    try:
        saved = _save(f"studio: Modal run {started:%Y-%m-%d %H:%M} UTC ({trigger})")
    except Exception as err:
        _alert(f"The studio's run on Modal couldn't save what it did: {err}"[:900])
        raise
    if code:
        why = f"ran over {RUN_MINUTES} minutes and was stopped" if code == 124 else f"failed (exit {code})"
        _alert(f"The studio's run on Modal {why}. Its log: modal.com > days-of-odd > studio_run.")
    return {"exit": code, "saved": saved, "minutes": round((datetime.now(timezone.utc) - started).total_seconds() / 60, 1)}


# A few minutes after each of publish.SLOTS, in the audience's time. Buffer failed about one post in five in the first
# days, and a studio run only finds a failure hours later, when the next free slot is days away.
WATCH_SCHEDULE = "5,20,40 " + ",".join(str(h) for h in sorted({slot.hour for slot in SLOTS})) + " * * *"
# What slot_watch keeps between checks: Buffer's organization id (a lookup would cost one of Buffer's 250 requests a
# day on every check), and "alerted:<post id>" for each post it told the owner it couldn't send again, so the phone
# hears about a post once rather than on every check.
watch_state = modal.Dict.from_name("days-of-odd-slot-watch", create_if_missing=True)


@app.function(image=image, cpu=0.25, memory=512, timeout=600, secrets=[run_secret], max_containers=1,
              schedule=modal.Cron(WATCH_SCHEDULE, timezone=AUDIENCE_TZ.key))
def slot_watch() -> list[str]:
    """Send each post Buffer just failed again (publish.resend_failed). It changes Buffer only: the post keeps its
    id, so the next studio run's sync records how it went."""
    from . import publish

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if not os.environ.get("BUFFER_ORG_ID") and (org := watch_state.get("organization")):
        os.environ["BUFFER_ORG_ID"] = org
    try:
        results = publish.resend_failed()
        watch_state["organization"] = publish.organization_id()
    except publish.BufferBusy as err:
        logging.warning("%s; the next check tries again", err)
        return []
    lines = [f'"{r["title"]}" {"sent" if r["sent"] else "not sent"} again: {r["note"]}' for r in results]
    for line in lines:
        logging.info(line)
    stuck = [(r, line) for r, line in zip(results, lines) if not r["sent"] and not watch_state.get(f"alerted:{r['id']}")]
    if stuck:
        _alert("Buffer failed a Short, and it couldn't be sent again: " + "; ".join(line for _, line in stuck)[:800])
        for r, _ in stuck:
            watch_state[f"alerted:{r['id']}"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return lines


def deploy() -> None:
    """Publish this code as the app the studio's runs and workers run in (works from inside a run on Modal too)."""
    with modal.enable_output():
        app.deploy(name=APP_NAME)


def spawn(root: Path, job: dict) -> str:
    """Start a worker on one episode and return its call id; the run doesn't wait for it."""
    function = modal.Function.from_name(APP_NAME, "studio_episode")
    return function.spawn(context_bundle(root, job["id"]), job).object_id


def _read(name: str) -> bytes | None:
    try:
        return b"".join(outbox.read_file(name))
    except (FileNotFoundError, NotFoundError):
        return None


def collect(root: Path, episode_id: str, call_id: str, key: str | None = None) -> dict | None:
    """A finished worker's outcome, with its files unpacked into the repo; None while it's still working."""
    key = key or episode_id
    raw = _read(f"{key}.json")
    if raw is None:
        try:
            modal.FunctionCall.from_id(call_id).get(timeout=0)
        except TimeoutError:
            return None
        except Exception as err:
            return {"id": episode_id, "outcome": "unfinished", "reason": f"the worker failed: {type(err).__name__}: {err}"[:600]}
        # It may have finished after the first look.
        raw = _read(f"{key}.json")
        if raw is None:
            return {"id": episode_id, "outcome": "unfinished", "reason": "the worker ended without leaving its files"}
    outcome = json.loads(raw)
    if data := _read(f"{key}.tar"):
        _unpack(data, root)
    for name in (f"{key}.tar", f"{key}.json"):
        outbox.remove_file(name)
    return outcome


def sweep(keep: set[str], days: float = 1) -> int:
    """Delete outbox files that no episode waits for: from workers given up on, or whose start never reached git."""
    cutoff = time.time() - days * 86400
    removed = 0
    for entry in outbox.listdir("/"):
        name = entry.path.rsplit("/", 1)[-1]
        if name.rsplit(".", 1)[0] not in keep and entry.mtime < cutoff:
            outbox.remove_file(entry.path)
            removed += 1
    return removed


def cancel(call_id: str) -> None:
    modal.FunctionCall.from_id(call_id).cancel()


def month_cost() -> dict:
    """This billing cycle's Modal cost in dollars, and how much the plan's free credit didn't cover."""
    summary = modal.Workspace.from_context().billing.summary()
    return {"metered": round(float(summary.metered_cost), 2), "billed": round(float(summary.billed_cost), 2)}
