from __future__ import annotations

import json
import base64
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from ytc.news import gemini
from ytc.news.live import natural, tts, voice
from ytc.news.live.voice import Voice, key_of


class NaturalVoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = {"natural_voice": True, "voice_budget": 1000}
        self.req = {"folder": "voice_local", "key": "test", "text": "यह आज की खबर है।", "lang": "hi"}
        self.out = self.root / "voice_local" / "test.wav"

    def tearDown(self):
        self.tmp.cleanup()

    def test_language_is_explicit_without_changing_existing_hindi_calls(self):
        for language, label in (("hi-IN", "Hindi"), ("en-IN", "Indian English")):
            body, _ = gemini._speech_body("gemini-2.5-flash-preview-tts", "Transcript.", "Clear.", "Orus", language)
            config = body["generationConfig"]["speechConfig"]
            self.assertEqual(config.get("languageCode"), language if language == "hi-IN" else None)
            self.assertIn(f"aloud in {label}.", body["contents"][0]["parts"][0]["text"])
        body, _ = gemini._speech_body("gemini-2.5-flash-preview-tts", "खबर।", "Clear.", "Orus")
        self.assertEqual(body["generationConfig"]["speechConfig"]["languageCode"], "hi-IN")

    def test_all_spoken_audio_parts_are_kept_after_a_thought_part(self):
        def part(value):
            return {"inlineData": {"mimeType": "audio/L16;rate=24000",
                                   "data": base64.b64encode(np.full(240, value, np.int16).tobytes()).decode()}}
        response = Mock(status_code=200)
        response.json.return_value = {"candidates": [{"content": {"parts": [
            {"text": "internal note", "thought": True}, part(100), part(200)]}, "finishReason": "STOP"}]}
        with patch.object(gemini, "_post", return_value=response), patch.object(gemini, "_COOL", {}):
            audio, rate, _ = gemini.speech("A real transcript.", "Clear", models=["fixture-tts"])
        self.assertEqual(rate, 24000)
        self.assertEqual(len(audio), 480)
        self.assertAlmostEqual(float(audio[-1]), 200 / 32768)

    def test_empty_speech_enters_a_cooldown_instead_of_repeating_requests(self):
        response = Mock(status_code=200)
        response.json.return_value = {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}
        with patch.object(gemini, "_post", return_value=response), patch.object(gemini, "_COOL", {}):
            with self.assertRaises(gemini.Exhausted):
                gemini.speech("A real transcript.", "Clear", models=["fixture-tts"])
            self.assertIn("fixture-tts", gemini.cooldowns())

    def test_transcript_check_accepts_spoken_numbers_and_acronyms(self):
        self.assertTrue(natural.check_read("सीबीआई ने 12 लोगों को बुलाया।", "सीबीआई ने बारह लोगों को बुलाया", "hi")["accepted"])
        self.assertTrue(natural.check_read("The council approved 12 new buses.", "The council approved twelve new buses.", "en")["accepted"])

    def test_missing_words_added_speech_changed_numbers_and_negations_fail(self):
        script = "नगर निगम ने आज नई बस सेवा शुरू नहीं की। यात्रियों को सोमवार तक इंतजार करना होगा।"
        for transcript in ("", script.replace("नई ", ""), script.replace("नहीं ", ""),
                           script.split("।")[0], script + " अब चैनल को सब्सक्राइब करें।"):
            self.assertFalse(natural.check_read(script, transcript, "hi")["accepted"], transcript)
        self.assertFalse(natural.check_read("There are 120 buses on this route.",
                                           "There are 130 buses on this route.", "en")["accepted"])
        script = "The council says the new service is not available before Monday morning."
        self.assertFalse(natural.check_read(script, script.replace("not ", ""), "en")["accepted"])

    def run_read(self, heard=None, failure=None):
        audio = np.sin(np.arange(24000) * 0.07).astype(np.float32) * 0.1
        with patch.object(natural, "STATE", self.root), patch.object(natural, "conf", return_value=self.settings), \
             patch.dict("os.environ", {"NEWS_GEMINI_API_KEY": "fixture-not-a-credential"}), \
             patch.object(gemini, "ready_at", return_value=0), patch.object(gemini, "restore"), \
             patch.object(gemini, "cooldowns", return_value={}), \
             patch.object(gemini, "speech", return_value=(audio, 24000, natural.SPEECH_MODELS[0]),
                          side_effect=failure) as speech, \
             patch.object(gemini, "text_json", return_value={"transcript": heard or self.req["text"],
                                                            "_model": natural.CHECK_MODELS[0]}) as check:
            ok = natural.Natural().save(self.req, self.out)
        return ok, speech, check

    def test_success_installs_checked_pcm_and_checker_never_gets_the_script(self):
        ok, speech, check = self.run_read()
        self.assertTrue(ok)
        with wave.open(str(self.out)) as wav:
            self.assertEqual((wav.getframerate(), wav.getsampwidth(), wav.getnchannels()), (48000, 2, 1))
        self.assertTrue(json.loads(self.out.with_suffix(".natural.json").read_text())["accepted"])
        self.assertNotIn(self.req["text"], str(check.call_args))
        self.assertFalse(check.call_args.kwargs["fallback"])
        self.assertEqual(speech.call_args.kwargs["language"], "hi-IN")

    def test_failed_check_or_refusal_does_not_overwrite_an_existing_read(self):
        self.out.parent.mkdir()
        self.out.write_bytes(b"existing broadcast read")
        for kwargs in ({"heard": "कुछ अलग शब्द बोले गए।"}, {"failure": gemini.Exhausted("quota")}):
            ok, _, _ = self.run_read(**kwargs)
            self.assertFalse(ok)
            self.assertEqual(self.out.read_bytes(), b"existing broadcast read")
            self.assertFalse(self.out.with_suffix(".natural.json").exists())
        spent = json.loads((self.root / "voice_quality.json").read_text())
        self.assertEqual(spent["chars"]["news"], len(self.req["text"]) * 2)

    def test_daily_budget_and_absent_enablement_make_no_generation_call(self):
        for settings in ({"natural_voice": False, "voice_budget": 1000},
                         {"natural_voice": True, "natural_voice_dynamic": False, "voice_budget": 1000},
                         {"natural_voice": True, "voice_budget": 1}):
            self.settings = settings
            ok, speech, check = self.run_read()
            self.assertFalse(ok)
            speech.assert_not_called()
            check.assert_not_called()

    def test_disabled_dynamic_generation_keeps_the_recorded_neural_fixed_lines(self):
        (self.root / "english.json").write_text(json.dumps({"natural_voice": True, "natural_voice_dynamic": False}))
        fixed = self.root / "assets" / "voice_en"
        fixed.mkdir(parents=True)
        (fixed / "manifest.json").write_text("{}")
        with patch.object(voice, "CONF", self.root), patch.object(voice, "ASSETS", self.root / "assets"), \
             patch.object(voice, "Voice") as factory:
            voice.for_channel("english")
        self.assertFalse(factory.call_args.kwargs["natural"])
        self.assertEqual(factory.call_args.kwargs["fixed_folder"], "voice_en")
        self.assertEqual(factory.call_args.args[1], "voice_en_local")

    def test_cooling_models_are_not_polled_again(self):
        with patch.object(natural, "STATE", self.root), patch.object(natural, "conf", return_value=self.settings), \
             patch.dict("os.environ", {"NEWS_GEMINI_API_KEY": "fixture-not-a-credential"}), \
             patch.object(gemini, "restore"), patch.object(gemini, "ready_at", return_value=float("inf")), \
             patch.object(gemini, "speech") as speech:
            self.assertFalse(natural.Natural().save(self.req, self.out))
            speech.assert_not_called()

    def test_upgrade_stays_queued_with_an_old_read_and_leaves_after_quality_install(self):
        with patch.object(tts, "STATE", self.root), patch.object(tts, "QUEUE", self.root / "queue"):
            self.out.parent.mkdir()
            self.out.write_bytes(b"old")
            tts.request("voice_local", "test", self.req["text"], "hi", "hindimale", 1.0, 2, upgrade=True)
            self.assertEqual(len(tts.pending()), 1)
            self.out.with_suffix(".natural.json").write_text('{"accepted":true}')
            self.assertEqual(tts.pending(), [])

    def test_new_take_reloads_without_changing_audio_already_being_played(self):
        voice = object.__new__(Voice)
        voice.cache, voice.clips, voice.clip_revisions = self.root, {}, {}
        key = "say-" + key_of(self.req["text"])
        target = self.root / f"{key}.wav"
        def write(path, value):
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(48000)
                wav.writeframes(np.full(4800, value, dtype=np.int16).tobytes())
        write(target, 100)
        playing = voice.say(self.req["text"])
        part = self.root / "replacement.wav"
        write(part, 200)
        part.replace(target)
        current = voice.say(self.req["text"])
        self.assertIsNot(playing, current)
        self.assertAlmostEqual(float(playing[0, 0]), 100 / 32768)
        self.assertAlmostEqual(float(current[0, 0]), 200 / 32768)


if __name__ == "__main__":
    unittest.main()
