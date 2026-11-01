from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ytc.news.live import archives, briefing, encoder, policy, seo, supervisor, tts

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def story(sid="s", hours=1, **kwargs):
    return {"id": sid, "headline": "Confirmed development", "at": (NOW - timedelta(hours=hours)).isoformat(),
            **kwargs}


class LiveEditorialTests(unittest.TestCase):
    def test_stale_future_undated_and_old_weather_never_reenter_the_queue(self):
        desk = [story("old", 25), story("future", -2), story("weather", 7, label="मौसम"),
                {"id": "undated", "headline": "Old"}, story("current", 2)]
        self.assertEqual([s["id"] for s in policy.select(desk, [], NOW)], ["current"])
        self.assertEqual(policy.select([story("wire", 13, wire=True, verified=True, script="read")], [], NOW), [])

    def test_raw_wire_is_attributed_ticker_material_only(self):
        wire = [story("raw", wire=True), story("checked", wire=True, verified=True, script="original context")]
        self.assertEqual([s["id"] for s in policy.select([], wire, NOW)], ["checked"])
        self.assertEqual(len(policy.select([], wire, NOW, narrated=False)), 2)

    def test_a_corrected_version_changes_revision_even_with_the_same_id(self):
        first = story()
        corrected = {**first, "headline": "Corrected development"}
        self.assertNotEqual(policy.revision(first), policy.revision(corrected))
        self.assertEqual(policy.revision(first), policy.revision({**first, "views": 999999}))

    def test_fresh_development_leads_over_old_lifetime_hit(self):
        old = story("old", 20, views=1000000)
        new = story("new", 0.1, views=0)
        self.assertEqual(policy.select([old, new], [], NOW)[0]["id"], "new")

    def test_missing_or_fabricated_evidence_verdicts_are_rejected(self):
        articles = [{"text": "The council started a bus service on Monday morning."}]
        answer = {"claims": [{"n": 1, "supported": True, "source": 0,
                              "quote": "started a bus service on Monday"}]}
        self.assertTrue(briefing.supported(answer, ["claim"], articles))
        for field, value in (("quote", "A quote the article never stated"), ("source", 1), ("supported", "true")):
            bad = copy.deepcopy(answer)
            bad["claims"][0][field] = value
            self.assertFalse(briefing.supported(bad, ["claim"], articles))
        self.assertFalse(briefing.supported(answer, ["one", "two"], articles))

    def test_bilingual_bulletin_keeps_sentence_boundaries_and_exact_context(self):
        hi = ("नगर निगम ने नई बस सेवा शुरू की। इस रूट पर पहले केवल निजी बसें चलती थीं। "
              "अब सरकारी बसें स्टेशन से अस्पताल तक चलेंगी। निगम के मुताबिक सेवा सोमवार से उपलब्ध है। "
              "यात्रियों को सुबह छह बजे पहली बस मिलेगी। हर बस पर रूट का बोर्ड भी रहेगा।")
        en = ("The city council has started a new bus service. Previously only private buses operated on this route. "
              "The new buses will connect the station and the hospital. The council says service begins on Monday. "
              "The first bus will leave at six in the morning.")
        draft = {"publish": True,
                 "hi": {"headline": "नई बस सेवा", "hook": "बस सेवा", "script": hi,
                        "context": "इस रूट पर पहले केवल निजी बसें चलती थीं।"},
                 "en": {"headline": "New bus service", "hook": "Bus service", "script": en,
                        "context": "Previously only private buses operated on this route."}}
        self.assertIn("अब सरकारी बसें स्टेशन से अस्पताल तक चलेंगी।", briefing.claims(draft))
        self.assertIn("The first bus will leave at six in the morning.", briefing.claims(draft))
        draft["hi"]["context"] = "यह संदर्भ स्रोत या स्क्रिप्ट में मौजूद नहीं है।"
        with self.assertRaises(ValueError):
            briefing.claims(draft)

    def test_quoted_support_allows_hindi_punctuation_but_preserves_words(self):
        evidence = [{"title": "रूट पर नई बस सेवा शुरू", "text": "नगर निगम ने नई बस सेवा शुरू की है."}]
        answer = {"claims": [{"n": 1, "supported": True, "source": 0, "quote": "नई बस सेवा शुरू की है।"}]}
        self.assertTrue(briefing.supported(answer, ["claim"], evidence))
        answer["claims"][0]["quote"] = "नई ट्रेन सेवा शुरू की है।"
        self.assertFalse(briefing.supported(answer, ["claim"], evidence))
        self.assertEqual(briefing.source_key("https://www.aajtak.in/a"),
                         briefing.source_key("https://www.indiatoday.in/b"))

    def test_no_additional_stream_is_admitted_without_cpu_and_memory_room(self):
        self.assertIn("insufficient CPU headroom", supervisor.capacity(2, 10, 2000, 0, 3, 6))
        self.assertIn("insufficient memory headroom", supervisor.capacity(8, 2, 600, 60, 3, 6))
        self.assertEqual(supervisor.capacity(8, 2, 6000, 60, 3, 6), [])
        self.assertIn("configured stream limit reached", supervisor.capacity(2, 1, 4000, 50, 3, 3))

    def test_encoder_limits_buffer_and_emits_two_second_keyframes(self):
        cmd = encoder.ffmpeg_cmd("test.flv", 8, 3000, "ultrafast", output_fps=20)
        self.assertNotIn("1024", cmd)
        self.assertIn("fps=20,format=yuv420p", cmd)
        self.assertEqual(cmd[cmd.index("-g") + 1], "40")
        self.assertIn("zerolatency", cmd)
        with self.assertRaises(ValueError):
            encoder.ffmpeg_cmd("test.flv", 8, 3000, "ultrafast", output_fps=100)

    def test_current_bulletin_overtakes_old_voice_backlog_and_expired_jobs_leave(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tts, "STATE", Path(tmp)), \
             patch.object(tts, "QUEUE", Path(tmp) / "queue"):
            tts.request("voice_local", "lead", "A confirmed current development.", "en", "male", 1.0, 6)
            original = next(tts.QUEUE.glob("*.json"))
            tts.request("voice_local", "lead", "A confirmed current development.", "en", "male", 1.0, 2)
            promoted = list(tts.QUEUE.glob("*.json"))
            self.assertEqual(len(promoted), 1)
            self.assertFalse(original.exists())
            self.assertTrue(promoted[0].name.startswith("2_"))
            tts.request("voice_en_local", "old", "An expired headline.", "en", "male", 1.0, 6)
            expired = next(p for p in tts.QUEUE.glob("*.json") if "__old.json" in p.name)
            value = json.loads(expired.read_text()); value["at"] = 1
            expired.write_text(json.dumps(value))
            self.assertEqual(tts.pending(), promoted)
            self.assertFalse(expired.exists())

    def test_live_title_can_use_the_current_bulletin_without_a_short_keyword(self):
        title = seo.news_title([story(headline="A current development", hook="Current development")])
        self.assertIn("Current development", title)
        self.assertLessEqual(len(title), 100)

    def test_archive_switch_is_prepared_before_old_live_ends_and_is_recoverable(self):
        class Google:
            def __init__(self):
                self.events = []
                self.new_status = "created"
                self.old_status = "live"
            def get(self, resource, units, **params):
                old = params["id"] == "old"
                return {"items": [{"id": params["id"], "status": {
                    "lifeCycleStatus": self.old_status if old else self.new_status},
                    "snippet": {"title": "News", "actualStartTime": (NOW - timedelta(hours=11)).isoformat()},
                    "contentDetails": {"boundStreamId": "stream" if old else ""}}]}
            def post(self, resource, units, body=None, **params):
                self.events.append((resource, params))
                if resource == "liveBroadcasts":
                    self.body = body
                    return {"id": "new"}
                if params.get("broadcastStatus") == "complete":
                    self.old_status = "complete"
                return {}
        api = Google()
        session = archives.Sessions(api, save=lambda: None)
        session.saved = {}
        social = {"broadcasts": {"news": "old"}}
        settings = {"archive_sessions": True, "session_hours": 10}
        self.assertFalse(session.step(settings, social, NOW))  # create
        self.assertFalse(session.step(settings, social, NOW))  # bind and test
        self.assertEqual(api.old_status, "live")
        self.assertEqual(social["broadcasts"]["news"], "old")
        self.assertTrue(api.body["contentDetails"]["recordFromStart"])
        self.assertEqual(api.body["status"]["privacyStatus"], "public")
        api.new_status = "testing"
        self.assertFalse(session.step(settings, social, NOW))
        self.assertEqual(api.old_status, "complete")
        self.assertEqual(social["broadcasts"]["news"], "old")
        api.new_status = "live"
        self.assertTrue(session.step(settings, social, NOW))
        self.assertEqual(social["broadcasts"]["news"], "new")
        self.assertIn("pending", session.saved)  # safe to recover before the config write succeeds
        session.acknowledge(NOW)
        self.assertNotIn("pending", session.saved)

    def test_archive_scheduler_preserves_continuous_mode_and_owner_changes(self):
        session = archives.Sessions(None, save=lambda: None)
        session.saved = {}
        self.assertFalse(session.step({}, {}, NOW))
        with self.assertRaises(ValueError):
            session.step({"archive_sessions": True, "session_hours": 12}, {}, NOW)
        session.saved["pending"] = {"channel": "news", "old": "old", "new": "new"}
        with self.assertRaises(ValueError):
            session.step({"archive_sessions": True}, {"broadcasts": {"news": "owner-changed"}}, NOW)


if __name__ == "__main__":
    unittest.main()
