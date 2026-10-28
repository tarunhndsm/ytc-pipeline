from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
import yaml
from PIL import Image

from ytc import publish, reel_art, reel_factory, reel_pictures, reel_rescue, reels

KEYS = {"IG_CLOUDFLARE_ACCOUNT_ID": "account", "IG_CLOUDFLARE_API_TOKEN": "token"}
# Klein alone, as the steps paint while the free models are down (reel_factory.FREE_WAIT).
KLEIN_ONLY = {"YTC_FREE_ART": "0"}
# A factory state whose free models are down.
DOWN = {"free_down": "2099-01-01T00:00:00+00:00"}
KLEIN, LIGHTNING, SDXL = (*reel_art.MODELS, *reel_art.FREE_MODELS)


def _museum(n: int) -> dict:
    return {"ref": f"m{n}", "title": f"Painting {n}", "url": f"https://example.org/m{n}.jpg", "source": "The Met",
            "license": "CC0", "size": [2000, 3000], "subject": [0.2, 0.2, 0.8, 0.8], "faces": [], "kind": "painting",
            "hindi": f"चित्र {n}"}


def _painting(n: int) -> dict:
    url = f"https://res.cloudinary.com/demo/ak999-{n:02d}.jpg"
    return {"ref": f"ai:ak999-{n:02d}-7", "title": f"AI painting: brief {n}", "url": url, "page": url, "thumb": url,
            "source": reel_art.SOURCE, "license": reel_art.LICENSE, "artist": "FLUX.2 klein", "width": 864,
            "height": 1536, "size": [864, 1536], "subject": [0.2, 0.2, 0.8, 0.8], "faces": [], "kind": "painting",
            "hindi": f"बना चित्र {n}"}


class PaintLinesTests(unittest.TestCase):
    """How many lines a picture try or a rescue gets painted by klein alone."""

    def setUp(self) -> None:
        env = patch.dict(os.environ, KEYS | KLEIN_ONLY)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("YTC_PAINT_NEURONS", None)
        self.beats = [{"text": f"line {n}", "picture": f"brief {n}"} for n in range(1, 13)]
        self.out = set()
        painter = patch.object(reel_art, "paint", side_effect=self._paint)
        painter.start()
        self.addCleanup(painter.stop)

    def _paint(self, reel, number, beat, paid=True, refused=None):
        if number in self.out:
            refused.append("paid")
            return None, 0.0
        return {"ref": f"ai:{reel}-{number:02d}-1", "artist": "FLUX.2 klein"}, reel_art.NEURONS_EACH

    def test_nothing_is_painted_without_the_cloudflare_keys(self) -> None:
        with patch.dict(os.environ, {"IG_CLOUDFLARE_API_TOKEN": ""}):
            self.assertEqual(reel_art.paint_lines("ak999", self.beats, [1, 2]), {})
        reel_art.paint.assert_not_called()

    def test_a_try_paints_at_most_max_painted_lines_the_most_needed_first(self) -> None:
        count = reel_art.MAX_PAINTED + 3
        beats = [{"text": f"line {n}", "picture": f"brief {n}"} for n in range(1, count + 1)]
        made = reel_art.paint_lines("ak999", beats, list(range(count, 0, -1)))
        self.assertEqual(sorted(made), list(range(count - reel_art.MAX_PAINTED + 1, count + 1)))

    def test_the_neurons_left_today_limit_the_paintings(self) -> None:
        with patch.dict(os.environ, {"YTC_PAINT_NEURONS": str(2 * reel_art.NEURONS_EACH + 30)}):
            self.assertEqual(sorted(reel_art.paint_lines("ak999", self.beats, [7, 1, 2, 3])), [1, 7])

    def test_nothing_is_painted_once_todays_neurons_are_spent(self) -> None:
        with patch.dict(os.environ, {"YTC_PAINT_NEURONS": "100"}):
            self.assertEqual(reel_art.paint_lines("ak999", self.beats, [1, 2]), {})
        reel_art.paint.assert_not_called()

    def test_a_line_the_neurons_ran_out_on_is_left_to_the_museum_pictures(self) -> None:
        self.out = {2}
        self.assertEqual(sorted(reel_art.paint_lines("ak999", self.beats, [1, 2, 3])), [1, 3])

    def test_what_a_call_paints_comes_off_the_neurons_the_next_one_has(self) -> None:
        with patch.dict(os.environ, {"YTC_PAINT_NEURONS": str(3 * reel_art.NEURONS_EACH + 30)}):
            self.assertEqual(sorted(reel_art.paint_lines("ak999", self.beats, [1, 2])), [1, 2])
            self.assertEqual(sorted(reel_art.paint_lines("ak999", self.beats, [3, 4])), [3])
            self.assertEqual(reel_art.paint_lines("ak999", self.beats, [5]), {})

    def test_cloudflare_refusing_ends_the_days_share_for_the_factory_too(self) -> None:
        self.out = {2}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"YTC_USAGE_LOG": f"{tmp}/usage.jsonl"}):
            self.assertEqual(sorted(reel_art.paint_lines("ak999", self.beats, [1, 2, 3])), [1, 3])
            self.assertEqual(os.environ["YTC_PAINT_NEURONS"], "0.0")
            spent = reel_factory._spend(Path(tmp) / "usage.jsonl")
        self.assertEqual((spent[reel_art.OUT_OF_NEURONS], spent["calls"]), (True, 0))

    def test_flagged_paintings_come_off_the_neurons_too(self) -> None:
        costs = {1: (None, 264.0), 2: ({"ref": "ai:ak999-02-1"}, 263.76)}
        reel_art.paint.side_effect = lambda reel, number, beat, *rest: costs[number]
        with patch.dict(os.environ, {"YTC_PAINT_NEURONS": "1000"}):
            made = reel_art.paint_lines("ak999", self.beats, [1, 2])
            self.assertAlmostEqual(float(os.environ["YTC_PAINT_NEURONS"]), 1000 - 264.0 - 263.76)
        self.assertEqual(made, {2: {"ref": "ai:ak999-02-1"}})

    def test_every_line_klein_paints_may_name_a_god(self) -> None:
        beats = [{"text": "line 1", "picture": "Lord Krishna smiles"}, {"text": "line 2", "picture": "Kunti"}]
        reel_art.paint_lines("ak999", beats, [1, 2])
        self.assertEqual(sorted(c.args[1] for c in reel_art.paint.call_args_list if c.args[3]), [1, 2])


