"""Set up, from the Mac, what the studio's runs on Modal (cloud.studio_run) sign in with. Prints no secret values.

1. A deploy key that can push to ttannu/days-of-odd and nothing else (secrets/modal_deploy_key, git-ignored).
2. An ntfy topic for alerts on the owner's phone (YTC_NTFY_TOPIC in pipeline/.env and a GitHub secret).
3. The Modal secret `days-of-odd-studio`: the keys in pipeline/.env, YouTube's sign-in, and the deploy key.

    python3 pipeline/modal_secret.py          # all three; what already exists is kept
    python3 pipeline/modal_secret.py modal    # only rebuild the Modal secret, after changing a key in pipeline/.env
    python3 pipeline/modal_secret.py brand    # the Modal secret `ankahi-kathayein`, for the brand's Reels runs
"""

import json
import os
import secrets as tokens
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / "pipeline" / ".env"
KEY = ROOT / "secrets" / "modal_deploy_key"
REPO = "ttannu/days-of-odd"
SECRET = "days-of-odd-studio"
FROM_ENV = ("YTC_CONTACT", "BUFFER_API_KEY", "BUFFER_YOUTUBE_CHANNEL_ID", "CLOUDINARY_URL", "YTC_GEMINI_API_KEY",
            "YTC_MISTRAL_API_KEY", "YTC_OPENROUTER_API_KEY", "YTC_CURSOR_API_KEY", "YTC_NTFY_TOPIC")
# Modal's monthly credit with a card on file (auto.MODAL_CREDIT).
MODAL_CREDIT = "30"
BRAND_SECRET = "ankahi-kathayein"
# What the Instagram brand's runs (ytc.reel_cloud) use: its own Gemini project, Buffer account and that account's
# Instagram, YouTube and Facebook channels, with the studio's Cloudinary account, ntfy topic and contact address (the
# Reel factory's picture searches send it). Of YouTube's sign-ins, only the brand channel's (`ytc auth --brand`),
# never the Days of Odd channel's.
BRAND_ENV = ("IG_GEMINI_API_KEY", "IG_BUFFER_API_KEY", "IG_BUFFER_CHANNEL_ID", "IG_BUFFER_YOUTUBE_CHANNEL_ID",
             "IG_BUFFER_FACEBOOK_CHANNEL_ID", "IG_BUFFER_ORGANIZATION_ID", "CLOUDINARY_URL", "YTC_NTFY_TOPIC",
             "YTC_CONTACT")
BRAND_TOKEN = ROOT / "secrets" / "ig_youtube_token.json"


def env_values() -> dict:
    values = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def run(*args: str, **kw) -> str:
    done = subprocess.run(list(args), capture_output=True, text=True, **kw)
    if done.returncode:
        raise SystemExit(f"{' '.join(args[:3])} failed: {done.stderr.strip()[:400]}")
    return done.stdout


def deploy_key() -> None:
    if not KEY.exists():
        KEY.parent.mkdir(exist_ok=True)
        run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "days-of-odd studio on Modal", "-f", str(KEY))
        print("made the deploy key")
    if "Modal studio runs" not in run("gh", "repo", "deploy-key", "list", "--repo", REPO):
        run("gh", "repo", "deploy-key", "add", str(KEY.with_suffix(".pub")), "--repo", REPO, "--allow-write",
            "--title", "Modal studio runs")
        print("added it to the repository, with write access")


def ntfy_topic() -> None:
    if not env_values().get("YTC_NTFY_TOPIC"):
        topic = "dod-" + "".join(c for c in tokens.token_urlsafe(16) if c.isalnum())[:14].lower()
        with ENV.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# The owner's phone subscribes to this ntfy topic for alerts (monitor/monitor.py).\nYTC_NTFY_TOPIC={topic}\n")
        print("made an ntfy topic, on the YTC_NTFY_TOPIC line of pipeline/.env")
    subprocess.run(["gh", "secret", "set", "YTC_NTFY_TOPIC", "--repo", REPO], input=env_values()["YTC_NTFY_TOPIC"],
                   text=True, check=True, capture_output=True)
    print("set the YTC_NTFY_TOPIC secret on GitHub")


def modal_secret() -> None:
    values = env_values()
    payload = {k: values[k] for k in FROM_ENV if values.get(k)}
    payload |= {"YTC_MODAL_CREDIT": MODAL_CREDIT,
                "YTC_GOOGLE_CLIENT": (ROOT / "secrets" / "client_secret.json").read_text(encoding="utf-8"),
                "YTC_GOOGLE_TOKEN": (ROOT / "secrets" / "token.json").read_text(encoding="utf-8"),
                "YTC_DEPLOY_KEY": KEY.read_text(encoding="utf-8")}
    create(SECRET, payload, FROM_ENV)


def brand_secret() -> None:
    values = env_values()
    payload = {k: values[k] for k in BRAND_ENV if values.get(k)} | {"YTC_DEPLOY_KEY": KEY.read_text(encoding="utf-8")}
    if BRAND_TOKEN.exists():
        payload["IG_YOUTUBE_TOKEN"] = BRAND_TOKEN.read_text(encoding="utf-8")
    create(BRAND_SECRET, payload, BRAND_ENV)


def create(name: str, payload: dict, wanted: tuple[str, ...]) -> None:
    env = {**os.environ, **{k: v for k, v in env_values().items() if k.startswith("MODAL_")}}
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        run("uv", "run", "--no-sync", "modal", "secret", "create", name, "--from-json", path, "--force",
            cwd=ROOT / "pipeline", env=env)
    finally:
        os.remove(path)
    missing = [k for k in wanted if k not in payload]
    print(f"Modal secret {name}: {', '.join(sorted(payload))}" + (f" (missing: {', '.join(missing)})" if missing else ""))


if __name__ == "__main__":
    if sys.argv[1:] == ["brand"]:
        brand_secret()
        sys.exit()
    if sys.argv[1:] != ["modal"]:
        deploy_key()
        ntfy_topic()
    modal_secret()
