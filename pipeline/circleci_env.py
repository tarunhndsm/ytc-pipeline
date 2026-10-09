"""Set up, from the Mac, what @ankahi.kathayein's runs on CircleCI (ytc.reel_host) need. Prints no secret values.

    python3 pipeline/circleci_env.py      # needs a CircleCI personal API token on a CIRCLECI_TOKEN line in pipeline/.env

1. A deploy key that can push to this repo and nothing else (secrets/circleci_deploy_key, git-ignored), apart from the
   studio's Modal key, so CircleCI's access can be revoked alone.
2. The CircleCI project's environment variables: the brand's keys in pipeline/.env (modal_secret.BRAND_ENV), the token
   for the renders' repo and the public copy (AK_MEDIA_TOKEN, a fine-grained token of the owner's personal account),
   the brand YouTube channel's sign-in and the deploy key.
"""

import json
import os
import urllib.error
import urllib.request

from modal_secret import BRAND_ENV, BRAND_TOKEN, REPO, ROOT, env_values, run

KEY = ROOT / "secrets" / "circleci_deploy_key"
PROJECT = os.environ.get("AK_CIRCLECI_PROJECT", "gh/tarunhndsm/ytc-pipeline")
API = "https://circleci.com/api/v2"
WANTED = (*BRAND_ENV, "AK_MEDIA_TOKEN", "IG_YOUTUBE_TOKEN", "YTC_DEPLOY_KEY")


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


if __name__ == "__main__":
    deploy_key()
    variables()
