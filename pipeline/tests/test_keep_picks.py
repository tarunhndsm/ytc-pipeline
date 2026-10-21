from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ytc import reel_art, reel_pictures, reel_writer


def _hit(ref: str) -> dict:
    return {"ref": ref, "title": f"Painting {ref}", "url": f"https://example.org/{ref}.jpg", "source": "The Met",
            "license": "CC0"}


def _framed(ref: str) -> dict:
    return _hit(ref) | {"size": (2000, 3000), "subject": [0.2, 0.2, 0.8, 0.8], "faces": [], "kind": "painting",
                        "hindi": "एक पुराना चित्र"}


class KeepPicksTests(unittest.TestCase):
    """A picture try that can't picture enough lines keeps those it could; the story's next try looks for the rest."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "ak999"
        (self.folder / "work").mkdir(parents=True)
        self.beats = [{"text": f"line {n}", "search": [f"term {n}"], "picture": f"brief {n}", "role": "story"}
                      for n in range(1, 7)]
        (self.folder / reel_writer.SCRIPT).write_text(json.dumps({
            "id": "ak999", "row": {"story": "A story", "source": "Bhagavata Purana 10.11"},
            "script": {"beats": self.beats}}), encoding="utf-8")
        # Each line finds two pictures of its own, and every search finds line 1's first.
        self.per_line = {n: [_hit(f"p{n}a"), _hit(f"p{n}b"), _hit("p1a")] for n in range(1, 7)}
        self.accept: set[int] = set()
        self.asked: list[list[int]] = []
        self.offered: set[str] = set()
        self.saved: dict = {}
        os.environ.pop("YTC_MAX_REUSED", None)
        for patcher in (
                patch.object(reel_art, "ready", return_value=False),
                patch.object(reel_pictures, "candidates",
                             side_effect=lambda beats: {n: list(hits) for n, hits in self.per_line.items()}),
                patch.object(reel_pictures, "shortlist", side_effect=lambda beats, per_line: per_line),
                patch.object(reel_pictures, "choose", side_effect=self._choose),
                patch.object(reel_pictures, "frame_all", side_effect=self._frame_all),
                patch.object(reel_writer, "spec_fields",
                             side_effect=lambda draft: {"beats": [{} for _ in self.beats], "captions": {},
                                                        "description": "about\n"}),
                patch.object(reel_pictures, "save", side_effect=self._save)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _choose(self, beats, shown, story, purpose, fits=None):
        self.asked.append(sorted(shown))
        self.offered |= {hit["ref"] for hits in shown.values() for hit in hits}
        return {n: list(hits) for n, hits in shown.items()}

    def _frame_all(self, beats, ranked, shown, purpose, **_):
        return {n: _framed(hits[0]["ref"]) for n, hits in ranked.items() if n in self.accept and hits}

    def _save(self, folder, spec, chosen, spares):
        self.saved = {"chosen": {n: hit["ref"] for n, hit in chosen.items()}, "spares": spares}
        return folder / "short.yaml"

    def _kept(self) -> dict:
        return json.loads((self.folder / reel_pictures.KEPT).read_text(encoding="utf-8"))

    def test_a_failed_try_keeps_the_pictures_it_accepted(self) -> None:
        self.accept = {1, 2}
        with self.assertRaises(LookupError):
            reel_pictures.picture(self.folder)
        kept = self._kept()
        self.assertEqual({n: hit["ref"] for n, hit in kept.items()}, {"1": "p1a", "2": "p2a"})
        self.assertEqual(kept["2"]["words"], "line 2")
        self.assertEqual(kept["2"]["subject"], [0.2, 0.2, 0.8, 0.8])

    def test_the_next_try_chooses_only_for_the_other_lines(self) -> None:
        self.accept = {1, 2}
        with self.assertRaises(LookupError):
            reel_pictures.picture(self.folder)
        self.accept, self.offered = {3, 4, 5}, set()
        reel_pictures.picture(self.folder)
        self.assertEqual(self.asked[-1], [3, 4, 5, 6])
        self.assertFalse(self.offered & {"p1a", "p2a"}, "a kept picture was offered for another line")
        self.assertEqual(self.saved["chosen"], {1: "p1a", 2: "p2a", 3: "p3a", 4: "p4a", 5: "p5a"})
        # The kept lines still get spares for QA to swap in.
        self.assertEqual(sorted(self.saved["spares"]), [1, 2, 3, 4, 5, 6])
        self.assertFalse((self.folder / reel_pictures.KEPT).exists())

    def test_a_kept_picture_whose_line_changed_or_that_the_judge_faulted_is_dropped(self) -> None:
        (self.folder / reel_pictures.KEPT).write_text(json.dumps({
            "1": _framed("p1a") | {"size": [2000, 3000], "words": "line 1"},
            "2": _framed("p2a") | {"size": [2000, 3000], "words": "the words line 2 had before"},
            "3": _framed("p3a") | {"size": [2000, 3000], "words": "line 3"}}), encoding="utf-8")
        self.assertEqual(sorted(reel_pictures.kept_picks(self.folder, self.beats, {"p3a"})), [1])

    def test_a_story_with_every_line_kept_asks_no_model(self) -> None:
        reel_pictures.keep_picks(self.folder, self.beats, {n: _framed(f"p{n}a") for n in range(1, 7)})
        reel_pictures.picture(self.folder)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.saved["chosen"], {n: f"p{n}a" for n in range(1, 7)})

    def test_the_picture_line_1_borrows_is_kept_for_its_own_line(self) -> None:
        self.accept = {2, 3}
        with self.assertRaises(LookupError):
            reel_pictures.picture(self.folder)
        self.assertEqual({n: hit["ref"] for n, hit in self._kept().items()}, {"2": "p2a", "3": "p3a"})

    def test_the_last_try_passes_on_what_earlier_tries_found(self) -> None:
        self.accept = {1, 2, 3}
        with self.assertRaises(LookupError):
            reel_pictures.picture(self.folder)
        self.accept = set()
        with patch.dict(os.environ, {"YTC_MAX_REUSED": "4"}):
            reel_pictures.picture(self.folder)
        self.assertEqual(self.saved["chosen"], {1: "p1a", 2: "p2a", 3: "p3a"})


if __name__ == "__main__":
    unittest.main()
