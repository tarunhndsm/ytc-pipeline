from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from ytc import llm, reel_qa, reel_writer

SPENT = llm.OutOfQuota(f"no model in {reel_writer.WRITERS} has free quota left today")
FACE = {"line": 2, "problem": "captions_on_face", "detail": "Captions cross Sita's eyes", "captions_to": "top"}


class QaTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "ak999"
        (self.folder / "work").mkdir(parents=True)
        (self.folder / "short.yaml").write_text(yaml.safe_dump({"beats": [{"text": "एक"}, {"text": "दो"}]}),
                                                encoding="utf-8")
        (self.folder / "work" / "manifest.json").write_text(json.dumps({"id": "ak999", "beats": [
            {"text": "एक", "start": 0.0, "end": 2.0}, {"text": "दो", "start": 2.0, "end": 4.0}]}), encoding="utf-8")

    def test_only_the_flash_models_look(self) -> None:
        asked = {}

        def generate(parts, *, schema, models, purpose):
            asked["models"] = models
            return {"lines": [FACE]}

        with patch.object(reel_qa, "_frame", return_value=b"jpeg"), patch.object(llm, "generate", generate):
            lines = reel_qa.check(self.folder)
        self.assertEqual(asked["models"], reel_writer.WRITERS)
        self.assertFalse(set(asked["models"]) & set(llm.FLASH_LITE))
        self.assertEqual([line["problem"] for line in lines], ["none", "captions_on_face"])

    def test_with_the_flash_models_spent_the_reel_stays_as_it_is(self) -> None:
        with patch.object(reel_qa, "check", side_effect=SPENT), \
                patch.object(reel_qa, "fix", side_effect=AssertionError("fixed")), \
                patch.object(reel_qa.reel_tools, "render", side_effect=AssertionError("rendered")):
            record = reel_qa.run(self.folder)
        self.assertTrue(record["finished"])
        self.assertEqual(len(record["rounds"]), 1)
        self.assertIn("free quota", record["rounds"][0]["skipped"])
        self.assertTrue(json.loads((self.folder / reel_qa.QA).read_text(encoding="utf-8"))["finished"])

    def test_a_fix_stands_when_the_look_after_it_finds_the_flash_models_spent(self) -> None:
        with patch.object(reel_qa, "check", side_effect=[[FACE], SPENT]), \
                patch.object(reel_qa, "fix", return_value=["line 2: captions to the top"]), \
                patch.object(reel_qa.reel_tools, "render") as render:
            record = reel_qa.run(self.folder)
        render.assert_called_once()
        self.assertTrue(record["finished"])
        self.assertEqual(record["rounds"][0]["changes"], ["line 2: captions to the top"])
        self.assertIn("skipped", record["rounds"][1])


if __name__ == "__main__":
    unittest.main()
