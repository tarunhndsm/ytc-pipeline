from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import yaml

from ytc import reel_writer, reels
from ytc.growth import brain


def _made(root: Path, reel: str, quiz: bool = False, answers: str = "", withdrawn: bool = False) -> None:
    folder = root / reel
    folder.mkdir()
    spec: dict = {"id": reel}
    if quiz:
        spec["quiz"] = {"question": f"{reel} का सवाल?", "answer": "जवाब"}
    if answers:
        spec["answers"] = {"reel": answers, "question": f"{answers} का सवाल?", "answer": "जवाब"}
    (folder / "short.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    (folder / "instagram.json").write_text(json.dumps({"dueAt": "2026-10-19T02:00:00Z", "withdrawn": withdrawn}),
                                           encoding="utf-8")


def _script(quiz: bool) -> dict:
    beats = [{"text": f"पंक्ति {n}", "emphasis": "पंक्ति", "sfx": "none", "picture": "p", "search": ["s"]}
             for n in range(1, 11)]
    return {"beats": beats, **({"quiz": {"question": "नया सवाल?", "answer": "जवाब"}} if quiz else {})}


class QuizOffTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for patcher in (patch.object(reels, "REELS", self.root), patch.object(reel_writer, "QUIZ", False)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_only_the_first_reel_after_the_last_question_answers_it(self) -> None:
        _made(self.root, "ak060", quiz=True, answers="ak059")
        _made(self.root, "ak061", quiz=True, answers="ak060")
        self.assertEqual(reel_writer.asked_before("ak062")[0].name, "ak061")
        _made(self.root, "ak062", answers="ak061")
        self.assertIsNone(reel_writer.asked_before("ak063"))
        _made(self.root, "ak063")
        self.assertIsNone(reel_writer.asked_before("ak064"))

    def test_the_next_reel_answers_when_the_answering_one_was_taken_back(self) -> None:
        _made(self.root, "ak061", quiz=True)
        _made(self.root, "ak062", answers="ak061", withdrawn=True)
        self.assertEqual(reel_writer.asked_before("ak063")[0].name, "ak061")

    def test_with_the_quiz_on_each_reel_answers_the_one_before(self) -> None:
        _made(self.root, "ak060", quiz=True)
        _made(self.root, "ak061", quiz=True, answers="ak060")
        with patch.object(reel_writer, "QUIZ", True):
            self.assertEqual(reel_writer.asked_before("ak062")[0].name, "ak061")

    def test_the_last_answer_comes_after_the_story_and_nothing_is_asked(self) -> None:
        before = (self.root / "ak061", {"quiz": {"question": "पुराना सवाल?", "answer": "जवाब"}})
        roles = [beat["role"] for beat in reel_writer.chained(_script(quiz=False), before, None)["beats"]]
        self.assertEqual(roles, ["story"] * 10 + ["answer"])
        roles = [beat["role"] for beat in reel_writer.chained(_script(quiz=False), None, None)["beats"]]
        self.assertEqual(roles, ["story"] * 10)

    def test_with_the_quiz_on_the_answer_follows_the_hook_and_the_question_ends_it(self) -> None:
        before = (self.root / "ak061", {"quiz": {"question": "पुराना सवाल?", "answer": "जवाब"}})
        with patch.object(reel_writer, "QUIZ", True):
            roles = [beat["role"] for beat in reel_writer.chained(_script(quiz=True), before, None)["beats"]]
        self.assertEqual(roles, ["story", "answer"] + ["story"] * 9 + ["quiz"])

    def test_a_script_written_with_the_quiz_ends_on_its_story_when_resumed(self) -> None:
        folder = self.root / "ak062"
        (folder / "work").mkdir(parents=True)
        before = (self.root / "ak061", {"quiz": {"question": "पुराना सवाल?", "answer": "जवाब"}})
        with patch.object(reel_writer, "QUIZ", True):
            script = reel_writer.chained(_script(quiz=True), before, None)
        (folder / reel_writer.SCRIPT).write_text(json.dumps({"id": "ak062", "script": script}), encoding="utf-8")
        self.assertTrue(reel_writer.end_on_story(folder))
        saved = json.loads((folder / reel_writer.SCRIPT).read_text(encoding="utf-8"))["script"]
        self.assertEqual([beat["role"] for beat in saved["beats"]], ["story"] * 10 + ["answer"])
        self.assertEqual([beat["text"] for beat in saved["beats"][:10]], [f"पंक्ति {n}" for n in range(1, 11)])
        self.assertNotIn("quiz", saved)
        self.assertFalse(reel_writer.end_on_story(folder))

    def test_the_writer_is_asked_for_no_question(self) -> None:
        self.assertNotIn("quiz", reel_writer._schema()["properties"])
        self.assertNotIn("quiz", reel_writer._schema()["required"])
        around = reel_writer._around(None, {"series": "S", "number": 1, "of": 2})
        self.assertNotIn("सवाल", around)
        self.assertNotIn("कमेंट", around)
        with patch.object(reel_writer, "QUIZ", True):
            self.assertIs(reel_writer._schema(), reel_writer.SCHEMA)
            self.assertIn("कमेंट", reel_writer._around(None, None))

    def test_the_judge_is_told_a_reel_ends_on_its_story(self) -> None:
        brief = reels.REVIEW_PROMPT.format(previous="", quiz=reels.NO_QUIZ_BRIEF)
        self.assertIn("no longer asks its viewers a question", brief)
        self.assertNotIn("shout-out", brief)

    def test_the_growth_desk_drops_writer_notes_that_bring_back_the_quiz(self) -> None:
        notes = ["End every Reel on a quiz question about the story", "Ask viewers to answer in the comments",
                 "Open on the figure line 1 names, as in रावण का असली नाम"]
        kept, dropped = brain.guard({"writer_notes": notes}, [], date(2026, 10, 20))
        self.assertEqual(kept["writer_notes"], notes[2:])
        self.assertEqual(len([d for d in dropped if "stopped the quiz" in d]), 2)


if __name__ == "__main__":
    unittest.main()