class FreePaintLinesTests(unittest.TestCase):
    """With the free models, every line is painted whatever the neurons left, and klein paints only the lines that
    name no god, as far as the neurons go."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.usage = Path(tmp.name) / "usage.jsonl"
        env = patch.dict(os.environ, KEYS | {"YTC_USAGE_LOG": str(self.usage)})
        env.start()
        self.addCleanup(env.stop)
        for name in ("YTC_PAINT_NEURONS", "YTC_FREE_ART"):
            os.environ.pop(name, None)
        self.beats = [{"text": f"line {n}", "picture": brief} for n, brief in enumerate(
            ("Lord Krishna smiles at Rukmini", "Kunti at the river", "Hanuman leaps to Lanka", "King Pandu hunts a deer",
             "Karna gives away his armour", "Goddess Ambika's shrine"), start=1)]
        self.paid: dict[int, bool] = {}
        self.free_fails = False
        painter = patch.object(reel_art, "paint", side_effect=self._paint)
        painter.start()
        self.addCleanup(painter.stop)

    def _paint(self, reel, number, beat, paid=True, refused=None):
        self.paid[number] = paid
        if paid and "paid" not in refused:
            return {"ref": f"ai:{reel}-{number:02d}-1", "artist": "FLUX.2 klein"}, reel_art.NEURONS_EACH
        if self.free_fails:
            return None, 0.0
        return {"ref": f"ai:{reel}-{number:02d}-2", "artist": "SDXL-Lightning"}, 0.0

    def _notes(self) -> dict:
        return reel_factory._spend(self.usage)

    def test_every_line_is_painted_with_no_neurons_left(self) -> None:
        with patch.dict(os.environ, {"YTC_PAINT_NEURONS": "0.0"}):
            made = reel_art.paint_lines("ak999", self.beats, [1, 2, 3, 4, 5, 6])
        self.assertEqual(sorted(made), [1, 2, 3, 4, 5, 6])
        self.assertFalse(any(self.paid.values()))

    def test_klein_paints_the_lines_that_name_no_god_as_far_as_the_neurons_go(self) -> None:
        with patch.dict(os.environ, {"YTC_PAINT_NEURONS": str(2 * reel_art.NEURONS_EACH + 30)}):
            made = reel_art.paint_lines("ak999", self.beats, [5, 1, 2, 3, 4, 6])
            self.assertAlmostEqual(float(os.environ["YTC_PAINT_NEURONS"]), 30)
        self.assertEqual(sorted(made), [1, 2, 3, 4, 5, 6])
        self.assertEqual(sorted(n for n, paid in self.paid.items() if paid), [2, 5])

    def test_klein_refused_ends_its_day_but_not_the_paintings(self) -> None:
        def refused_klein(reel, number, beat, paid=True, refused=None):
            if paid:
                refused.append("paid")
            return {"ref": f"ai:{reel}-{number:02d}-2", "artist": "SDXL-Lightning"}, 0.0

        reel_art.paint.side_effect = refused_klein
        self.assertEqual(sorted(reel_art.paint_lines("ak999", self.beats, [1, 2, 3])), [1, 2, 3])
        self.assertEqual(os.environ["YTC_PAINT_NEURONS"], "0.0")
        self.assertEqual((self._notes().get(reel_art.OUT_OF_NEURONS), self._notes().get(reel_art.FREE_DOWN)),
                         (True, None))

    def test_free_models_that_paint_nothing_are_reported_down(self) -> None:
        self.free_fails = True
        self.assertEqual(sorted(reel_art.paint_lines("ak999", self.beats, [1, 2, 3])), [2])
        self.assertTrue(self._notes()[reel_art.FREE_DOWN])

    def test_free_models_that_paint_one_line_are_not_down(self) -> None:
        reel_art.paint.side_effect = lambda reel, number, beat, paid, refused: (
            ({"ref": "ai:x", "artist": "SDXL-Lightning"}, 0.0) if number == 3 else (None, 0.0))
        self.assertEqual(sorted(reel_art.paint_lines("ak999", self.beats, [1, 3])), [3])
        self.assertNotIn(reel_art.FREE_DOWN, self._notes())

    def test_the_gods_are_told_from_the_mortals(self) -> None:
        for brief in ("Lord Balarama holding his plough", "Parashurama raises his axe", "Rama strings the bow",
                      "Hanumanji leaps", "Sita in the Ashoka grove", "Rukmini kneels before Goddess Ambika",
                      "Yamaraja takes Satyavan", "Brahma's court"):
            self.assertTrue(reel_art.GODS.search(brief), brief)
        for brief in ("Prince Rukmi draws his sword", "Kunti at the river", "A Brahmin at the gate", "Karna and Arjuna",
                      "King Jarasandha", "Gandhari blindfolds herself"):
            self.assertFalse(reel_art.GODS.search(brief), brief)


class _Painting(unittest.TestCase):
    """A painting's hosting faked, the Cloudflare keys set, and `env` on top."""

    env: dict = {}

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.usage = Path(tmp.name) / "usage.jsonl"
        env = patch.dict(os.environ, KEYS | {"YTC_USAGE_LOG": str(self.usage)} | self.env)
        env.start()
        self.addCleanup(env.stop)
        picture = io.BytesIO()
        Image.new("RGB", (reel_art.WIDTH, reel_art.HEIGHT), "orange").save(picture, "JPEG")
        self.jpeg = picture.getvalue()
        self.hosted: list[Path] = []
        host = patch.object(publish, "host_image", side_effect=self._host)
        host.start()
        self.addCleanup(host.stop)

    def _host(self, path: Path, public_id: str) -> str:
        self.assertTrue(path.exists())
        self.hosted.append(path)
        return f"https://res.cloudinary.com/demo/{public_id}.jpg"


