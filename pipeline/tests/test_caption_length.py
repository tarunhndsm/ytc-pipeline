from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from ytc import instagram, reel_pictures
from ytc.spec import ShortSpec

TAGS = ["#one", "#two", "#three", "#four", "#five"]


def _spec(description: str) -> ShortSpec:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "short.yaml"
        path.write_text(yaml.safe_dump({"id": "ak999", "title": "एक कथा", "description": description,
                                        "hashtags": TAGS, "beats": [{"text": "एक पंक्ति।"}]}, allow_unicode=True),
                        encoding="utf-8")
        return ShortSpec.load(path)


class CaptionLengthTests(unittest.TestCase):
    """Buffer turns down an Instagram caption over 2,196 characters (ak072)."""

    def test_a_story_too_long_for_buffer_ends_at_an_earlier_sentence_and_the_credits_and_tags_stay(self) -> None:
        story = "कथा का एक लंबा वाक्य यहाँ है। " * 90
        text = instagram.caption(_spec(f"{story.strip()}\nसीख की पंक्ति।\n\nस्रोत: ग्रंथ\nचित्र: एक चित्र, The Met"))
        self.assertLessEqual(instagram._units(text), instagram.CAPTION_CHARS)
        self.assertTrue(text.endswith(" ".join(TAGS)))
        self.assertIn("सीख की पंक्ति।\n\nस्रोत: ग्रंथ\nचित्र: एक चित्र, The Met", text)
        self.assertIn("यहाँ है।\nसीख की पंक्ति।", text)

    def test_a_caption_that_fits_is_left_as_it_is(self) -> None:
        description = "छोटी कथा।\nसीख।\n\nस्रोत: ग्रंथ\nचित्र: एक चित्र, The Met"
        self.assertEqual(instagram.caption(_spec(description)),
                         "\n\n".join(("एक कथा", description, instagram.FOLLOW_LINE, " ".join(TAGS))))

    def test_an_emoji_counts_as_two(self) -> None:
        self.assertEqual(instagram._units("कथा 🙏"), 6)
        self.assertEqual(instagram._cut("कथा 🙏🙏", 6), "कथा 🙏")
        self.assertEqual(instagram._cut("कथा 🙏", 5), "कथा ")


class CreditTests(unittest.TestCase):
    def test_a_credit_is_one_line_whatever_line_breaks_the_artist_holds(self) -> None:
        hit = {"hindi": "रथ पर कृष्ण", "kind": "painting", "license": "Public domain", "source": "Wikimedia Commons",
               "artist": "Udaipur Style\n\nRajasthan School"}
        self.assertEqual(reel_pictures.credit_lines([hit]),
                         "चित्र: रथ पर कृष्ण, Udaipur Style Rajasthan School (सभी सार्वजनिक डोमेन या CC0)")

    def test_new_credits_replace_everything_after_the_sources_line(self) -> None:
        description = ("कथा।\nसीख।\n\nस्रोत: ग्रंथ\n\nRajasthan School; पुराना चित्र, The Met\n"
                       "चित्र: पुराना, Udaipur Style \n\nRajasthan School; बचा हुआ, The Met\nAI से बने चित्र: पुराना")
        hit = {"hindi": "नया चित्र", "kind": "painting", "license": "CC0", "source": "The Met"}
        self.assertEqual(reel_pictures.with_credits(description, [hit]),
                         "कथा।\nसीख।\n\nस्रोत: ग्रंथ\nचित्र: नया चित्र, The Met (सभी सार्वजनिक डोमेन या CC0)")


if __name__ == "__main__":
    unittest.main()
