from __future__ import annotations

import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from ytc.news import editor, footage, make, originality, publish, render

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
SCRIPT = ("नगर निगम ने नई बस सेवा शुरू की। इस रूट पर पहले केवल निजी बसें चलती थीं। "
          "अब सरकारी बसें स्टेशन से अस्पताल तक चलेंगी। निगम के मुताबिक सेवा सोमवार से उपलब्ध है। "
          "यात्रियों को सुबह छह बजे पहली बस मिलेगी। हर बस पर रूट का बोर्ड भी रहेगा।")
CONTEXT = "इस रूट पर पहले केवल निजी बसें चलती थीं।"


def package():
    return {"format": "explainer", "context": CONTEXT, "script": SCRIPT, "headline": "नगर में नई बस सेवा",
            "hook": "नई बस सेवा", "title": "नगर में नई बस सेवा शुरू", "description": "निगम की बस सेवा का संदर्भ।",
            "label": "देश", "facts": [{"text": "नई बस सेवा"}, {"text": "स्टेशन से अस्पताल"}],
            "publish": True, "verified": True}


class NewsFormatTests(unittest.TestCase):
    def test_context_is_required_beyond_the_opening_sentence(self):
        self.assertEqual(make.check(package()), [])
        for context in ("", "नगर निगम ने नई बस सेवा शुरू की।", "स्रोत में न दी गई अलग जानकारी यहाँ लिखी गई है।"):
            p = package()
            p["context"] = context
            self.assertTrue(any("context" in issue for issue in make.check(p)))

    def test_questions_and_short_display_claims_are_fact_checked(self):
        p = package()
        p.update(script=SCRIPT + " क्या किराया अब मुफ्त होगा?", location="नया शहर")
        claims = editor.claims(p)
        self.assertIn("क्या किराया अब मुफ्त होगा?", claims)
        self.assertIn(CONTEXT, claims)
        self.assertIn("नई बस सेवा", claims)
        self.assertIn("नया शहर", claims)

    def test_period_ending_hindi_is_checked_sentence_by_sentence(self):
        p = package()
        p.update(script=SCRIPT.replace("।", "."), context=CONTEXT.replace("।", "."))
        self.assertEqual(make.check(p), [])
        self.assertIn("अब सरकारी बसें स्टेशन से अस्पताल तक चलेंगी.", editor.claims(p))
        self.assertIsNone(make.salvage(p, [{"claim": p["context"]}]))
        p["script"] += " निगम ने सोमवार को नई समय सारिणी भी जारी की."
        optional = "यात्रियों को सुबह छह बजे पहली बस मिलेगी."
        kept = make.salvage(p, [{"claim": optional}])
        self.assertIsNotNone(kept)
        self.assertNotIn(optional, kept["script"])

    def test_string_false_is_not_an_accepted_fact_check(self):
        failed = editor._failed(["a claim"], {"claims": [{"n": 1, "supported": "false", "quote": "a claim"}]}, [])
        self.assertEqual(len(failed), 1)

    def test_required_context_cannot_be_salvaged_out(self):
        p = package()
        self.assertIsNone(make.salvage(p, [{"claim": CONTEXT}]))

    def test_hindi_word_marks_stay_inside_words(self):
        self.assertEqual(originality._phrases("नई सरकारी बसें सोमवार सुबह चलेंगी"), {
            ("नई", "सरकारी", "बसें", "सोमवार", "सुबह"), ("सरकारी", "बसें", "सोमवार", "सुबह", "चलेंगी")})

    def test_copy_and_cross_topic_repeat_are_blocked_but_a_new_story_passes(self):
        self.assertEqual(originality.copied_from(SCRIPT, {"articles": [{"outlet": "source", "text": SCRIPT}]}),
                         "source")
        earlier = {"id": "previous", "topic": "different-topic", "script": SCRIPT,
                   "at": (NOW - timedelta(hours=6)).isoformat()}
        self.assertEqual(originality.duplicate(SCRIPT, [earlier], NOW), "previous")
        self.assertIsNone(originality.duplicate("अलग " * 50, [earlier], NOW))
        earlier["at"] = (NOW - timedelta(hours=80)).isoformat()
        self.assertIsNone(originality.duplicate(SCRIPT, [earlier], NOW))

    def test_punctuation_changes_do_not_disguise_a_copied_hindi_script(self):
        reporting = {"articles": [{"outlet": "source", "text": SCRIPT}]}
        self.assertEqual(originality.copied_from(SCRIPT.replace("।", "."), reporting), "source")

    def test_moment_never_substitutes_photos_for_event_footage(self):
        p = package()
        p["format"] = "moment"
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(editor, "reporting", return_value={"articles": []}), \
             patch.object(editor, "write", return_value=p), patch.object(editor, "verify", return_value=[]), \
             patch.object(make.articles, "for_story", return_value=[]), \
             patch.object(make.stories, "headline_sample", return_value=[]), \
             patch.object(make.pictures, "for_story", return_value=[{"subject": "bus", "title": "licensed photo"}]), \
             patch.object(make, "_footage", return_value=[]), patch.object(make.voice, "narrate") as voice:
            result = make.produce({"items": [], "developments": []}, "bus", NOW, Path(tmp))
        self.assertIn("needs licensed footage", result["skipped"])
        voice.assert_not_called()

    def test_vision_requests_do_not_use_text_only_gemma(self):
        prepared = [{"video": {"title": "event", "channel": "owner"}, "shots": [(0, 4)],
                     "frames": [Image.new("RGB", (128, 72), "red")]}]
        with patch.object(footage.gemini, "text_json", return_value={"frames": [{"n": 1}], "_model": "vision"}) as call:
            self.assertEqual(len(footage.first_look(prepared, {"headline": "event"})), 1)
        self.assertTrue(call.call_args.kwargs["media"])
        self.assertTrue(all(model.startswith("gemini-") for model in call.call_args.kwargs["models"]))
        self.assertEqual(footage.refusal({"licence": "youtube"}), "not CC BY")

    def test_tail_keeps_the_last_picture_without_a_cta_blackout(self):
        plan = render.scenes([], [], render.VO_START, 3.0, [], [{"seconds": 4.0}])
        self.assertEqual(plan[-1]["t1"], 3.0)
        self.assertIn("clip", plan[-1])
        self.assertLess(render.VO_START, 0.1)
        self.assertLess(render.TAIL, 0.3)
        self.assertEqual(footage.MEDIA, render.MEDIA)

    def test_description_keeps_source_links_and_licence_when_lead_is_long(self):
        p = package()
        p.update(description="खबर का विस्तार " * 2000,
                 source_links=[{"outlet": "source", "url": "https://example.org/report"}])
        result = publish.description(p, ["Footage: Owner, CC BY 3.0, cropped and narrated"])
        self.assertLessEqual(len(result.encode()), publish.DESCRIPTION_BYTES)
        self.assertIn("https://example.org/report", result)
        self.assertIn("CC BY 3.0", result)
        with self.assertRaises(ValueError):
            publish.description(p, ["अत्यंत लंबा क्रेडिट " * 2000])

    def test_all_retained_ids_refresh_and_one_day_counts_are_age_matched(self):
        rows = [{"id": str(i), "at": (NOW - timedelta(hours=25 if i % 2 else 4)).isoformat(),
                 "publish": {"youtube_url": f"https://youtube.com/shorts/vid{i:08d}"}, "views": 5}
                for i in range(63)]
        api = MagicMock()
        seen = []
        def response(**kwargs):
            ids = kwargs["id"].split(",")
            seen.append(ids)
            value = MagicMock()
            value.execute.return_value = {"items": [{"id": vid, "statistics": {
                "viewCount": "123", "likeCount": "7", "commentCount": "2"}} for vid in ids if vid != "vid00000062"]}
            return value
        api.videos.return_value.list.side_effect = response
        with patch.dict(os.environ, {"NEWS_YOUTUBE_TOKEN": "{}"}), \
             patch("google.oauth2.credentials.Credentials.from_authorized_user_info") as creds, \
             patch("googleapiclient.discovery.build", return_value=api):
            creds.return_value.valid = True
            self.assertEqual(publish.record_views(rows, NOW), 62)
        self.assertEqual([len(batch) for batch in seen], [50, 13])
        self.assertEqual(rows[-1]["views"], 5)  # missing videos retain their last observation
        self.assertEqual(rows[1]["views_24h"], 123)
        self.assertNotIn("views_24h", rows[0])
        self.assertEqual((rows[0]["likes"], rows[0]["comments"]), (7, 2))


if __name__ == "__main__":
    unittest.main()