class PaintTests(_Painting):
    """One painting by klein alone: made, hosted, credited as AI, and its neurons counted."""

    env = KLEIN_ONLY

    def test_a_painting_is_hosted_and_credited_as_made_with_ai(self) -> None:
        with patch.object(reel_art, "_run", return_value=(self.jpeg, 131.88)):
            hit, spent = reel_art.paint("ak999", 3, {"picture": "Sage Durvasa blesses young Kunti"})
        self.assertEqual(spent, 131.88)
        self.assertFalse(self.hosted[0].exists(), "the painting's local copy was left behind")
        self.assertTrue(hit["ref"].startswith("ai:ak999-03-"))
        self.assertEqual((hit["source"], hit["license"], hit["artist"]), ("AI", "AI-generated", "FLUX.2 klein"))
        self.assertEqual((hit["width"], hit["height"]), (reel_art.WIDTH, reel_art.HEIGHT))
        self.assertIn(f"{reel_art.CLOUDINARY_FOLDER}/ak999-03-", hit["url"])
        self.assertEqual(json.loads(self.usage.read_text(encoding="utf-8"))["neurons"], 131.88)

    def test_a_painting_turned_down_for_anything_but_a_flag_is_not_painted_again(self) -> None:
        with patch.object(reel_art, "_run", side_effect=reel_art.Refused("HTTP 400: AiError: Bad input")) as run:
            self.assertEqual(reel_art.paint("ak999", 3, {"picture": "Kunti at the river"}), (None, 0.0))
        self.assertEqual(run.call_count, 1)

    def test_no_painting_when_cloudflare_is_down(self) -> None:
        with patch.object(reel_art, "_run", side_effect=requests.ConnectionError("down")):
            self.assertEqual(reel_art.paint("ak999", 3, {"picture": "Kunti at the river"}), (None, 0.0))

    def test_cloudflare_refusing_for_the_days_neurons_raises(self) -> None:
        with patch.object(reel_art.requests, "post", return_value=MagicMock(status_code=429, text="Too Many Requests")):
            with self.assertRaises(reel_art.OutOfNeurons):
                reel_art._run("@cf/black-forest-labs/flux-2-klein-4b", "a painting", 1)

    def test_a_flagged_painting_is_painted_again_with_another_seed(self) -> None:
        seeds = []

        def run(model, text, seed):
            seeds.append(seed)
            if len(seeds) == 1:
                raise reel_art.Flagged("HTTP 400: AiError: Your output has been flagged.", 131.88)
            return self.jpeg, 131.88

        with patch.object(reel_art, "_run", side_effect=run), patch.object(reel_art.random, "randrange", side_effect=[7, 8]):
            hit, spent = reel_art.paint("ak999", 3, {"picture": "Rama and Sita in a forest"})
        self.assertEqual((seeds, hit["artist"], hit["ref"]), ([7, 8], "FLUX.2 klein", "ai:ak999-03-8"))
        self.assertAlmostEqual(spent, 263.76)
        self.assertAlmostEqual(json.loads(self.usage.read_text(encoding="utf-8"))["neurons"], 263.76)

    def test_a_line_flagged_every_try_still_counts_its_neurons(self) -> None:
        flagged = reel_art.Flagged("HTTP 400: AiError: Your output has been flagged.", 131.88)
        with patch.object(reel_art, "_run", side_effect=flagged) as run:
            hit, spent = reel_art.paint("ak999", 3, {"picture": "Rama holding his bow"})
        self.assertEqual((hit, run.call_count), (None, reel_art.FLAGGED_TRIES))
        self.assertAlmostEqual(spent, reel_art.FLAGGED_TRIES * 131.88)
        self.assertAlmostEqual(json.loads(self.usage.read_text(encoding="utf-8"))["neurons"], spent)

    def test_a_flagged_painting_is_charged_what_cloudflare_says_else_a_painting(self) -> None:
        answer = MagicMock(status_code=400, text='{"errors": []}', headers={})
        answer.json.return_value = {"errors": [{"message": "AiError: Your output has been flagged.", "code": 3030}]}
        with patch.object(reel_art.requests, "post", return_value=answer):
            with self.assertRaises(reel_art.Flagged) as guessed:
                reel_art._run("@cf/black-forest-labs/flux-2-klein-4b", "a painting", 1)
            answer.headers = {"cf-ai-neurons": "120.5"}
            with self.assertRaises(reel_art.Flagged) as told:
                reel_art._run("@cf/black-forest-labs/flux-2-klein-4b", "a painting", 1)
        self.assertEqual((guessed.exception.neurons, told.exception.neurons), (reel_art.NEURONS_EACH, 120.5))

    def test_a_refusal_carries_cloudflare_s_message_and_not_the_url(self) -> None:
        answer = MagicMock(status_code=400, text='{"errors": []}', headers={})
        answer.json.return_value = {"errors": [{"message": "AiError: Your output has been flagged.", "code": 3030}]}
        with patch.object(reel_art.requests, "post", return_value=answer):
            with self.assertRaises(reel_art.Flagged) as flagged:
                reel_art._run("@cf/black-forest-labs/flux-2-klein-4b", "a painting", 1)
            answer.json.return_value = {"errors": [{"message": "AiError: Bad input", "code": 5006}]}
            with self.assertRaises(reel_art.Refused) as refused:
                reel_art._run("@cf/black-forest-labs/flux-2-klein-4b", "a painting", 1)
        self.assertEqual(str(flagged.exception), "HTTP 400: AiError: Your output has been flagged.")
        self.assertNotIsInstance(refused.exception, reel_art.Flagged)
        self.assertEqual((str(refused.exception), refused.exception.neurons), ("HTTP 400: AiError: Bad input", 0.0))

    def test_the_figures_a_brief_names_are_painted_as_the_old_paintings_show_them(self) -> None:
        with patch.object(reel_art, "CINEMATIC", False):
            text = reel_art.prompt({"picture": "Pahari painting of Yamaraja bowing deeply before Krishna and Balarama."})
        self.assertTrue(text.startswith("Pahari painting of Yamaraja bowing deeply before Krishna and Balarama. "
                                        "Yama, god of death, is dark-skinned"))
        self.assertLess(text.index("Krishna has blue skin"), text.index("Balarama has fair skin"))
        self.assertNotIn("Rama has", reel_art.icons("Scenes from the Ramayana and the life of Parashurama"))
        self.assertEqual(reel_art.icons("Siva and Brahma, Vishnu, Indra"), "Shiva has pale ash-smeared skin, matted "
                         "locks with the crescent moon, a snake at his neck and a trident. Brahma has four bearded "
                         "heads. Vishnu has blue skin, yellow robes and four arms holding a conch, a discus, a mace and "
                         "a lotus. ")
        for name in ("Hanuman", "Hanumanji", "Bajrangbali", "Anjaneya"):
            self.assertIn("monkey's face with golden-orange fur, a long tail", reel_art.icons(f"{name} leaps to Lanka"))

    def test_line_1_asks_for_a_close_vivid_figure_and_every_line_for_the_style(self) -> None:
        first, other = (reel_art.prompt({"picture": "Kunti at the river."}, n == 1) for n in (1, 2))
        self.assertTrue(first.startswith("Kunti at the river. " + reel_art.FIRST))
        self.assertTrue(other.startswith("Kunti at the river. " + reel_art.STYLE))
        self.assertIn("close", reel_art.FIRST)
        self.assertNotIn("Sages", other)

    def test_painted_line_by_line_a_brief_loses_the_school_of_painting_it_names(self) -> None:
        with patch.object(reel_art, "CINEMATIC", True):
            for brief in ("Pahari painting of Kunti at the river.", "A Kangra miniature painting showing Kunti at the river",
                          "Kunti at the river, in Pahari style", "miniature painting of Kunti at the river"):
                self.assertTrue(reel_art.prompt({"picture": brief}).startswith("Kunti at the river. "), brief)
            self.assertTrue(reel_art.prompt({"picture": "Indian king Pandu in a forest"}).startswith(
                "Indian king Pandu in a forest. "))
            # As ak086's writer gave them (23 Oct), every brief ending in a style.
            for brief, left in (("Close-up of King Vrihadratha in shock at two cloth bundles, warm torchlight, "
                                 "traditional Indian classical painting style.", "Close-up of King Vrihadratha in shock "
                                 "at two cloth bundles, warm torchlight. "),
                                ("The two queens of Kasi sharing a mango, hopeful expressions, traditional Indian art "
                                 "style", "The two queens of Kasi sharing a mango, hopeful expressions. "),
                                ("Sage Chanda-kausika under a mango tree, classical Indian painting",
                                 "Sage Chanda-kausika under a mango tree. ")):
                self.assertTrue(reel_art.prompt({"picture": brief}).startswith(left), brief)
        self.assertIn("never a crown", reel_art.prompt({"picture": "Sage Durvasa blesses young Kunti"}))


