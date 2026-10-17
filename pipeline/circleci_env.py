"""Set up, from the Mac, what @ankahi.kathayein's runs on CircleCI (ytc.reel_host) need. Prints no secret values.

    python3 pipeline/circleci_env.py      # needs a CircleCI personal API token on a CIRCLECI_TOKEN line in pipeline/.env
    python3 pipeline/circleci_env.py watchdog     # only the third
    python3 pipeline/circleci_env.py growth       # only the growth desk's schedule

1. A deploy key that can push to this repo and nothing else (secrets/circleci_deploy_key, git-ignored), apart from the
   studio's Modal key, so CircleCI's access can be revoked alone.
2. The CircleCI project's environment variables: the brand's keys in pipeline/.env (modal_secret.BRAND_ENV), the token
   for the renders' repo and the public copy (AK_MEDIA_TOKEN, a fine-grained token of the owner's personal account),
   the brand YouTube channel's sign-in and the deploy key.
3. Days of Odd's watchdog (reel_host's `watchdog`): Days of Odd's own keys in the WATCH_CONTEXT context, which only
   that job gets, and its schedule, every 3 hours.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from modal_secret import BRAND_ENV, BRAND_TOKEN, REPO, ROOT, env_values, run

KEY = ROOT / "secrets" / "circleci_deploy_key"
PROJECT = os.environ.get("AK_CIRCLECI_PROJECT", "gh/tarunhndsm/ytc-pipeline")
API = "https://circleci.com/api/v2"
WANTED = (*BRAND_ENV, "AK_MEDIA_TOKEN", "IG_YOUTUBE_TOKEN", "YTC_DEPLOY_KEY")
WATCH_CONTEXT = "days-of-odd"
WATCH_KEYS = ("BUFFER_API_KEY", "BUFFER_YOUTUBE_CHANNEL_ID", "CLOUDINARY_URL", "YTC_NTFY_TOPIC")  # reel_host.WATCH_KEYS
WATCH_SCHEDULE = "days of odd watchdog"
GROWTH_SCHEDULE = "ankahi growth desk"


def deploy_key() -> None:
    if not KEY.exists():
        run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "CircleCI Reels runs", "-f", str(KEY))
        print("made the deploy key")
    if "CircleCI Reels runs" not in run("gh", "repo", "deploy-key", "list", "--repo", REPO):
        run("gh", "repo", "deploy-key", "add", str(KEY.with_suffix(".pub")), "--repo", REPO, "--allow-write",
            "--title", "CircleCI Reels runs")
        print(f"added it to {REPO}, with write access")


def variables() -> None:
    values = env_values()
    if not values.get("CIRCLECI_TOKEN"):
        raise SystemExit("put a CircleCI personal API token on a CIRCLECI_TOKEN line in pipeline/.env first")
    payload = {k: values[k] for k in (*BRAND_ENV, "AK_MEDIA_TOKEN") if values.get(k)}
    payload["YTC_DEPLOY_KEY"] = KEY.read_text(encoding="utf-8")
    if BRAND_TOKEN.exists():
        payload["IG_YOUTUBE_TOKEN"] = BRAND_TOKEN.read_text(encoding="utf-8")
    for name, value in payload.items():
        request = urllib.request.Request(f"{API}/project/{PROJECT}/envvar", method="POST",
                                         data=json.dumps({"name": name, "value": value}).encode(),
                                         headers={"Circle-Token": values["CIRCLECI_TOKEN"],
                                                  "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=30).read()
        except urllib.error.HTTPError as err:
            raise SystemExit(f"setting {name} on {PROJECT} failed: HTTP {err.code}")
    missing = [k for k in WANTED if k not in payload]
    print(f"CircleCI {PROJECT}: {', '.join(sorted(payload))}" + (f" (missing: {', '.join(missing)})" if missing else ""))


def _call(method: str, path: str, token: str, body: dict | None = None):
    request = urllib.request.Request(f"{API}/{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Circle-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as err:
        raise SystemExit(f"{method} {path} failed: HTTP {err.code}")


def watchdog() -> None:
    values = env_values()
    token = values["CIRCLECI_TOKEN"]
    org = _call("GET", f"project/{PROJECT}", token)["organization_id"]
    contexts = _call("GET", f"context?owner-id={org}&owner-type=organization", token)["items"]
    context = next((c for c in contexts if c["name"] == WATCH_CONTEXT), None) or _call(
        "POST", "context", token, {"name": WATCH_CONTEXT, "owner": {"id": org, "type": "organization"}})
    for key in WATCH_KEYS:
        _call("PUT", f"context/{context['id']}/environment-variable/{key}", token, {"value": values[key]})
    if not any(s["name"] == WATCH_SCHEDULE for s in _call("GET", f"project/{PROJECT}/schedule", token)["items"]):
        _call("POST", f"project/{PROJECT}/schedule", token, {
            "name": WATCH_SCHEDULE, "description": "Days of Odd's monitor every 3 hours (ytc.reel_host watchdog)",
            "attribution-actor": "system", "parameters": {"branch": "main", "run": "watchdog"},
            "timetable": {"per-hour": 1, "hours-of-day": list(range(0, 24, 3)),
                          "days-of-week": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]}})
    print(f"CircleCI context {WATCH_CONTEXT}: {', '.join(WATCH_KEYS)}; schedule \"{WATCH_SCHEDULE}\", every 3 hours")


def growth() -> None:
    """The growth desk's schedule (reel_host's `growth`): 02, 08, 14 and 20 UTC, so the strategist's daily run, which
    waits for the morning in India, lands at 07:30 IST."""
    token = env_values()["CIRCLECI_TOKEN"]
    if not any(s["name"] == GROWTH_SCHEDULE for s in _call("GET", f"project/{PROJECT}/schedule", token)["items"]):
        _call("POST", f"project/{PROJECT}/schedule", token, {
            "name": GROWTH_SCHEDULE, "description": "The Reels' growth desk every 6 hours (ytc.reel_host growth)",
            "attribution-actor": "system", "parameters": {"branch": "main", "run": "growth"},
            "timetable": {"per-hour": 1, "hours-of-day": [2, 8, 14, 20],
                          "days-of-week": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]}})
    print(f"CircleCI schedule \"{GROWTH_SCHEDULE}\": 02, 08, 14 and 20 UTC")


if __name__ == "__main__":
    if sys.argv[1:] == ["growth"]:
        growth()
        raise SystemExit
    if sys.argv[1:] != ["watchdog"]:
        deploy_key()
        variables()
    watchdog()
