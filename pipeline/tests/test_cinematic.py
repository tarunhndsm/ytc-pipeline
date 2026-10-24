"""Reels painted line by line (reel_art.CINEMATIC): which Reels count as painted, how one takes an older Reel's
Instagram slot, what the factory still makes, and the writer's rule for line 1."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ytc import instagram, reel_art, reel_cloud, reel_factory, reel_pictures, reel_writer, reels

IST = reel_cloud.IST
NOW = datetime(2026, 10, 23, 6, 0, tzinfo=IST)
DELETED = {"deletePost": {"__typename": "DeletePostSuccess"}}


def _reel(root: Path, name: str, painted: bool, due: datetime | None = None) -> Path:
    folder = root / name
    (folder / "work").mkdir(parents=True)
    (folder / "short.yaml").write_text(f"id: {name}\n", encoding="utf-8")
    second = ({"ref": f"ai:{name}-02-1", "license": reel_art.LICENSE} if painted else
              {"ref": "met-12345", "license": "CC0"})
    pictures = {"1": {"ref": f"ai:{name}-01-1", "license": reel_art.LICENSE}, "2": second}
    (folder / reel_pictures.PICTURES).write_text(json.dumps(pictures), encoding="utf-8")
    if due:
        (folder / instagram.RECORD).write_text(json.dumps(
            {"id": f"post-{name}", "dueAt": due.astimezone(timezone.utc).isoformat()}), encoding="utf-8")
    return folder


class PaintedTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for patcher in (patch.object(reels, "REELS", self.root), patch.object(reel_art, "CINEMATIC", True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _incoming(self, *names: str) -> None:
        for name in names:
            _reel(self.root, name, painted=True)

    def test_only_a_reel_whose_every_picture_was_painted_for_it_counts(self) -> None:
        self.assertTrue(reel_pictures.all_painted(_reel(self.root, "ak101", painted=True)))
        self.assertFalse(reel_pictures.all_painted(_reel(self.root, "ak102", painted=False)))
        (self.root / "ak103").mkdir()
        self.assertFalse(reel_pictures.all_painted(self.root / "ak103"))

    def test_a_painted_reel_takes_the_soonest_older_booking_more_than_an_hour_ahead(self) -> None:
        _reel(self.root, "ak101", painted=False, due=NOW + timedelta(minutes=40))
        _reel(self.root, "ak102", painted=False, due=NOW + timedelta(hours=29))
        _reel(self.root, "ak103", painted=False, due=NOW + timedelta(hours=5))
        _reel(self.root, "ak104", painted=True, due=NOW + timedelta(hours=3))
        self._incoming("ak110", "ak111", "ak112")
        done, swapped, later = {"moved": {}, "errors": []}, set(), [NOW + timedelta(days=3)]
        with patch.object(instagram, "_buffer") as buffer:
            self.assertEqual(reel_cloud.swap_for("ak110", NOW, done, swapped, later, dry=True), NOW + timedelta(hours=5))
            self.assertEqual(reel_cloud.swap_for("ak111", NOW, done, swapped, later, dry=True),
                             NOW + timedelta(hours=29))
            self.assertIsNone(reel_cloud.swap_for("ak112", NOW, done, swapped, later, dry=True))
        buffer.assert_not_called()
        self.assertEqual(sorted(done["moved"]), ["ak102", "ak103"])
        self.assertIn("would be taken off Instagram's Fri 23 Oct 11:00 IST for ak110", done["moved"]["ak103"])

    def test_an_open_slot_before_the_older_booking_is_taken_instead(self) -> None:
        _reel(self.root, "ak101", painted=False, due=NOW + timedelta(hours=5))
        self._incoming("ak110")
        done = {"moved": {}, "errors": []}
        self.assertIsNone(reel_cloud.swap_for("ak110", NOW, done, set(), [NOW + timedelta(hours=4)], dry=True))
        self.assertEqual(reel_cloud.swap_for("ak110", NOW, done, set(), [], dry=True), NOW + timedelta(hours=5))

    def test_with_cinematic_off_no_booking_is_taken(self) -> None:
        _reel(self.root, "ak101", painted=False, due=NOW + timedelta(hours=5))
        self._incoming("ak110")
        with patch.object(reel_art, "CINEMATIC", False):
            self.assertIsNone(reel_cloud.swap_for("ak110", NOW, {"moved": {}, "errors": []}, set(), [], dry=True))

    def test_an_older_reel_takes_no_booking(self) -> None:
        _reel(self.root, "ak101", painted=False, due=NOW + timedelta(hours=5))
        _reel(self.root, "ak120", painted=False)
        self.assertIsNone(reel_cloud.swap_for("ak120", NOW, {"moved": {}, "errors": []}, set(), [], dry=True))

    def test_the_queue_is_filled_with_painted_reels_first_then_older_ones(self) -> None:
        for name, painted, total in (("ak130", False, 70.0), ("ak131", True, 60.0)):
            folder = _reel(self.root, name, painted=painted)
            (folder / f"{name}.mp4").write_bytes(b"")
            (folder / reels.REVIEW).write_text(json.dumps({"total": total}), encoding="utf-8")
        done = {"scheduled": {}, "held": {}, "moved": {}, "errors": []}
        planned: list[datetime] = []
        with patch.object(reels, "rescued", return_value=None), \
                patch.object(reels, "unshippable", return_value=None), \
                patch.object(reels, "bar", return_value=73.4), \
                patch.object(reel_cloud, "_dues", return_value=[]), \
                patch.object(reel_cloud, "_after", return_value=(None, "")), \
                patch.object(reel_cloud, "free_slots",
                             side_effect=lambda now, count, planned, after: [NOW + timedelta(hours=2 + len(planned))]):
            reel_cloud.fill_queue(NOW, done, planned, dry=True)
        self.assertEqual(list(done["scheduled"]), ["ak131", "ak130"])

    def test_the_older_reels_post_is_deleted_with_the_brands_key_and_it_is_not_booked_again(self) -> None:
        folder = _reel(self.root, "ak101", painted=False, due=NOW + timedelta(hours=5))
        self._incoming("ak110")
        done = {"moved": {}, "errors": []}
        with patch.object(instagram, "_buffer", return_value=DELETED) as buffer:
            self.assertEqual(reel_cloud.swap_for("ak110", NOW, done, set(), []), NOW + timedelta(hours=5))
        self.assertEqual(buffer.call_args.args[1], {"input": {"id": "post-ak101"}})
        record = json.loads((folder / instagram.RECORD).read_text(encoding="utf-8"))
        self.assertIn("ak110", record["withdrawn_why"])
        self.assertIsNone(reel_cloud._due(folder))
        self.assertIsNone(reel_cloud.old_booking(NOW, set()))

    def test_a_post_buffer_wont_delete_keeps_its_slot(self) -> None:
        folder = _reel(self.root, "ak101", painted=False, due=NOW + timedelta(hours=5))
        self._incoming("ak110")
        done = {"moved": {}, "errors": []}
        refused = {"deletePost": {"__typename": "VoidMutationError", "message": "post not found"}}
        with patch.object(instagram, "_buffer", return_value=refused):
            self.assertIsNone(reel_cloud.swap_for("ak110", NOW, done, set(), []))
        self.assertIn("post not found", done["errors"][0])
        self.assertEqual(done["moved"], {})
        self.assertIsNotNone(reel_cloud._due(folder))

    def test_the_factory_counts_only_painted_reels_as_waiting(self) -> None:
        _reel(self.root, "ak101", painted=False)
        _reel(self.root, "ak102", painted=True)
        _reel(self.root, "ak103", painted=False, due=NOW + timedelta(hours=5))
        found = reel_factory.supply({"reels": {}}, NOW)
        self.assertEqual(found["pending"], ["ak102"])
        self.assertEqual(found["booked"], ["ak103"])


class PictureStepTests(unittest.TestCase):
    def test_every_line_is_painted_but_those_an_earlier_try_painted_and_kept(self) -> None:
        beats = [{"text": f"line {n}", "picture": f"brief {n}", "role": "story"} for n in range(1, 6)]
        draft = {"id": "ak999", "script": {"beats": beats}}
        kept = {2: {"ref": "ai:ak999-02-1"}, 3: {"ref": "met-1"}}
        with (patch.object(reel_pictures, "kept_picks", return_value=kept),
              patch.object(reel_pictures, "paint_framed", side_effect=lambda r, b, lines: {n: {"ref": f"ai:{n}"}
                                                                                          for n in lines}) as paint,
              patch.object(reel_pictures, "lay_out", return_value=Path("short.yaml")) as lay_out):
            reel_pictures.cinematic(Path("ak999"), draft, set())
        self.assertEqual(paint.call_args.args[2], [1, 3, 4, 5])
        chosen = lay_out.call_args.args[2]
        self.assertEqual(sorted(chosen), [1, 2, 3, 4, 5])
        self.assertEqual(chosen[2]["ref"], "ai:ak999-02-1")
        self.assertEqual(chosen[3]["ref"], "ai:3")


class HookTests(unittest.TestCase):
    @staticmethod
    def _script(first: str) -> dict:
        def beat(text: str) -> dict:
            return {"text": text, "emphasis": text.split()[0], "sfx": "", "search": ["Krishna"],
                    "picture": "Krishna smiling"}
        return {"beats": [beat(first)] + [beat("कृष्ण मुस्कुराए और बोले।")] * 11, "hook_text": "कृष्ण भी हार गए!",
                "title": "कृष्ण क्यों हारे?", "hashtags": ["#krishna"]}

    def test_a_line_1_that_asks_a_question_goes_back_to_the_writer(self) -> None:
        asks = [p for p in reel_writer.problems(self._script("कृष्ण ने क्या अनोखी दक्षिणा दी?")) if p.startswith("Line 1 asks")]
        states = [p for p in reel_writer.problems(self._script("रावण का असली नाम… रावण नहीं था।"))
                  if p.startswith("Line 1 asks")]
        self.assertEqual((len(asks), states), (1, []))

    def test_the_writer_is_asked_for_a_painting_for_every_line_and_a_close_first_frame(self) -> None:
        with patch.object(reel_art, "CINEMATIC", True):
            self.assertIn("close up", reel_writer.PAINTED_PICTURES)
            self.assertNotIn("museum", reel_writer.PAINTED_PICTURES.lower())

    def test_no_picture_shows_a_broken_body_from_brief_to_judge(self) -> None:
        # ak086 (23 Oct) was painted with the two halves of Jarasandha lying at a crossroads.
        for text in (reel_writer.PAINTED_PICTURES, reel_pictures.PAINTED_RULES, reels.REVIEW_PROMPT):
            self.assertIn("a dead, cut or broken body", " ".join(text.split()))
        self.assertIn("at most 3", " ".join(reels.REVIEW_PROMPT.split()))


if __name__ == "__main__":
    unittest.main()
