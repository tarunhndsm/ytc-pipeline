from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ytc import reel_pictures, reel_writer

PRADYUMNA = 'Pradyumna and Samvara fight with maces, from the "Large Basohli Bhagavata Purana"'
DARBAR = "Rama and Sita enthroned with Lakshmana and Hanuman attending, from a Kalighat album"
TITLES = {"a": PRADYUMNA, "b": DARBAR, "c": "Portrait of a Princess", "d": "Krishna and Balarama"}


def _hit(ref: str) -> dict:
    return {"ref": ref, "title": TITLES[ref], "url": f"https://example.org/{ref}.jpg", "source": "The Met",
            "license": "CC0"}


def _framed(ref: str) -> dict:
    return _hit(ref) | {"size": (2000, 3000), "subject": [0.2, 0.2, 0.8, 0.8], "faces": [], "kind": "painting",
                        "hindi": "एक पुराना चित्र"}


class FiguresTests(unittest.TestCase):
    """The people of the epics a title names, however it spells them."""

    def test_names(self) -> None:
        self.assertEqual(reel_pictures.figures(PRADYUMNA), {"pradyumna", "sambara"})
        self.assertEqual(reel_pictures.figures(DARBAR), {"rama", "sita", "lakshmana", "hanuman"})
        self.assertEqual(reel_pictures.figures("Kṛṣṇa and Rādhā"), {"krishna", "radha"})
        self.assertEqual(reel_pictures.figures("File:Hanuman_carrying_the_mountain.jpg"), {"hanuman"})
        self.assertEqual(reel_pictures.figures("Ravana's court"), {"ravana"})

    def test_a_longer_name_is_not_read_as_a_shorter_one(self) -> None:
        self.assertEqual(reel_pictures.figures("Bala Rama and Parashu-rama"), {"balarama", "parashurama"})
        self.assertEqual(reel_pictures.figures("Kumbha Karna awakened"), {"kumbhakarna"})
        self.assertEqual(reel_pictures.figures("A sambar deer"), set())

    def test_a_book_names_no_one(self) -> None:
        self.assertEqual(reel_pictures.figures("A page from a Ramayana; a scene from the Maha-bharata"), set())
        self.assertEqual(reel_pictures.figures("Vishnu Battles Madhu and Kaitabha, from a Markandeya Purana"), set())
        self.assertEqual(reel_pictures.figures("Vishnu from Gita Govinda.jpg"), set())
        self.assertEqual(reel_pictures.figures(
            "Anasuya feeding the Hindu Trinity, The Krishna-Sudama Temple of Porbandar, India.JPG"), set())
        self.assertEqual(reel_pictures.figures("Krishna and Radha, Lakshmana Temple, Khajuraho"), {"krishna", "radha"})
        self.assertEqual(reel_pictures.figures("Rama's brothers Bharata and Shatrughna set out from Ayodhya"),
                         {"rama", "bharata", "shatrughna"})

    def test_hindi(self) -> None:
        self.assertEqual(reel_pictures.figures("प्रभु श्री राम और सीता जी"), {"rama", "sita"})
        self.assertEqual(reel_pictures.figures("श्रीराम ने हनुमानजी को देखा।"), {"rama", "hanuman"})
        self.assertEqual(reel_pictures.figures("परशुराम और बलराम"), {"parashurama", "balarama"})
        self.assertEqual(reel_pictures.figures("रामायण की कथा, आने वाली रात"), set())


class OtherFaithTests(unittest.TestCase):
    """Titles that never reach the models, from ak068's first try (20 Oct)."""

    def test_left_out(self) -> None:
        for title in ("Painting of the funeral of Maharaja Ranjit Singh, ca.1840.png",
                      "Four Rajput wives of Ranjit Singh perform sati, detail from a painting of the funeral",
                      "Abbas II of Persia and the Mughal ambassador.jpeg",
                      "Basreliëf in de muur aan de oostzijde van de Borobudur Idem 2e gaanderij"):
            self.assertTrue(reel_pictures.other_faith(title), title)

    def test_kept(self) -> None:
        for title in (DARBAR, "Maharaja Sawai Madho Singh of Jaipur in durbar", "King Shibi weighs his flesh"):
            self.assertFalse(reel_pictures.other_faith(title), title)


