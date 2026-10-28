from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from ytc import tts
from ytc.spec import Beat, ShortSpec, Voice

FIRST, NEXT = tts.GEMINI_TTS[:2]
OUT = RuntimeError(f"no Gemini TTS model read the script: {FIRST}: HTTP 429 "
                   "GenerateRequestsPerDayPerProjectPerModel-FreeTier=10")


class TakesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = ShortSpec(id="ak999", title="t", beats=[Beat(text="एक दो"), Beat(text="तीन चार")],
                              voice=Voice(engine="gemini", voice="Orus", lang_code="h", direction="Narrate"))
        self.asked: list[list[str] | None] = []
        self.answers: list = []
        # How much of each line Whisper hears in every take; under tts.MIN_COVERAGE the next take is read too.
        self.coverage = [0.95, 0.95]
        self.audio = np.full(tts.SAMPLE_RATE, 0.1, dtype=np.float32)
        for name, fake in (("_tempo", lambda a, speed: a), ("_tighten", lambda a, longest: a),
                           ("_speech_bounds", lambda a: (0, len(a))), ("_heard", lambda wav, language: []),
                           ("_align", lambda tokens, heard: ([], self.coverage)),
                           ("_narration", lambda *args: "narration")):
            patcher = patch.object(tts, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _take(self, text: str, voice: Voice, models: list[str] | None = None):
        self.asked.append(models)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return self.audio, answer

    def _read(self, strays: list[tuple[float, float]]) -> tuple[object, str]:
        with tempfile.TemporaryDirectory() as tmp, patch.object(tts, "_gemini_take", self._take), \
                patch.object(tts, "_unscripted", side_effect=strays):
            result = tts._gemini(self.spec, Path(tmp))
            served = json.loads((Path(tmp) / "take.json").read_text(encoding="utf-8"))["served_by"]
        return result, served

    def test_with_no_take_kept_another_model_reads_once_the_first_ones_quota_runs_out(self) -> None:
        self.answers = [FIRST, FIRST, OUT, NEXT]
        result, served = self._read([(32.0, 33.1), (14.0, 15.1), (0.0, 0.2)])
        self.assertEqual((result, served), ("narration", NEXT))
        self.assertEqual(self.asked, [None, [FIRST], [FIRST], None])

    def test_with_a_take_kept_the_best_so_far_stays(self) -> None:
        self.answers, self.coverage = [FIRST, OUT], [0.6, 0.9]
        result, served = self._read([(0.0, 0.3)])
        self.assertEqual((result, served, self.asked), ("narration", FIRST, [None, [FIRST]]))

    def test_the_first_take_failing_on_every_model_fails_the_step(self) -> None:
        self.answers = [OUT]
        with self.assertRaises(RuntimeError):
            self._read([])
        self.assertEqual(self.asked, [None])


if __name__ == "__main__":
    unittest.main()