class FreePaintTests(_Painting):
    """One painting with the free models there: klein first when asked, once, then the free models."""

    def setUp(self) -> None:
        super().setUp()
        os.environ.pop("YTC_FREE_ART", None)
        self.asked: list[tuple[str, str]] = []
        self.answers: dict[str, list] = {}

    def _run(self, model, text, seed):
        self.asked.append((model, text))
        answer = (self.answers.get(model) or [(self.jpeg, 0.0 if model in reel_art.FREE_MODELS else 131.88)]).pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _paint(self, brief: str, number: int = 3, paid: bool = True, refused: list | None = None):
        with patch.object(reel_art, "_run", side_effect=self._run):
            return reel_art.paint("ak999", number, {"picture": brief}, paid, refused)

    def test_a_line_klein_paints_is_painted_by_klein(self) -> None:
        hit, spent = self._paint("Kunti at the river")
        self.assertEqual(([m for m, _ in self.asked], hit["artist"], spent), ([KLEIN], "FLUX.2 klein", 131.88))

    def test_a_flagged_painting_goes_to_the_free_models_without_another_klein_try(self) -> None:
        self.answers[KLEIN] = [reel_art.Flagged("HTTP 400: AiError: Your output has been flagged.", 131.88)]
        hit, spent = self._paint("Kunti at the river")
        self.assertEqual(([m for m, _ in self.asked], hit["artist"]), ([KLEIN, LIGHTNING], "SDXL-Lightning"))
        self.assertAlmostEqual(spent, 131.88)
        self.assertTrue(hit["ref"].startswith("ai:ak999-03-"))
        self.assertEqual(hit["license"], reel_art.LICENSE)

    def test_a_line_klein_isnt_asked_for_is_painted_free_with_the_short_prompt(self) -> None:
        hit, spent = self._paint("Hanuman leaps to Lanka", number=1, paid=False)
        self.assertEqual(([m for m, _ in self.asked], hit["artist"], spent), ([LIGHTNING], "SDXL-Lightning", 0.0))
        text = self.asked[0][1]
        self.assertTrue(text.startswith(reel_art.FREE_FIRST + "Hanuman leaps to Lanka. Hanuman, the vanara god"), text)
        self.assertTrue(text.endswith(reel_art.FREE_STYLE))
        self.assertFalse(self.usage.exists(), "a free painting was logged with neurons")

    def test_klein_refused_for_the_neurons_hands_the_line_to_the_free_models(self) -> None:
        refused: list[str] = []
        self.answers[KLEIN] = [reel_art.OutOfNeurons("4006: daily free allocation of 10,000 neurons used up")]
        hit, _ = self._paint("Kunti at the river", refused=refused)
        self.assertEqual((refused, hit["artist"]), (["paid"], "SDXL-Lightning"))
        self._paint("Kunti at the well", refused=refused)
        self.assertEqual([m for m, _ in self.asked], [KLEIN, LIGHTNING, LIGHTNING])

    def test_lightning_failing_leaves_the_line_to_sdxl(self) -> None:
        self.answers[LIGHTNING] = [reel_art.Refused("HTTP 503: capacity")]
        hit, _ = self._paint("Hanuman leaps to Lanka", paid=False)
        self.assertEqual(([m for m, _ in self.asked], hit["artist"]), ([LIGHTNING, SDXL], "Stable Diffusion XL"))

    def test_free_models_refused_for_the_neurons_arent_asked_again(self) -> None:
        refused: list[str] = []
        self.answers[LIGHTNING] = [reel_art.OutOfNeurons("4006: neurons used up")]
        self.assertEqual(self._paint("Hanuman leaps to Lanka", paid=False, refused=refused), (None, 0.0))
        self.assertEqual((refused, [m for m, _ in self.asked]), (["free"], [LIGHTNING]))

    def test_a_free_model_is_sent_json_with_a_negative_prompt_and_answers_with_the_png(self) -> None:
        answer = MagicMock(status_code=200, headers={"content-type": "image/png", "cf-ai-neurons": "0.00"},
                           content=self.jpeg)
        with patch.object(reel_art.requests, "post", return_value=answer) as post:
            self.assertEqual(reel_art._run(LIGHTNING, "a painting", 5), (self.jpeg, 0.0))
        body = post.call_args.kwargs["json"]
        self.assertEqual((body["seed"], body["width"], body["height"], body["negative_prompt"]),
                         (5, reel_art.FREE_WIDTH, reel_art.FREE_HEIGHT, reel_art.NEGATIVE))
        self.assertIn("text", body["negative_prompt"])

    def test_klein_is_sent_a_form_for_its_own_size(self) -> None:
        answer = MagicMock(status_code=200, headers={"content-type": "application/json", "cf-ai-neurons": "131.88"})
        answer.json.return_value = {"result": {"image": base64.b64encode(self.jpeg).decode()}}
        with patch.object(reel_art.requests, "post", return_value=answer) as post:
            self.assertEqual(reel_art._run(KLEIN, "a painting", 5), (self.jpeg, 131.88))
        form = post.call_args.kwargs["files"]
        self.assertEqual((form["width"], form["height"]), ((None, str(reel_art.WIDTH)), (None, str(reel_art.HEIGHT))))

    def test_a_free_models_429_is_its_rate_and_not_the_days_neurons(self) -> None:
        answer = MagicMock(status_code=429, text='{"errors": [{"message": "Capacity temporarily exceeded"}]}',
                           headers={})
        answer.json.return_value = {"errors": [{"message": "Capacity temporarily exceeded", "code": 3040}]}
        with patch.object(reel_art.requests, "post", return_value=answer):
            with self.assertRaises(reel_art.Refused) as rate:
                reel_art._run(LIGHTNING, "a painting", 1)
            self.assertNotIsInstance(rate.exception, reel_art.OutOfNeurons)
            answer.text = "you have used up your daily free allocation of 10,000 neurons"
            with self.assertRaises(reel_art.OutOfNeurons):
                reel_art._run(LIGHTNING, "a painting", 1)