class OtherStoryTests(unittest.TestCase):
    """A picture of another story's people is one whose title names some, and none of this story's."""

    def test_ak066(self) -> None:
        named = reel_pictures.figures("Sita Gives Hanuman the Choodamani (Valmiki Ramayana, Sundara Kanda 36-38)")
        self.assertTrue(reel_pictures.other_story(PRADYUMNA, named))
        self.assertTrue(reel_pictures.other_story("Krishna and Balarama", named))
        self.assertFalse(reel_pictures.other_story(DARBAR, named))
        self.assertFalse(reel_pictures.other_story("Rama, Lakshmana, and Hanuman", named))
        self.assertFalse(reel_pictures.other_story("Portrait of a Princess", named))

    def test_the_story_names_people_in_its_hindi_too(self) -> None:
        draft = {"row": {"story": "Bharadwaja's miraculous hospitality to Bharata's army",
                         "source": "Valmiki Ramayana, Ayodhya Kanda 91-93"},
                 "script": {"summary": "जब भरत जी प्रभु श्री राम को वापस अयोध्या लाने के लिए निकले",
                            "beats": [{"text": "पूरी सेना आश्रम पहुँची", "search": ["hermitage"], "picture": "an ashram",
                                       "role": "story"}]}}
        named = reel_pictures.story_figures(draft)
        self.assertLessEqual({"bharadvaja", "bharata", "rama"}, named)
        self.assertFalse(reel_pictures.other_story("Rama, Sita, and Lakshmana in the forest", named))
        self.assertTrue(reel_pictures.other_story("Yashoda and Gopal", named))


class PictureStepTests(unittest.TestCase):
    """The picture step offers the models no picture of another story's people, nor keeps one from a failed try."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "ak999"
        (self.folder / "work").mkdir(parents=True)
        self.beats = [
            {"text": "पंक्ति 1", "search": ["Hanuman", "Pahari painting"], "picture": "Hanuman flying over the ocean",
             "role": "story"},
            {"text": "पंक्ति 2", "search": ["Ashoka Vatika", "Mewar painting"], "picture": "Sita under the Ashoka tree",
             "role": "story"}]
        (self.folder / reel_writer.SCRIPT).write_text(json.dumps({
            "id": "ak999", "row": {"story": "Sita Gives Hanuman the Choodamani",
                                   "source": "Valmiki Ramayana, Sundara Kanda 36-38"},
            "script": {"beats": self.beats}}), encoding="utf-8")
        self.offered: set[str] = set()
        self.asked: list[list[int]] = []
        self.saved: dict = {}
        for patcher in (
                patch.object(reel_pictures, "candidates",
                             side_effect=lambda beats: {1: [_hit(r) for r in "abd"], 2: [_hit(r) for r in "dcb"]}),
                patch.object(reel_pictures, "shortlist", side_effect=self._shortlist),
                patch.object(reel_pictures, "choose", side_effect=self._choose),
                patch.object(reel_pictures, "frame_all",
                             side_effect=lambda beats, ranked, shown, purpose: {
                                 n: _framed(hits[0]["ref"]) for n, hits in ranked.items() if hits}),
                patch.object(reel_writer, "spec_fields",
                             side_effect=lambda draft: {"beats": [{} for _ in self.beats], "captions": {},
                                                        "description": "about\n"}),
                patch.object(reel_pictures, "save", side_effect=self._save)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _shortlist(self, beats, per_line):
        self.offered |= {hit["ref"] for hits in per_line.values() for hit in hits}
        return per_line

    def _choose(self, beats, shown, story, purpose):
        self.asked.append(sorted(shown))
        return {n: list(hits) for n, hits in shown.items()}

    def _save(self, folder, spec, chosen, spares):
        self.saved = {n: hit["ref"] for n, hit in chosen.items()}
        return folder / "short.yaml"

    def test_other_stories_people_are_left_out(self) -> None:
        reel_pictures.picture(self.folder)
        self.assertEqual(self.offered, {"b", "c"})
        self.assertEqual(self.saved, {1: "b", 2: "c"})

    def test_a_kept_picture_of_another_story_is_chosen_again(self) -> None:
        (self.folder / reel_pictures.KEPT).write_text(json.dumps({
            "1": _framed("b") | {"size": [2000, 3000], "words": "पंक्ति 1"},
            "2": _framed("d") | {"size": [2000, 3000], "words": "पंक्ति 2"}}, ensure_ascii=False), encoding="utf-8")
        reel_pictures.picture(self.folder)
        self.assertEqual(self.asked, [[2]])
        self.assertEqual(self.saved, {1: "b", 2: "c"})


if __name__ == "__main__":
    unittest.main()
