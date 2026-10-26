import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ytc import auto, cloud


class FakeOutbox:
    """The results volume: files by name, with the time each was written."""

    def __init__(self, files: dict[str, bytes], age: dict[str, float] | None = None) -> None:
        self.files = dict(files)
        self.mtimes = {name: time.time() - (age or {}).get(name, 0) for name in files}

    def read_file(self, name: str):
        if name not in self.files:
            raise FileNotFoundError(name)
        yield self.files[name]

    def remove_file(self, path: str) -> None:
        name = path.lstrip("/")
        del self.files[name], self.mtimes[name]

    def listdir(self, path: str) -> list:
        return [SimpleNamespace(path=f"/{name}", mtime=self.mtimes[name]) for name in self.files]


def _worker_files(key: str, outcome: str) -> dict[str, bytes]:
    with tempfile.TemporaryDirectory() as made:
        short = Path(made, "content", "episodes", "ep900", "short.yaml")
        short.parent.mkdir(parents=True)
        short.write_text("title: x\n", encoding="utf-8")
        tar = cloud._pack(Path(made), ["content/episodes/ep900/short.yaml"])
    return {f"{key}.tar": tar, f"{key}.json": json.dumps({"id": "ep900", "outcome": outcome}).encode()}


class OutboxTests(unittest.TestCase):
    """Days of Odd's finished workers: a studio run collects what they left in the outbox and pushes it to git."""

    def test_a_run_stopped_before_it_pushes_leaves_the_workers_files_for_its_retry(self) -> None:
        outbox = FakeOutbox(_worker_files("ep900-ab12", "ready"))
        with patch.object(cloud, "outbox", outbox), tempfile.TemporaryDirectory() as first, \
                tempfile.TemporaryDirectory() as retry:
            for root in (first, retry):
                outcome = cloud.collect(Path(root), "ep900", "fc-1", "ep900-ab12")
                self.assertEqual(outcome["outcome"], "ready")
                self.assertTrue(Path(root, "content", "episodes", "ep900", "short.yaml").exists())
        self.assertEqual(set(outbox.files), {"ep900-ab12.tar", "ep900-ab12.json"})

    def test_the_sweep_clears_files_a_day_old_that_no_episode_waits_for(self) -> None:
        day = 86400
        outbox = FakeOutbox({"ep901-aa.json": b"{}", "ep902-bb.json": b"{}", "ep903-cc.json": b"{}"},
                            age={"ep901-aa.json": 2 * day, "ep902-bb.json": 2 * day})
        with patch.object(cloud, "outbox", outbox):
            self.assertEqual(cloud.sweep({"ep902-bb"}), 1)
        self.assertEqual(set(outbox.files), {"ep902-bb.json", "ep903-cc.json"})

    def test_what_a_run_collects_stays_through_its_own_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            remote = {"call_id": "fc-1", "key": "ep900-ab12", "spawned_at": "2026-10-26T11:36:00+00:00", "topic": "t"}
            episode = {"id": "ep900", "state": "remote", "remote": remote, "folder": Path(folder)}
            run = SimpleNamespace(renders={"modal": 0, "runner": 0}, worker_calls=[], made=[], notes=[], quota_until=None)
            with patch.object(auto, "episodes", side_effect=[[episode], []]), \
                    patch.object(auto, "_finish_topic"), \
                    patch.object(cloud, "collect", return_value={"id": "ep900", "outcome": "ready"}), \
                    patch.object(cloud, "sweep", return_value=0) as sweep:
                auto.collect(run)
        sweep.assert_called_once_with({"ep900-ab12"})
        self.assertEqual([m["outcome"] for m in run.made], ["ready"])


if __name__ == "__main__":
    unittest.main()
