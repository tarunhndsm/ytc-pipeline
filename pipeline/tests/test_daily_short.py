from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ytc import facebook, instagram, reel_cloud, reels
from ytc.growth import brands

IST = reel_cloud.IST
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=IST)


def _record(folder: Path, name: str, due: datetime, **extra) -> None:
    (folder / name).write_text(json.dumps({"dueAt": due.astimezone(timezone.utc).isoformat(), **extra}),
                               encoding="utf-8")


class DailyShortTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        growth = self.root / "automation" / "growth"
        growth.mkdir(parents=True)
        for patcher in (patch.object(reels, "REELS", self.root),
                        patch.object(brands, "ANKAHI", dataclasses.replace(brands.ANKAHI, out=growth)),
                        patch.dict(os.environ, {"IG_BUFFER_YOUTUBE_CHANNEL_ID": "yt-test"}),
                        patch("ytc.instagram.posts_by_reel", return_value={})):
            patcher.start()
            self.addCleanup(patcher.stop)
        posts = {}
        # Due on Instagram this long before NOW, and views there (read at NOW).
        for reel, hours, views in (("ak070", 38, 9000), ("ak071", 26, 3000), ("ak072", 21, 50000),
                                   ("ak073", 9 * 24, 100000), ("ak074", 45, 1000)):
            folder = self.root / reel
            folder.mkdir()
            (folder / "short.yaml").write_text(f"id: {reel}\n", encoding="utf-8")
            sent = NOW - timedelta(hours=hours)
            _record(folder, instagram.RECORD, sent, media_public_id=f"ankahikathayein/{reel}")
            posts[f"post-{reel}"] = {"id": f"post-{reel}", "platform": "instagram", "reel": reel,
                                     "sent": sent.astimezone(timezone.utc).isoformat(),
                                     "seen": NOW.astimezone(timezone.utc).isoformat(), "metrics": {"views": views}}
        (growth / "posts.json").write_text(json.dumps({"posts": posts}), encoding="utf-8")

    def test_the_best_reel_of_the_week_without_a_short_is_picked(self) -> None:
        # ak072 is under a day on Instagram and ak073 over a week, so the week's Reels are ak070, ak071 and ak074.
        self.assertEqual(reel_cloud.pick_short(NOW)[0].name, "ak070")
        _record(self.root / "ak070", instagram.YOUTUBE_RECORD, NOW - timedelta(hours=10))
        self.assertEqual(reel_cloud.pick_short(NOW)[0].name, "ak071")

    def test_no_reel_under_the_weeks_median_is_picked(self) -> None:
        for reel in ("ak070", "ak071"):
            _record(self.root / reel, instagram.YOUTUBE_RECORD, NOW - timedelta(hours=10))
        picked, why = reel_cloud.pick_short(NOW)
        self.assertIsNone(picked)
        self.assertIn("ak074", why)

    def test_the_days_short_is_booked_at_its_slot_within_the_hours_before(self) -> None:
        done = {"mirrored": {}, "held": {}, "errors": []}
        planned = reel_cloud.mirror(NOW, done, dry=True)
        self.assertEqual(planned, [datetime(2026, 10, 21, 20, 0, tzinfo=IST)])
        self.assertTrue(done["mirrored"]["ak070"].startswith("would go out on YouTube Wed 21 Oct 20:00 IST"))

    def test_nothing_is_booked_too_early_or_on_a_day_with_a_short(self) -> None:
        done = {"mirrored": {}, "held": {}, "errors": []}
        self.assertEqual(reel_cloud.mirror(NOW - timedelta(hours=3), done, dry=True), [])
        _record(self.root / "ak074", instagram.YOUTUBE_RECORD, datetime(2026, 10, 21, 18, 30, tzinfo=IST))
        self.assertEqual(reel_cloud.mirror(NOW, done, dry=True), [])
        self.assertEqual(done["mirrored"], {})

    def test_only_days_without_instagram_count_as_idle(self) -> None:
        _record(self.root / "ak072", instagram.RECORD, NOW + timedelta(hours=3))
        self.assertEqual(reel_cloud.idle_days(NOW, []), ["nothing on Instagram Thu 22 Oct"])
        later = self.root / "ak075"
        later.mkdir()
        _record(later, instagram.RECORD, NOW + timedelta(days=1, hours=3))
        self.assertEqual(reel_cloud.idle_days(NOW, []), [])

    def test_a_reel_youtube_passed_over_leaves_cloudinary_after_a_week(self) -> None:
        for reel, hours in (("ak071", 26), ("ak073", 9 * 24)):
            _record(self.root / reel, facebook.RECORD, NOW - timedelta(hours=hours),
                    media_public_id=f"ankahikathayein/{reel}")
        done = {"unhosted": {}}
        reel_cloud.unhost_sent(NOW, done, dry=True)
        self.assertEqual(list(done["unhosted"]), ["ak073"])


if __name__ == "__main__":
    unittest.main()
