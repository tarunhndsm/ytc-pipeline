from __future__ import annotations

import unittest

from ytc import reel_pictures


class ChooseOrderTests(unittest.TestCase):
    """The chooser ranks a scene-setting picture above the line's figures caught in another episode."""

    def setUp(self) -> None:
        self.prompt = " ".join(reel_pictures.CHOOSE_PROMPT.split())

    def test_order(self) -> None:
        places = [self.prompt.index(words) for words in (
            "this very moment", "a portrait or icon of the deity or person the line names",
            "a picture that sets the scene", "last, the same deity, person or place in another scene")]
        self.assertEqual(places, sorted(places))

    def test_another_episode_only_without_contradiction(self) -> None:
        self.assertIn("only when nothing in it (an action, a weapon, a battle, a mood) contradicts the line",
                      self.prompt)
        self.assertIn("never a fight or a killing on a line about worship", self.prompt)

    def test_still_formats(self) -> None:
        self.assertIn("The story: Narada in Dvaraka", reel_pictures.CHOOSE_PROMPT.format(story="Narada in Dvaraka"))


if __name__ == "__main__":
    unittest.main()
