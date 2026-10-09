"""@ankahi.kathayein's renders in draft releases of a private repo of the owner's personal GitHub account (REPO), which
only people with push access see, so a run on any host finds what git leaves out (reel_host, on CircleCI).

`media akNNN` holds what git leaves out of a Reel's render (reel_cloud.MEDIA) and its manifest (reel_cloud.RENDER),
and the same for the Reel's YouTube cut under a "youtube." prefix. `wip akNNN` holds a half-made Reel's folder as
one tar between factory runs, and `factory logs` the factory steps' latest logs. Asset names are the paths with "/"
turned into ".".

The token (AK_MEDIA_TOKEN) is a fine-grained one of that account with only Contents read and write, on this repo and
the public copy CircleCI runs from (reel_host.publish).
"""

from __future__ import annotations

import logging
import os
import tarfile
import tempfile
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
REPO = os.environ.get("AK_MEDIA_REPO", "tarunhndsm/ankahi-media")
LOGS = "factory logs"


def asset_name(path: str, prefix: str = "") -> str:
    return prefix + path.replace("/", ".")


class Store:
    def __init__(self, token: str | None = None, repo: str = REPO):
        self.repo = repo
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token or os.environ['AK_MEDIA_TOKEN']}",
                                     "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        self._releases: dict[str, dict] | None = None

    def _call(self, method: str, url: str, timeout: int = 120, **kwargs) -> requests.Response:
        for attempt in range(4):
            try:
                response = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as err:
                if attempt == 3:
                    raise
                log.warning("GitHub %s %s failed (%s); trying again", method, url.split("/repos/")[-1], err)
            else:
                if response.status_code < 500:
                    return response
                if attempt == 3:
                    response.raise_for_status()
                log.warning("GitHub %s %s answered %s; trying again", method, url.split("/repos/")[-1],
                            response.status_code)
            time.sleep(10 * (attempt + 1))
            if "data" in kwargs and hasattr(kwargs["data"], "seek"):
                kwargs["data"].seek(0)
        raise AssertionError("unreachable")

    def releases(self) -> dict[str, dict]:
        """The draft releases by name, each with its assets."""
        if self._releases is None:
            found, page = {}, 1
            while True:
                response = self._call("GET", f"{API}/repos/{self.repo}/releases", params={"per_page": 100, "page": page})
                response.raise_for_status()
                batch = response.json()
                found |= {release["name"]: release for release in batch if release.get("draft")}
                if len(batch) < 100:
                    break
                page += 1
            self._releases = found
        return self._releases

    def assets(self, name: str) -> dict[str, dict]:
        release = self.releases().get(name)
        return {asset["name"]: asset for asset in release["assets"]} if release else {}

    def _release(self, name: str) -> dict:
        if release := self.releases().get(name):
            return release
        response = self._call("POST", f"{API}/repos/{self.repo}/releases",
                              json={"tag_name": name.replace(" ", "-"), "name": name, "draft": True,
                                    "body": "Kept by @ankahi.kathayein's runs (ytc/reel_media.py); not for publishing."})
        response.raise_for_status()
        release = response.json() | {"assets": []}
        self.releases()[name] = release
        return release

    def put(self, name: str, asset: str, path: Path) -> None:
        """Upload a file as a release's asset, replacing one of the same name."""
        release = self._release(name)
        if old := next((a for a in release["assets"] if a["name"] == asset), None):
            self._call("DELETE", f"{API}/repos/{self.repo}/releases/assets/{old['id']}").raise_for_status()
            release["assets"].remove(old)
        with path.open("rb") as data:
            response = self._call("POST", f"{UPLOADS}/repos/{self.repo}/releases/{release['id']}/assets",
                                  params={"name": asset}, data=data, timeout=600,
                                  headers={"Content-Type": "application/octet-stream"})
        response.raise_for_status()
        release["assets"].append(response.json())

    def get(self, name: str, asset: str, path: Path) -> bool:
        """Download a release's asset to a path; False when there's no such asset."""
        if not (found := self.assets(name).get(asset)):
            return False
        response = self._call("GET", f"{API}/repos/{self.repo}/releases/assets/{found['id']}", stream=True, timeout=600,
                              headers={"Accept": "application/octet-stream"})
        response.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        with part.open("wb") as out:
            for chunk in response.iter_content(1 << 20):
                out.write(chunk)
        part.replace(path)
        return True

    def read(self, name: str, asset: str) -> bytes | None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "asset"
            return target.read_bytes() if self.get(name, asset, target) else None

    def drop(self, name: str) -> None:
        if release := self.releases().pop(name, None):
            self._call("DELETE", f"{API}/repos/{self.repo}/releases/{release['id']}").raise_for_status()

    # --- a half-made Reel's folder, as one tar --------------------------------------------------------------------

    def keep_folder(self, name: str, folder: Path) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            packed = Path(tmp) / f"{folder.name}.tar"
            with tarfile.open(packed, "w") as tar:
                tar.add(folder, arcname=folder.name)
            self.put(name, packed.name, packed)

    def bring_folder(self, name: str, reel: str, parent: Path) -> bool:
        """Unpack a kept folder into parent, over what is there; the tar keeps each file's time."""
        with tempfile.TemporaryDirectory() as tmp:
            packed = Path(tmp) / f"{reel}.tar"
            if not self.get(name, packed.name, packed):
                return False
            with tarfile.open(packed) as tar:
                members = [m for m in tar.getmembers() if m.name == reel or m.name.startswith(f"{reel}/")]
                tar.extractall(parent, members=members, filter="data")
        return True
