from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from ytc import facebook, instagram, publish, reel_cloud, reels


def _url(reel: str) -> str:
    return f"https://res.cloudinary.com/example/video/upload/ankahikathayein/{reel}.mp4"


def _reel(root: Path, number: int, due: datetime, scheduled: datetime | None = None) -> Path:
    reel = f"ak{number:03d}"
    folder = root / reel
    folder.mkdir()
    (folder / "short.yaml").write_text(
        f'id: {reel}\ntitle: A story\ndescription: A sourced story.\n'
        'hashtags: ["#one", "#two", "#three", "#four", "#five", "#six"]\n'
        'beats:\n  - text: One line.\n', encoding="utf-8")
    (folder / instagram.RECORD).write_text(json.dumps({
        "status": "scheduled", "dueAt": due.isoformat(),
        "scheduled_at": (scheduled or due - timedelta(days=2)).isoformat(),
        "media_url": _url(reel),
    }), encoding="utf-8")
    return folder


def _response(data: dict) -> Mock:
    response = Mock(status_code=200)
    response.json.return_value = {"data": data}
    return response


class FacebookTests(unittest.TestCase):
    def test_schedule_creates_a_facebook_reel_with_instagram_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "IG_BUFFER_API_KEY": "test", "IG_BUFFER_FACEBOOK_CHANNEL_ID": "fb-test",
        }):
            root = Path(tmp)
            due = datetime(2026, 1, 20, 20, 0, tzinfo=facebook.IST)
            folder = _reel(root, 1, due)
            head = Mock(status_code=200, headers={"content-type": "video/mp4"})
            success = {"createPost": {"__typename": "PostActionSuccess", "post": {
                "id": "fb-post-1", "status": "scheduled", "dueAt": due.isoformat(),
            }}}
            with patch("ytc.instagram.requests.head", return_value=head) as check, \
                    patch("ytc.instagram.requests.post", return_value=_response(success)) as http:
                saved = facebook.schedule(folder / "short.yaml", due)

            payload = http.call_args.kwargs["json"]["variables"]["input"]
            self.assertEqual(payload["channelId"], "fb-test")
            self.assertEqual(payload["metadata"], {"facebook": {"type": "reel"}})
            self.assertEqual(payload["assets"], [{"video": {"url": _url("ak001")}}])
            spec = instagram.ShortSpec.load(folder / "short.yaml")
            self.assertTrue(payload["text"].startswith(instagram.caption(spec)))
            self.assertIn("#six", payload["text"])
            self.assertTrue(payload["text"].endswith(facebook.SOCIAL_LINE))
            self.assertEqual(check.call_count, 1)
            self.assertEqual(http.call_count, 1)
            self.assertEqual(saved["id"], "fb-post-1")
            self.assertEqual(facebook.record(folder)["media_url"], _url("ak001"))

    def test_caption_says_the_voice_is_ai_only_for_synthetic_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = _reel(Path(tmp), 1, datetime(2026, 1, 20, 20, 0, tzinfo=facebook.IST))
            plain = instagram.ShortSpec.load(folder / "short.yaml")
            text = (folder / "short.yaml").read_text(encoding="utf-8")
            (folder / "short.yaml").write_text("synthetic_media: true\n" + text, encoding="utf-8")
            synthetic = instagram.ShortSpec.load(folder / "short.yaml")
        self.assertNotIn(facebook.AI_LINE, facebook.caption(plain))
        self.assertIn(facebook.AI_LINE, facebook.caption(synthetic))
        self.assertTrue(facebook.caption(synthetic).endswith(facebook.SOCIAL_LINE))

    def test_channel_posts_reads_all_pages_without_a_date_cutoff(self) -> None:
        first = {"posts": {"edges": [{"node": {"id": "one"}}],
                           "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}
        last = {"posts": {"edges": [{"node": {"id": "two"}}],
                          "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        with patch("ytc.instagram._organization", return_value="org"), \
                patch.dict(os.environ, {"IG_BUFFER_API_KEY": "test"}), \
                patch("ytc.instagram.requests.post", side_effect=[_response(first), _response(last)]) as http:
            posts = facebook.channel_posts("fb-test")

        self.assertEqual([post["id"] for post in posts], ["one", "two"])
        inputs = [call.kwargs["json"]["variables"] for call in http.call_args_list]
        self.assertEqual(inputs[0]["input"]["filter"], {"channelIds": ["fb-test"]})
        self.assertEqual([item["after"] for item in inputs], [None, "next"])

    def test_channel_posts_limits_hourly_reads_to_three_days_back_and_sixty_ahead(self) -> None:
        now = datetime(2026, 1, 20, 12, 0, tzinfo=timezone.utc)
        page = {"posts": {"edges": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        with patch("ytc.instagram._organization", return_value="org"), \
                patch("ytc.instagram._buffer", return_value=page) as read:
            self.assertEqual(facebook.channel_posts("fb-test", now), [])
        filters = read.call_args.args[1]["input"]["filter"]
        self.assertEqual(filters, {"channelIds": ["fb-test"],
                                   "startDate": (now - timedelta(days=3)).isoformat(),
                                   "endDate": (now + timedelta(days=60)).isoformat()})

    def test_backfill_dry_run_is_local_oldest_first_and_respects_queue_cap(self) -> None:
        now = datetime(2026, 1, 20, 12, 0, tzinfo=facebook.IST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for number in range(1, 13):
                _reel(root, number, now - timedelta(days=13 - number))
            with patch("ytc.facebook.channel_posts", side_effect=AssertionError("Buffer was called")), \
                    patch("ytc.instagram.requests.post", side_effect=AssertionError("HTTP was called")), \
                    patch("ytc.instagram.requests.head", side_effect=AssertionError("HTTP was called")):
                result = facebook.backfill(root, now, dry=True)

            self.assertEqual([item["reel"] for item in result["planned"]],
                             [f"ak{number:03d}" for number in range(1, 11)])
            self.assertEqual([item["facebook_at"] for item in result["planned"][:2]],
                             [now.replace(hour=19).isoformat(), now.replace(hour=21).isoformat()])
            self.assertEqual(len(result["held"]), 2)
            self.assertEqual(len(list(root.glob("ak*/facebook.json"))), 0)
            self.assertFalse((root / facebook.LIVE_START).exists())

    def test_backfill_reserves_queue_room_for_live_reels(self) -> None:
        now = datetime(2026, 1, 20, 12, 0, tzinfo=facebook.IST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for number in range(1, 9):
                _reel(root, number, now - timedelta(days=9 - number))
            for number in range(9, 13):
                _reel(root, number, now + timedelta(days=number - 8))
            with patch("ytc.facebook.channel_posts", side_effect=AssertionError("Buffer was called")):
                result = facebook.backfill(root, now, dry=True)

            self.assertEqual([item["reel"] for item in result["planned"]],
                             [f"ak{number:03d}" for number in range(1, 7)])
            self.assertEqual([item["reel"] for item in result["held"]], ["ak007", "ak008"])
            self.assertFalse((root / facebook.LIVE_START).exists())

    def test_backfill_reserves_room_only_for_reels_due_within_the_window(self) -> None:
        now = datetime(2026, 1, 20, 12, 0, tzinfo=facebook.IST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for number in range(1, 9):
                _reel(root, number, now - timedelta(days=9 - number))
            for number in range(9, 13):
                _reel(root, number, now + timedelta(days=number - 8))
            with patch("ytc.facebook.channel_posts", side_effect=AssertionError("Buffer was called")):
                result = facebook.backfill(root, now, dry=True, reserve_within=timedelta(hours=48))

            self.assertEqual([item["reel"] for item in result["planned"]],
                             [f"ak{number:03d}" for number in range(1, 9)])
            self.assertEqual(result["held"], [])

    def test_backfill_fills_the_given_slots_earliest_first_up_to_the_days_cap(self) -> None:
        now = datetime(2026, 1, 20, 12, 0, tzinfo=facebook.IST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for number in range(1, 6):
                _reel(root, number, now - timedelta(days=6 - number))
            with patch("ytc.facebook.channel_posts", side_effect=AssertionError("Buffer was called")):
                result = facebook.backfill(root, now, per_day=3, days=3, dry=True,
                                           slots=reel_cloud.CATCH_UP_SLOTS)

            tomorrow = now + timedelta(days=1)
            self.assertEqual([item["facebook_at"] for item in result["planned"]],
                             [now.replace(hour=16).isoformat(), now.replace(hour=19).isoformat(),
                              now.replace(hour=21).isoformat(), tomorrow.replace(hour=10).isoformat(),
                              tomorrow.replace(hour=16).isoformat()])
            with self.assertRaises(ValueError):
                facebook.backfill(root, now, per_day=3, dry=True)

    def test_backfill_leaves_an_instagram_reels_day_place_and_time_for_its_mirror(self) -> None:
        now = datetime(2026, 1, 20, 12, 0, tzinfo=facebook.IST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _reel(root, 1, now - timedelta(days=2))
            _reel(root, 2, now - timedelta(days=1))
            _reel(root, 3, now.replace(hour=19))
            with patch("ytc.facebook.channel_posts", side_effect=AssertionError("Buffer was called")):
                result = facebook.backfill(root, now, per_day=2, dry=True, slots=((16, 0), (19, 0)))

            self.assertEqual([item["facebook_at"] for item in result["planned"]],
                             [now.replace(hour=16).isoformat(), (now + timedelta(days=1)).replace(hour=16).isoformat()])

    def test_hourly_catch_up_leaves_buffer_alone_once_facebook_has_every_reel(self) -> None:
        now = datetime(2026, 1, 20, 12, 0, tzinfo=facebook.IST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = _reel(root, 1, now - timedelta(days=2))
            (folder / facebook.RECORD).write_text(json.dumps({"id": "fb-1", "status": "sent"}), encoding="utf-8")
            done = {"facebook": {}, "held": {}, "errors": []}
            with patch.object(reels, "REELS", root), \
                    patch("ytc.facebook.channel_posts", side_effect=AssertionError("Buffer was called")):
                reel_cloud.facebook_catch_up(now, done)
            self.assertEqual(done, {"facebook": {}, "held": {}, "errors": []})

            (folder / facebook.RECORD).unlink()
            with patch.object(reels, "REELS", root):
                reel_cloud.facebook_catch_up(now, done, dry=True)
            self.assertEqual(done["facebook"]["ak001"], "Tue 20 Jan 16:00 IST (catch-up)")

    def test_backfill_dry_run_counts_other_facebook_posts_in_daily_limit(self) -> None:
        now = datetime(2026, 1, 20, 12, 0, tzinfo=facebook.IST)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _reel(root, 1, now - timedelta(days=2))
            for number, hour in ((90, 20), (91, 20)):
                folder = root / f"ak{number:03d}"
                folder.mkdir()
                (folder / facebook.RECORD).write_text(json.dumps({
                    "id": f"fb-{number}", "status": "scheduled", "dueAt": now.replace(hour=hour).isoformat(),
                }), encoding="utf-8")
            with patch("ytc.facebook.channel_posts", side_effect=AssertionError("Buffer was called")):
                result = facebook.backfill(root, now, dry=True)
            self.assertEqual(result["planned"][0]["facebook_at"],
                             (now + timedelta(days=1)).replace(hour=19).isoformat())

    def test_backfill_adopts_existing_and_only_schedules_sent_public_reels(self) -> None:
        now = datetime(2026, 1, 20, 12, 0, tzinfo=facebook.IST)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "IG_BUFFER_API_KEY": "test", "IG_BUFFER_CHANNEL_ID": "ig-test",
            "IG_BUFFER_FACEBOOK_CHANNEL_ID": "fb-test",
        }):
            root = Path(tmp)
            folders = [_reel(root, number, now - timedelta(days=5 - number)) for number in range(1, 5)]
            facebook_post = {"id": "existing", "status": "sent", "dueAt": (now - timedelta(days=1)).isoformat(),
                             "createdAt": (now - timedelta(days=2)).isoformat(), "sentAt": now.isoformat(),
                             "externalLink": "https://www.facebook.com/reel/one",
                             "assets": [{"source": _url("ak001")}]}
            instagram_posts = [{"id": f"ig-{number}", "status": "scheduled" if number == 3 else "sent",
                                "dueAt": (now - timedelta(days=5 - number)).isoformat(),
                                "createdAt": now.isoformat(), "sentAt": (now - timedelta(days=5 - number)).isoformat(),
                                "externalLink": None if number == 3 else f"https://www.instagram.com/reel/{number}/",
                                "metadata": {"type": "post" if number == 4 else "reel"},
                                "assets": [{"source": _url(f"ak{number:03d}")}]}
                               for number in range(1, 5)]
            def posts(channel: str) -> list[dict]:
                return [facebook_post] if channel == "fb-test" else instagram_posts

            success = {"createPost": {"__typename": "PostActionSuccess", "post": {
                "id": "new-post", "status": "scheduled", "dueAt": now.replace(hour=19).isoformat(),
            }}}
            head = Mock(status_code=200, headers={"content-type": "video/mp4"})
            with patch("ytc.facebook.channel_posts", side_effect=posts), \
                    patch("ytc.instagram.requests.head", return_value=head), \
                    patch("ytc.instagram.requests.post", return_value=_response(success)) as http:
                result = facebook.backfill(root, now)
                again = facebook.backfill(root, now)

            self.assertEqual(result["adopted"], ["ak001"])
            self.assertEqual([item["reel"] for item in result["planned"]], ["ak002"])
            self.assertEqual(result["unverified"], ["ak003", "ak004"])
            self.assertEqual(again["planned"], [])
            self.assertEqual(http.call_count, 1)
            self.assertTrue(facebook.record(folders[0])["adopted"])
            self.assertEqual(facebook.record(folders[1])["id"], "new-post")
            self.assertIsNone(facebook.record(folders[2]))
            self.assertIsNone(facebook.record(folders[3]))
            self.assertFalse((root / facebook.LIVE_START).exists())

    def test_facebook_slot_uses_the_reel_slot_horizons(self) -> None:
        now = datetime(2026, 1, 20, 19, 0, tzinfo=facebook.IST)
        due = now.replace(hour=20)
        taken = {due} | {due + timedelta(days=offset) for offset in range(1, reel_cloud.COVER_DAYS)}
        per_day = Counter(slot.date() for slot in taken)
        self.assertEqual(reel_cloud.facebook_slot(due, now, taken, per_day),
                         (now + timedelta(days=1)).replace(hour=13))

        full = {now.replace(hour=hour, minute=minute) + timedelta(days=offset)
                for offset in range(reel_cloud.FULL_DAYS) for hour, minute in reel_cloud.SLOTS}
        self.assertIsNone(reel_cloud.facebook_slot(due, now, full, Counter(slot.date() for slot in full)))

    def test_hourly_mirror_keeps_instagram_slot_and_adopts_buffer_posts(self) -> None:
        now = datetime(2026, 1, 20, 12, 0, tzinfo=facebook.IST)
        due = now.replace(hour=20)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "IG_BUFFER_FACEBOOK_CHANNEL_ID": "fb-test",
        }):
            root = Path(tmp)
            folder = _reel(root, 1, due)
            facebook.live_start(root, now - timedelta(days=1))
            found = {"id": "existing", "status": "scheduled", "dueAt": due.isoformat(),
                     "createdAt": now.isoformat(), "sentAt": None, "externalLink": None,
                     "assets": [{"source": _url("ak001")}]}
            done = {"facebook": {}, "held": {}, "errors": []}
            with patch.object(reels, "REELS", root), \
                    patch("ytc.facebook.channel_posts", return_value=[found]) as read, \
                    patch("ytc.facebook.schedule", side_effect=AssertionError("duplicate post")):
                reel_cloud.facebook_mirror(now, done)
            read.assert_called_once_with("fb-test", now)
            self.assertEqual(facebook.record(folder)["id"], "existing")
            self.assertTrue(facebook.record(folder)["adopted"])
            self.assertIn("already in Buffer", done["facebook"]["ak001"])

            (folder / facebook.RECORD).unlink()
            done = {"facebook": {}, "held": {}, "errors": []}
            with patch.object(reels, "REELS", root), patch("ytc.facebook.channel_posts", return_value=[]), \
                    patch("ytc.facebook.schedule", return_value={"id": "new", "dueAt": due.isoformat()}) as send:
                reel_cloud.facebook_mirror(now, done)
            self.assertEqual(send.call_args.args[1], due)
            self.assertEqual(done["errors"], [])

    def test_page_posts_without_a_reel_leave_the_days_reel_places_free(self) -> None:
        now = datetime(2026, 1, 20, 12, 0, tzinfo=facebook.IST)
        due = now.replace(hour=20)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "IG_BUFFER_FACEBOOK_CHANNEL_ID": "fb-test",
        }):
            root = Path(tmp)
            _reel(root, 1, due)
            facebook.live_start(root, now - timedelta(days=1))
            imported = [{"id": f"page-{i}", "status": "sent", "dueAt": now.replace(hour=2 + i).isoformat(),
                         "assets": [{"source": f"https://buffer.example/{i}_thumbnail.jpg"}]} for i in range(3)]
            done = {"facebook": {}, "held": {}, "errors": []}
            with patch.object(reels, "REELS", root), patch("ytc.facebook.channel_posts", return_value=imported), \
                    patch("ytc.facebook.schedule", return_value={"id": "new", "dueAt": due.isoformat()}) as send:
                reel_cloud.facebook_mirror(now, done)
            self.assertEqual(send.call_args.args[1], due)

    def test_hourly_mirror_counts_all_facebook_posts_toward_free_cap(self) -> None:
        now = datetime(2026, 1, 20, 12, 0, tzinfo=facebook.IST)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "IG_BUFFER_FACEBOOK_CHANNEL_ID": "fb-test",
        }):
            root = Path(tmp)
            _reel(root, 1, now.replace(hour=20))
            facebook.live_start(root, now - timedelta(days=1))
            posts = [{"id": str(i), "status": "scheduled", "dueAt": now.replace(hour=21).isoformat(),
                      "assets": []} for i in range(facebook.QUEUE)]
            done = {"facebook": {}, "held": {}, "errors": []}
            with patch.object(reels, "REELS", root), patch("ytc.facebook.channel_posts", return_value=posts), \
                    patch("ytc.facebook.schedule", side_effect=AssertionError("queue exceeded")):
                reel_cloud.facebook_mirror(now, done)
            self.assertIn("room", done["held"]["ak001 (Facebook)"])

    def test_hourly_mirror_catches_up_a_reel_posted_after_activation(self) -> None:
        now = datetime(2026, 1, 25, 12, 0, tzinfo=facebook.IST)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "IG_BUFFER_FACEBOOK_CHANNEL_ID": "fb-test",
        }):
            root = Path(tmp)
            _reel(root, 1, now - timedelta(days=4))
            _reel(root, 2, now - timedelta(days=8))
            facebook.live_start(root, now - timedelta(days=5))
            done = {"facebook": {}, "held": {}, "errors": []}
            saved = {"id": "new", "dueAt": now.replace(hour=20).isoformat()}
            with patch.object(reels, "REELS", root), patch("ytc.facebook.channel_posts", return_value=[]), \
                    patch("ytc.facebook.schedule", return_value=saved) as send:
                reel_cloud.facebook_mirror(now, done)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[0].parent.name, "ak001")
            self.assertEqual(send.call_args.args[1], now.replace(hour=20))

    def test_hourly_mirror_gives_a_freed_queue_place_to_the_oldest_held_reel(self) -> None:
        now = datetime(2026, 1, 25, 12, 0, tzinfo=facebook.IST)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "IG_BUFFER_FACEBOOK_CHANNEL_ID": "fb-test",
        }):
            root = Path(tmp)
            older = _reel(root, 1, now - timedelta(days=1))
            _reel(root, 2, now.replace(hour=20))
            facebook.live_start(root, now - timedelta(days=5))
            posts = [{"id": f"other-{i}", "status": "scheduled",
                      "dueAt": (now + timedelta(days=1)).replace(hour=20).isoformat(), "assets": []}
                     for i in range(facebook.QUEUE - 1)]
            saved = {"id": "new", "dueAt": now.replace(hour=20).isoformat()}
            done = {"facebook": {}, "held": {}, "errors": []}
            with patch.object(reels, "REELS", root), patch("ytc.facebook.channel_posts", return_value=posts), \
                    patch("ytc.facebook.schedule", return_value=saved) as send:
                reel_cloud.facebook_mirror(now, done)

            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[0], older / "short.yaml")
            self.assertEqual(done["held"]["ak002 (Facebook)"], "waiting for room in the Facebook queue")
            self.assertEqual(done["errors"], [])

    def test_hourly_mirror_holds_reels_when_buffer_rate_limits_the_read(self) -> None:
        now = datetime(2026, 1, 20, 12, 0, tzinfo=facebook.IST)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "IG_BUFFER_FACEBOOK_CHANNEL_ID": "fb-test",
        }):
            root = Path(tmp)
            _reel(root, 1, now.replace(hour=20))
            facebook.live_start(root, now - timedelta(days=1))
            done = {"facebook": {}, "held": {}, "errors": []}
            with patch.object(reels, "REELS", root), \
                    patch("ytc.facebook.channel_posts", side_effect=publish.BufferBusy(now)):
                reel_cloud.facebook_mirror(now, done)
            self.assertIn("Buffer is busy", done["held"]["ak001 (Facebook)"])
            self.assertEqual(done["errors"], [])

    def test_facebook_failure_does_not_skip_youtube_steps(self) -> None:
        events = []

        def fail(*_args) -> None:
            events.append("facebook")
            raise RuntimeError("bad Facebook record")

        def fail_catch_up(*_args) -> None:
            events.append("catch-up")
            raise RuntimeError("bad Buffer history")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "IG_BUFFER_CHANNEL_ID": "ig-test", "IG_BUFFER_YOUTUBE_CHANNEL_ID": "yt-test",
            "IG_BUFFER_FACEBOOK_CHANNEL_ID": "fb-test",
        }, clear=True), patch.object(reels, "REELS", Path(tmp)), \
                patch("ytc.instagram.posts_by_reel", return_value={}), \
                patch("ytc.reel_cloud.mirror", side_effect=lambda *_: events.append("mirror") or []), \
                patch("ytc.reel_cloud.cuts", side_effect=lambda *_: events.append("cuts")), \
                patch("ytc.reel_cloud.lineup", side_effect=lambda *_: events.append("lineup")), \
                patch("ytc.reel_cloud.spread", side_effect=lambda *_: events.append("spread")), \
                patch("ytc.reel_cloud.facebook_catch_up", side_effect=fail_catch_up), \
                patch("ytc.reel_cloud.facebook_mirror", side_effect=fail), \
                self.assertLogs("ytc.reel_cloud", level="ERROR"):
            result = reel_cloud.work(dry=True)
        self.assertEqual(events, ["mirror", "cuts", "lineup", "spread", "catch-up", "facebook"])
        self.assertIn("the Facebook catch-up failed: bad Buffer history", result["errors"])
        self.assertIn("the Facebook mirror failed: bad Facebook record", result["errors"])

    def test_facebook_mirror_is_gated_when_instagram_buffer_read_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "IG_BUFFER_CHANNEL_ID": "ig-test", "IG_BUFFER_FACEBOOK_CHANNEL_ID": "fb-test",
        }, clear=True), patch.object(reels, "REELS", Path(tmp)), \
                patch("ytc.instagram.posts_by_reel", side_effect=RuntimeError("Instagram read failed")), \
                patch("ytc.reel_cloud.facebook_mirror", side_effect=AssertionError("Facebook was called")):
            result = reel_cloud.work(dry=True)
        self.assertTrue(any("Instagram read failed" in error for error in result["errors"]))

    def test_missing_facebook_channel_logs_one_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"IG_BUFFER_CHANNEL_ID": "ig-test"},
                                                               clear=True), patch.object(reels, "REELS", Path(tmp)), \
                patch("ytc.instagram.posts_by_reel", return_value={}), \
                self.assertLogs("ytc.reel_cloud", level="INFO") as logs:
            result = reel_cloud.work(dry=True)
        self.assertEqual(result["facebook"], {})
        self.assertEqual(sum("skipping the Facebook mirror" in line for line in logs.output), 1)

    def test_missing_facebook_channel_logs_one_skip_in_real_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"IG_BUFFER_CHANNEL_ID": "ig-test"},
                                                               clear=True), patch.object(reels, "REELS", Path(tmp)), \
                patch("ytc.instagram.posts_by_reel", return_value={}), \
                patch("ytc.reel_cloud.facebook_mirror", side_effect=AssertionError("Facebook was called")), \
                self.assertLogs("ytc.reel_cloud", level="INFO") as logs:
            result = reel_cloud.work(dry=False)
        self.assertEqual(result["facebook"], {})
        self.assertEqual(sum("skipping the Facebook mirror" in line for line in logs.output), 1)


if __name__ == "__main__":
    unittest.main()
