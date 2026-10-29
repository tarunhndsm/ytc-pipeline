from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from ytc import reel_art, reel_writer

ANIRUDDHA = "a slim fair prince of about twenty, thin moustache, pearl crown, blue dhoti"
BANA = "a towering thousand-armed demon king, dark skin, golden helmet"


def _script(*pictures: str, cast: list[dict] | None = None) -> dict:
    return {"cast": [{"name": "Aniruddha", "look": ANIRUDDHA}, {"name": "King Banasura", "look": BANA}]
            if cast is None else cast, "beats": [{"text": "पंक्ति", "picture": p} for p in pictures]}


def _pictures(script: dict) -> list[str]:
    return [beat["picture"] for beat in reel_writer.with_looks(script)["beats"]]


class CastLooks(unittest.TestCase):
    def test_every_picture_naming_a_figure_gets_their_look_once(self) -> None:
        first, other, again = _pictures(_script("Close-up of young prince Aniruddha sleeping on a golden couch.",
                                                "Usha points at a portrait.",
                                                "Aniruddha fights the guards; Aniruddha wins."))
        self.assertEqual(first, f"Close-up of young prince Aniruddha ({ANIRUDDHA}) sleeping on a golden couch.")
        self.assertEqual(other, "Usha points at a portrait.")
        self.assertEqual(again, f"Aniruddha ({ANIRUDDHA}) fights the guards; Aniruddha wins.")

    def test_a_look_already_in_the_picture_is_not_added_again(self) -> None:
        picture = f"Aniruddha, {ANIRUDDHA}, plays dice."
        self.assertEqual(_pictures(_script(picture)), [picture])

    def test_the_pictures_may_leave_out_a_title_the_cast_gives(self) -> None:
        self.assertEqual(_pictures(_script("Banasura binds Aniruddha with serpent cords.")),
                         [f"Banasura ({BANA}) binds Aniruddha ({ANIRUDDHA}) with serpent cords."])

    def test_not_after_a_possessive_but_after_a_later_plain_mention(self) -> None:
        self.assertEqual(_pictures(_script("Usha holds Aniruddha's portrait.",
                                           "Usha holds Aniruddha’s portrait as Aniruddha sleeps.")),
                         ["Usha holds Aniruddha's portrait.",
                          f"Usha holds Aniruddha’s portrait as Aniruddha ({ANIRUDDHA}) sleeps."])

    def test_a_look_that_names_another_figure_gets_no_look_inside_it(self) -> None:
        cast = [{"name": "Usha", "look": "a demon princess, daughter of Banasura, red silk"},
                {"name": "Banasura", "look": BANA}]
        self.assertEqual(_pictures(_script("Usha weeps.", cast=cast)),
                         ["Usha (a demon princess, daughter of Banasura, red silk) weeps."])

    def test_a_long_look_is_cut_and_a_script_without_a_cast_is_left_alone(self) -> None:
        long = " ".join(f"word{n}" for n in range(30))
        picture = _pictures(_script("Usha weeps.", cast=[{"name": "Usha", "look": long}]))[0]
        self.assertEqual(len(re.search(r"\((.*)\)", picture).group(1).split()), reel_writer.LOOK_WORDS)
        self.assertEqual(_pictures(_script("Usha weeps.", cast=[])), ["Usha weeps."])
        self.assertEqual(_pictures({"beats": [{"text": "पंक्ति", "picture": "Usha weeps."}]}), ["Usha weeps."])

    def test_the_writer_gives_the_cast_before_the_lines_and_only_for_paintings(self) -> None:
        properties = list(reel_writer.SCHEMA["properties"])
        self.assertLess(properties.index("cast"), properties.index("beats"))
        self.assertIn("cast", reel_writer._schema()["required"])
        with patch.object(reel_art, "CINEMATIC", False):
            self.assertNotIn("cast", reel_writer._schema()["properties"])
            self.assertNotIn("cast", reel_writer._schema()["required"])


if __name__ == "__main__":
    unittest.main()
