from __future__ import annotations

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
    """How many lines a picture try or a rescue gets painted."""

    def setUp(self) -> None:
        env = patch.dict(os.environ, KEYS)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("YTC_PAINT_NEURONS", None)
        self.beats = [{"text": f"line {n}", "picture": f"brief {n}"} for n in range(1, 13)]
        self.out = set()
        painter = patch.object(reel_art, "paint", side_effect=self._paint)
        painter.start()
        self.addCleanup(painter.stop)

    def _paint(self, reel, number, beat):
        if number in self.out:
            raise reel_art.OutOfNeurons("daily free allocation exceeded")
        return {"ref": f"ai:{reel}-{number:02d}-1"}

    def test_nothing_is_painted_without_the_cloudflare_keys(self) -> None:
        with patch.dict(os.environ, {"IG_CLOUDFLARE_API_TOKEN": ""}):
            self.assertEqual(reel_art.paint_lines("ak999", self.beats, [1, 2]), {})
        reel_art.paint.assert_not_called()

    def test_a_try_paints_at_most_max_painted_lines_the_most_needed_first(self) -> None:
        made = reel_art.paint_lines("ak999", self.beats, list(range(12, 0, -1)))
        self.assertEqual(sorted(made), list(range(12, 12 - reel_art.MAX_PAINTED, -1))[::-1])

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


class PaintTests(unittest.TestCase):
    """One painting: made, hosted, credited as AI, and its neurons counted."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.usage = Path(tmp.name) / "usage.jsonl"
        env = patch.dict(os.environ, KEYS | {"YTC_USAGE_LOG": str(self.usage)})
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

    def test_a_painting_is_hosted_and_credited_as_made_with_ai(self) -> None:
        with patch.object(reel_art, "_run", return_value=(self.jpeg, 131.88)):
            hit = reel_art.paint("ak999", 3, {"picture": "Sage Durvasa blesses young Kunti"})
        self.assertFalse(self.hosted[0].exists(), "the painting's local copy was left behind")
        self.assertTrue(hit["ref"].startswith("ai:ak999-03-"))
        self.assertEqual((hit["source"], hit["license"], hit["artist"]), ("AI", "AI-generated", "FLUX.2 klein"))
        self.assertEqual((hit["width"], hit["height"]), (reel_art.WIDTH, reel_art.HEIGHT))
        self.assertIn(f"{reel_art.CLOUDINARY_FOLDER}/ak999-03-", hit["url"])
        self.assertEqual(json.loads(self.usage.read_text(encoding="utf-8"))["neurons"], 131.88)

    def test_flux_1_schnell_paints_when_flux_2_klein_fails(self) -> None:
        def run(model, text, seed):
            if "klein" in model:
                raise requests.HTTPError("500 Server Error")
            return self.jpeg, 40.0

        with patch.object(reel_art, "_run", side_effect=run):
            self.assertEqual(reel_art.paint("ak999", 3, {"picture": "Kunti at the river"})["artist"], "FLUX.1 schnell")

    def test_no_painting_when_both_models_fail(self) -> None:
        with patch.object(reel_art, "_run", side_effect=requests.ConnectionError("down")):
            self.assertIsNone(reel_art.paint("ak999", 3, {"picture": "Kunti at the river"}))

    def test_cloudflare_refusing_for_the_days_neurons_raises(self) -> None:
        with patch.object(reel_art.requests, "post", return_value=MagicMock(status_code=429, text="Too Many Requests")):
            with self.assertRaises(reel_art.OutOfNeurons):
                reel_art._run("@cf/black-forest-labs/flux-2-klein-4b", "a painting", 1)

    def test_the_figures_a_brief_names_are_painted_as_the_old_paintings_show_them(self) -> None:
        text = reel_art.prompt({"picture": "Pahari painting of Yamaraja bowing deeply before Krishna and Balarama."})
        self.assertTrue(text.startswith("Pahari painting of Yamaraja bowing deeply before Krishna and Balarama. "
                                        "Yama, god of death, is dark-skinned"))
        self.assertLess(text.index("Krishna has blue skin"), text.index("Balarama has fair skin"))
        self.assertNotIn("Rama has", reel_art.icons("Scenes from the Ramayana and the life of Parashurama"))
        self.assertEqual(reel_art.icons("Siva and Brahma, Vishnu, Indra"), "Shiva has pale ash-smeared skin, matted "
                         "locks with the crescent moon, a snake at his neck and a trident. Brahma has four bearded "
                         "heads. Vishnu has blue skin and four arms holding a conch, a discus, a mace and a lotus. ")

    def test_line_1_asks_for_a_close_vivid_figure_and_every_line_for_the_style(self) -> None:
        first, other = (reel_art.prompt({"picture": "Kunti at the river."}, n == 1) for n in (1, 2))
        self.assertTrue(first.startswith("Kunti at the river. The figure large and close"))
        self.assertTrue(other.startswith("Kunti at the river. Indian miniature painting"))
        self.assertIn("never a crown", other)


class PictureStepTests(unittest.TestCase):
    """Which lines the picture step paints, and how the caption credits the paintings."""

    def test_line_1_first_then_lines_without_a_picture_then_pictures_of_another_scene(self) -> None:
        beats = [{"text": f"line {n}", "picture": f"brief {n}", "role": "story"} for n in range(1, 7)]
        chosen = {1: {"ref": "m1"}, 2: {"ref": "m2"}, 4: {"ref": "m4"}, 5: {"ref": "ai:ak999-05-1"}, 6: {"ref": "m6"}}
        fits = {1: "elsewhere", 2: "moment", 4: "none", 5: "elsewhere", 6: "figure"}
        with patch.object(reel_pictures, "paint_framed", return_value={}) as paint:
            reel_pictures.painted("ak999", beats, chosen, fits)
        self.assertEqual(paint.call_args.args[2], [1, 3, 4])

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

    def test_only_a_week_of_days_is_kept(self) -> None:
        state: dict = {}
        start = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        for day in range(10):
            reel_factory._add_neurons(state, start + timedelta(days=day), 10)
        self.assertEqual(sorted(state["neurons"]), [f"2026-10-{d:02d}" for d in range(4, 11)])


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

    def test_a_reel_that_needs_paintings_is_rescued_while_the_neurons_last(self) -> None:
        reel, done = reel_factory.decide({"reels": {}, "made": {}}, self.now, False)
        self.assertEqual((reel, done["decision"]), ("ak902", "rescuing"))
        self.assertEqual(self.passes, [0, reel_art.DAILY_NEURONS // reel_art.NEURONS_EACH])

    def test_no_paint_pass_once_todays_neurons_are_spent(self) -> None:
        state = {"reels": {}, "made": {}, "neurons": {"2026-10-20": reel_art.DAILY_NEURONS}}
        reel, done = reel_factory.decide(state, self.now, False)
        self.assertEqual((reel, done["decision"]), (None, "no readable story"))
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