class PictureStepTests(unittest.TestCase):
    """Which lines the picture step paints, and how the caption credits the paintings."""

    def test_line_1_first_then_lines_without_a_picture_then_pictures_of_another_scene(self) -> None:
        beats = [{"text": f"line {n}", "picture": f"brief {n}", "role": "story"} for n in range(1, 7)]
        chosen = {1: {"ref": "m1"}, 2: {"ref": "m2"}, 4: {"ref": "m4"}, 5: {"ref": "ai:ak999-05-1"}, 6: {"ref": "m6"}}
        fits = {1: "elsewhere", 2: "moment", 4: "none", 5: "elsewhere", 6: "figure"}
        with patch.object(reel_pictures, "paint_framed", return_value={}) as paint:
            reel_pictures.painted("ak999", beats, chosen, fits)
        self.assertEqual(paint.call_args.args[2], [1, 3, 4])

    def test_a_painting_the_framing_check_turns_down_is_painted_again_up_to_paint_tries(self) -> None:
        beats = [{"text": f"line {n}", "picture": f"brief {n}", "role": "story"} for n in range(1, 7)]
        again = reel_pictures.PAINT_TRIES - 1
        paintings = [{3: _painting(3), 5: _painting(5)}] + [{5: _painting(5) | {"ref": f"ai:ak999-05-{n}"}}
                                                            for n in range(again)]
        framed = [{3: _painting(3)}] + [{}] * again
        with (patch.object(reel_art, "paint_lines", side_effect=paintings) as paint,
              patch.object(reel_pictures, "frame_all", side_effect=framed) as frame):
            self.assertEqual(sorted(reel_pictures.paint_framed("ak999", beats, [3, 5, 6])), [3])
        self.assertEqual([c.args[2] for c in paint.call_args_list], [[3, 5, 6]] + [[5]] * again)
        self.assertEqual(sorted(frame.call_args_list[1].args[1]), [5])
        self.assertIn("letters or script painted on one", frame.call_args.args[4])

    def test_paintings_are_credited_on_their_own_line(self) -> None:
        lines = reel_pictures.credit_lines([_museum(1), _painting(2)]).split("\n")
        self.assertEqual(lines, ["चित्र: चित्र 1, The Met (सभी सार्वजनिक डोमेन या CC0)",
                                 reel_pictures.MADE_CREDIT + "बना चित्र 2"])

    def test_new_credits_replace_the_old_painting_credit(self) -> None:
        description = f"about\nचित्र: old\n{reel_pictures.MADE_CREDIT}old"
        self.assertEqual(reel_pictures.with_credits(description, [_painting(2)]),
                         f"about\n{reel_pictures.MADE_CREDIT}बना चित्र 2")


class RescueTests(unittest.TestCase):
    """A rescue shows paintings for the faulted lines, and paintings can make up for too few pictures."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for patcher in (patch.object(reels, "REELS", self.root), patch.object(reel_rescue, "why_not", return_value="")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _reel(self, name: str, faulted: list[int], no_brief: tuple[int, ...] = ()) -> Path:
        """Eight lines: 1 to 6 with a picture of their own, 7 showing line 3's again and 8 line 4's."""
        folder = self.root / name
        (folder / "work").mkdir(parents=True)
        beats = [{"text": f"line {n}", "role": "story", "visual": {
            "source": "url", "url": f"https://example.org/m{n}.jpg", "credit": {"source": "The Met"}}
            if n <= 6 else {"reuse": n - 4}} for n in range(1, 9)]
        (folder / "short.yaml").write_text(yaml.safe_dump({"id": name, "title": "A story", "description": "about\n",
                                                           "captions": {}, "beats": beats}, allow_unicode=True),
                                           encoding="utf-8")
        (folder / reel_pictures.PICTURES).write_text(json.dumps({str(n): _museum(n) for n in range(1, 7)}),
                                                     encoding="utf-8")
        (folder / reels.REVIEW).write_text(json.dumps({"frames": [{"beat": n, "problem": "off-topic picture"}
                                                                  for n in faulted]}), encoding="utf-8")
        (folder / "work" / "script.json").write_text(json.dumps({"script": {"beats": [
            {"text": f"line {n}", "picture": "" if n in no_brief else f"brief {n}", "role": "story"}
            for n in range(1, 9)]}}), encoding="utf-8")
        return folder

    def test_a_faulted_line_shows_its_painting_and_so_do_the_lines_that_showed_its_picture(self) -> None:
        spec, kept, changes = reel_rescue.plan(self._reel("ak901", [3, 5]), {3: _painting(3)})
        self.assertEqual(changes, {3: "a painting made for it", 7: "line 3's painting again",
                                   5: "line 4's picture again"})
        self.assertEqual(spec["beats"][2]["visual"]["url"], _painting(3)["url"])
        self.assertEqual(spec["beats"][6]["visual"]["reuse"], 3)
        self.assertEqual(kept["3"]["license"], reel_art.LICENSE)
        self.assertNotIn("5", kept)
        self.assertTrue(spec["description"].endswith(reel_pictures.MADE_CREDIT + "बना चित्र 3"))

    def test_paintings_make_up_for_too_few_unfaulted_pictures(self) -> None:
        folder = self._reel("ak902", [2, 3, 4, 5])
        with self.assertRaises(ValueError):
            reel_rescue.plan(folder)
        kept = reel_rescue.plan(folder, {n: _painting(n) for n in (2, 3, 4)})[1]
        self.assertEqual(sorted(kept, key=int), ["1", "2", "3", "4", "6"])

    def test_a_line_without_a_brief_gets_no_painting(self) -> None:
        self.assertEqual(reel_rescue.paintable(self._reel("ak902", [2, 3, 4, 5], no_brief=(3,))), [2, 4, 5])

    def test_the_paint_pass_takes_only_the_reels_that_need_paintings_and_only_as_many_as_today_allows(self) -> None:
        self._reel("ak901", [3])
        self._reel("ak902", [2, 3, 4, 5])
        self.assertEqual(reel_rescue.to_render(["ak901", "ak902"]), ["ak901"])
        self.assertEqual(reel_rescue.to_render(["ak901", "ak902"], None, 45), ["ak902"])
        self.assertEqual(reel_rescue.to_render(["ak901", "ak902"], None, 2), [])

    def test_the_report_says_the_rescue_has_paintings(self) -> None:
        folder = self._reel("ak901", [3])
        (folder / reels.RESCUE).write_text(json.dumps({"video_sha256": "abc", "as_is": False, "changes": {
            "3": "a painting made for it", "7": "line 3's painting again"}}), encoding="utf-8")
        self.assertEqual(reel_rescue.told(folder), "rescued with paintings for the pictures the judge faulted")


class NeuronTests(unittest.TestCase):
    """The factory counts Ankahi's share of the Cloudflare neurons by Cloudflare's UTC day."""

    def test_a_steps_paintings_are_counted_in_neurons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            usage = Path(tmp) / "usage.jsonl"
            usage.write_text("\n".join(json.dumps(call) for call in (
                {"model": "gemini-3-flash-preview", "input_tokens": 100, "output_tokens": 50, "usd": 0.001},
                {"model": "cloudflare:@cf/black-forest-labs/flux-2-klein-4b", "neurons": 131.88},
                {"model": "cloudflare:@cf/black-forest-labs/flux-1-schnell", "neurons": 40.0})), encoding="utf-8")
            spent = reel_factory._spend(usage)
        self.assertEqual((spent["calls"], spent["tokens"]), (3, 150))
        self.assertAlmostEqual(spent["neurons"], 171.88)

    def test_a_step_without_paintings_has_no_neurons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            usage = Path(tmp) / "usage.jsonl"
            usage.write_text(json.dumps({"model": "gemini-3-flash-preview", "usd": 0.001}), encoding="utf-8")
            self.assertNotIn("neurons", reel_factory._spend(usage))

    def test_the_share_starts_again_at_midnight_utc_and_never_goes_below_nothing(self) -> None:
        state: dict = {}
        late = datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc)
        reel_factory._add_neurons(state, late, 5000)
        self.assertEqual(reel_factory._neurons_left(state, late), reel_art.DAILY_NEURONS - 5000)
        self.assertEqual(reel_factory._neurons_left(state, late + timedelta(hours=2)), reel_art.DAILY_NEURONS)
        reel_factory._add_neurons(state, late, 5000)
        self.assertEqual(reel_factory._neurons_left(state, late), 0)

    def test_a_painting_steps_notes_name_no_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "ak999_pictures.log"
            log.write_text("\n".join((
                "12:05:01 INFO ytc.reel_art: line 9: SDXL-Lightning painted nothing (HTTPSConnectionPool(host="
                "'api.cloudflare.com', port=443): Max retries exceeded with url: /client/v4/accounts/abc123def/ai/run/x)",
                "12:05:02 INFO ytc.other: something else",
                "12:06:40 INFO ytc.llm: gemini-3-flash answered ak999 painted framing in 98s",
                "12:06:41 INFO ytc.reel_pictures: line 9: ai:ak999-09-5 set aside (two faces)",
                "12:06:42 INFO ytc.reel_art: painted lines [1, 4] of [1, 4, 9] (2 by the free models)")), encoding="utf-8")
            found = reel_factory.notes(log)
        self.assertEqual(len(found), 4)
        self.assertNotIn("abc123def", "\n".join(found))
        self.assertIn("/accounts/…/ai/run/x", found[0])
        self.assertEqual(reel_factory.notes(Path("/nonexistent/ak999_pictures.log")), [])

    def test_only_a_week_of_days_is_kept(self) -> None:
        state: dict = {}
        start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        for day in range(10):
            reel_factory._add_neurons(state, start + timedelta(days=day), 10)
        self.assertEqual(sorted(state["neurons"]), [f"2026-10-{d:02d}" for d in range(4, 11)])


class RefusedRescueTests(unittest.TestCase):
    """A rescue Cloudflare refused every painting for waits until it paints again; any other failed rescue is set
    aside."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.logs, self.notes = Path(tmp.name) / "logs", [reel_art.OUT_OF_NEURONS, reel_art.FREE_DOWN]
        (Path(tmp.name) / "content" / "ak902").mkdir(parents=True)
        for patcher in (
                patch.object(reels, "REELS", Path(tmp.name) / "content"),
                patch.object(reel_factory, "_steps", return_value=[("rescue", lambda: False, ["true"], 60)]),
                patch.object(reel_factory, "_run", side_effect=self._run),
                patch.object(reel_art, "ready", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = {"reels": {"ak902": {"fails": {}, "transient": {}, "rescue": {
            "at": "2026-10-21T06:33:00+05:30", "pushed": "2026-10-20T21:35:20+00:00"}}}, "made": {}}

    def _run(self, command, log, timeout):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("line 2: Cloudflare refused FLUX.2 klein for the day's neurons\n", encoding="utf-8")
        Path(os.environ["YTC_USAGE_LOG"]).write_text("".join(json.dumps({"model": "cloudflare:refused", note: True})
                                                             + "\n" for note in self.notes), encoding="utf-8")
        return 1, "ValueError: only 3 of its pictures weren't faulted; at least 8 are needed"

    def test_a_rescue_cloudflare_refused_waits_among_the_failed_reels(self) -> None:
        result = reel_factory.make("ak902", self.state, self.logs, {"steps": [], "alerts": []})
        mine = self.state["reels"]["ak902"]
        self.assertEqual(result, "failed")
        self.assertEqual((mine.get("rescue"), mine.get("set_aside"), mine["pushed"]),
                         (None, None, "2026-10-20T21:35:20+00:00"))
        now = datetime.now(timezone.utc)
        self.assertEqual((reel_factory._neurons_left(self.state, now), reel_factory._free_art(self.state, now)),
                         (0, False))
        self.assertTrue(reel_factory._free_art(self.state, now + reel_factory.FREE_WAIT + timedelta(minutes=1)))

    def test_a_rescue_that_fails_for_another_reason_is_set_aside(self) -> None:
        self.notes = []
        self.assertEqual(reel_factory.make("ak902", self.state, self.logs, {"steps": [], "alerts": []}), "set aside")
        self.assertIn("set_aside", self.state["reels"]["ak902"])

    def test_klein_refused_with_the_free_models_up_is_another_reason(self) -> None:
        self.notes = [reel_art.OUT_OF_NEURONS]
        self.assertEqual(reel_factory.make("ak902", self.state, self.logs, {"steps": [], "alerts": []}), "set aside")
        self.assertTrue(reel_factory._can_paint(self.state, datetime.now(timezone.utc)))

    def test_the_painting_steps_are_told_whether_the_free_models_may_paint(self) -> None:
        seen, run, self.notes = [], self._run, []
        reel_factory._run.side_effect = lambda *args: (seen.append(os.environ.get("YTC_FREE_ART")), run(*args))[1]
        reel_factory.make("ak902", self.state, self.logs, {"steps": [], "alerts": []})
        reel_factory.make("ak902", self.state | DOWN, self.logs, {"steps": [], "alerts": []})
        self.assertEqual(seen, ["1", "0"])
        self.assertNotIn("YTC_FREE_ART", os.environ)


class WaitingPicturesTests(unittest.TestCase):
    """A picture step short of pictures while the day's neurons are out and the free models down waits for them
    instead of using up its tries, and the factory takes it up again once it can paint."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.logs = Path(tmp.name) / "logs"
        (Path(tmp.name) / "content" / "ak903").mkdir(parents=True)
        for patcher in (
                patch.object(reels, "REELS", Path(tmp.name) / "content"),
                patch.object(reel_factory, "_steps", return_value=[("pictures", lambda: False, ["true"], 60)]),
                patch.object(reel_factory, "_run", side_effect=self._run),
                patch.object(reel_art, "ready", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        today = f"{datetime.now(timezone.utc):%Y-%m-%d}"
        self.state = {"reels": {"ak903": {"fails": {}, "transient": {}}}, "made": {},
                      "neurons": {today: reel_art.DAILY_NEURONS}} | DOWN
        self.free_down = False

    def _run(self, command, log, timeout):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("no painting for lines [2, 3]: today's 6000 neurons are spent\n", encoding="utf-8")
        if self.free_down:
            Path(os.environ["YTC_USAGE_LOG"]).write_text(json.dumps(
                {"model": "cloudflare:free-refused", reel_art.FREE_DOWN: True}) + "\n", encoding="utf-8")
        return 1, "LookupError: no usable picture for lines [2, 3, 4, 5, 6, 7, 9, 10]"

    def _make(self) -> str:
        return reel_factory.make("ak903", self.state, self.logs, {"steps": [], "alerts": []})

    def test_short_of_pictures_while_the_neurons_are_out_it_waits(self) -> None:
        self.assertEqual(self._make(), "failed")
        mine = self.state["reels"]["ak903"]
        self.assertEqual((mine.get("waits"), mine["fails"], mine["transient"]), ("paintings", {}, {"pictures": 1}))
        self.assertEqual(reel_factory.supply(self.state, datetime.now(timezone.utc))["waiting"], ["ak903"])

    def test_with_neurons_left_it_is_a_failed_try(self) -> None:
        self.state["neurons"] = {}
        self.assertEqual(self._make(), "failed")
        mine = self.state["reels"]["ak903"]
        self.assertEqual((mine.get("waits"), mine["fails"]), (None, {"pictures": 1}))

    def test_with_the_free_models_up_it_is_a_failed_try_without_neurons_too(self) -> None:
        self.state.pop("free_down")
        self.assertEqual(self._make(), "failed")
        mine = self.state["reels"]["ak903"]
        self.assertEqual((mine.get("waits"), mine["fails"]), (None, {"pictures": 1}))

    def test_free_models_that_painted_nothing_make_it_wait_and_rest_for_free_wait(self) -> None:
        self.state.pop("free_down")
        self.free_down = True
        self.assertEqual(self._make(), "failed")
        self.assertEqual(self.state["reels"]["ak903"].get("waits"), "paintings")
        until = datetime.fromisoformat(self.state["free_down"]) - datetime.now(timezone.utc)
        self.assertLess(abs(until - reel_factory.FREE_WAIT), timedelta(minutes=1))

    def test_without_cloudflare_it_is_a_failed_try(self) -> None:
        with patch.object(reel_art, "ready", return_value=False):
            self._make()
        self.assertEqual(self.state["reels"]["ak903"]["fails"], {"pictures": 1})

    def test_no_more_than_waiting_max_wait(self) -> None:
        self.state["reels"] |= {f"ak8{n}": {"fails": {}, "waits": "paintings"} for n in range(reel_factory.WAITING_MAX)}
        with patch.object(reel_art, "CINEMATIC", False):
            self._make()
        mine = self.state["reels"]["ak903"]
        self.assertEqual((mine.get("waits"), mine["fails"]), (None, {"pictures": 1}))

    def test_painted_line_by_line_every_story_short_of_neurons_waits(self) -> None:
        self.state["reels"] |= {f"ak8{n}": {"fails": {}, "waits": "paintings"} for n in range(reel_factory.WAITING_MAX)}
        self.state["reels"]["ak903"]["transient"] = {"pictures": reel_factory.TRANSIENT_GIVE_UP}
        with patch.object(reel_art, "CINEMATIC", True):
            self.assertEqual(self._make(), "failed")
        mine = self.state["reels"]["ak903"]
        self.assertEqual((mine.get("waits"), mine["fails"], mine.get("set_aside")), ("paintings", {}, None))

    def test_painted_line_by_line_nothing_is_started_or_pictured_again_without_neurons(self) -> None:
        found = {"booked": [], "pending": [], "failed": ["ak904"], "stuck": [], "unfinished": [], "waiting": []}
        now = datetime(2026, 10, 24, 21, 3, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(reel_factory, "PAUSE", Path(tmp) / "FACTORY_PAUSE"), \
                patch.object(reel_factory, "supply", return_value=found), \
                patch.object(reel_factory, "_stalled", return_value=False), \
                patch.object(reel_factory, "readable_left", return_value=10), \
                patch.object(reel_factory, "_pictures_failed", return_value=True), \
                patch.object(reel_factory.reel_rescue, "to_render", return_value=[]), \
                patch.object(reel_factory, "waits_for_judge", return_value=""), \
                patch.object(reel_factory.reel_writer, "next_story", return_value=({"story": "x"}, [])), \
                patch.object(reel_factory.reel_writer, "next_id", return_value="ak905"), \
                patch.object(reel_art, "ready", return_value=True), \
                patch.object(reel_art, "CINEMATIC", True), \
                patch.object(reel_factory, "MUSEUM_WHEN_IDLE", False):
            spent = {"reels": {}, "made": {}, "neurons": {"2026-10-24": reel_art.DAILY_NEURONS}}
            reel, done = reel_factory.decide(spent | DOWN, now, False)
            self.assertEqual(reel, None)
            self.assertIn("neurons are spent and the free models are down until 05:30 IST", done["decision"])
            reel, done = reel_factory.decide(spent, now, False)
            self.assertEqual((reel, done["decision"]), ("ak904", "picturing again"))
            reel, done = reel_factory.decide({"reels": {}, "made": {}, "neurons": {}} | DOWN, now, False)
            self.assertEqual((reel, done["decision"]), ("ak904", "picturing again"))
            found["failed"] = []
            reel, done = reel_factory.decide({"reels": {}, "made": {}, "neurons": {}}, now, False)
        self.assertEqual((reel, done["decision"]), ("ak905", "starting"))

    def test_taken_up_again_once_the_neurons_are_back(self) -> None:
        found = {"booked": [], "pending": [f"ak0{n}" for n in range(10, 20)], "failed": [], "stuck": [],
                 "unfinished": [], "waiting": ["ak903"]}
        now = datetime(2026, 10, 22, 0, 33, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(reel_factory, "PAUSE", Path(tmp) / "FACTORY_PAUSE"), \
                patch.object(reel_factory, "supply", return_value=found), \
                patch.object(reel_factory, "readable_left", return_value=10):
            spent = {"reels": {}, "made": {}, "neurons": {"2026-10-22": reel_art.DAILY_NEURONS}}
            reel, done = reel_factory.decide(spent | DOWN, now, False)
            self.assertEqual((reel, done["decision"]), (None, "supply is enough"))
            reel, done = reel_factory.decide({"reels": {}, "made": {}, "neurons": {}} | DOWN, now, False)
            self.assertEqual((reel, done["decision"]), ("ak903", "resuming with paintings"))
            reel, done = reel_factory.decide(spent, now, False)
        self.assertEqual((reel, done["decision"]), ("ak903", "resuming with paintings"))

    def test_resuming_ends_the_wait(self) -> None:
        self._make()
        self.state["neurons"] = {}
        self._make()
        mine = self.state["reels"]["ak903"]
        self.assertEqual((mine.get("waits"), mine["fails"]), (None, {"pictures": 1}))


class DecideTests(unittest.TestCase):
    """The factory rescues a Reel with paintings only after it couldn't be rescued or sent back without them."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.passes: list[int] = []
        self.offered: list[list[str]] = []
        found = {"booked": [], "pending": [], "failed": ["ak902"], "stuck": [], "unfinished": []}
        for patcher in (
                patch.object(reel_factory, "PAUSE", Path(tmp.name) / "FACTORY_PAUSE"),
                patch.object(reel_factory, "supply", return_value=found),
                patch.object(reel_factory, "_stalled", return_value=False),
                patch.object(reel_factory, "readable_left", return_value=10),
                patch.object(reel_factory, "_pictures_failed", return_value=False),
                patch.object(reel_factory.reel_writer, "next_story", return_value=None),
                patch.object(reel_art, "ready", return_value=True),
                patch.object(reel_rescue, "to_render", side_effect=self._to_render)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = datetime(2026, 10, 20, 14, 3, tzinfo=timezone.utc)

    def _to_render(self, failed, stored=None, paintings=0):
        self.passes.append(paintings)
        self.offered.append(list(failed))
        return ["ak902"] if paintings and "ak902" in failed else []

    def test_a_reel_that_needs_paintings_is_rescued_while_the_factory_can_paint(self) -> None:
        spent = {"reels": {}, "made": {}, "neurons": {"2026-10-20": reel_art.DAILY_NEURONS}}
        for state, paintings in ((spent, reel_art.MAX_PAINTED),
                                 ({"reels": {}, "made": {}} | DOWN, reel_art.DAILY_NEURONS // reel_art.NEURONS_EACH)):
            self.passes = []
            reel, done = reel_factory.decide(state, self.now, False)
            self.assertEqual((reel, done["decision"]), ("ak902", "rescuing"))
            self.assertEqual(self.passes, [0, paintings])

    def test_no_paint_pass_once_todays_neurons_are_spent_and_the_free_models_are_down(self) -> None:
        state = {"reels": {}, "made": {}, "neurons": {"2026-10-20": reel_art.DAILY_NEURONS}} | DOWN
        reel, done = reel_factory.decide(state, self.now, False)
        self.assertEqual(reel, None)
        self.assertEqual(self.passes, [0])

    def test_a_day_at_the_cap_still_rescues(self) -> None:
        reel, done = reel_factory.decide({"reels": {}, "made": {"2026-10-20": reel_factory.DAILY_CAP}}, self.now, False)
        self.assertEqual((reel, done["decision"]), ("ak902", "rescuing"))

    def test_a_day_at_the_cap_finishes_a_reel_already_started(self) -> None:
        found = {"booked": [], "pending": [], "failed": [], "stuck": [], "unfinished": ["ak903"]}
        with patch.object(reel_factory, "supply", return_value=found):
            reel, done = reel_factory.decide({"reels": {}, "made": {"2026-10-20": reel_factory.DAILY_CAP}}, self.now,
                                             False)
        self.assertEqual((reel, done["decision"]), ("ak903", "resuming"))

    def test_a_day_at_the_cap_sends_nothing_to_the_judge_and_paints_no_reel_that_can_go_back(self) -> None:
        state = {"reels": {}, "made": {"2026-10-20": reel_factory.DAILY_CAP}}
        with patch.object(reel_factory, "_pictures_failed", return_value=True), \
                patch.object(reel_factory.reel_writer, "next_story", return_value=("a story",)):
            reel, done = reel_factory.decide(state, self.now, False)
        self.assertEqual((reel, done["decision"]), (None, f"already made {reel_factory.DAILY_CAP} today"))
        self.assertEqual(self.offered, [["ak902"], []])


if __name__ == "__main__":
    unittest.main()
