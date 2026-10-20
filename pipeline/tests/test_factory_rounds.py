from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from ytc import reel_host


def _round(**done) -> dict:
    return {"reel": "ak071", "result": "made", "exit": 0, "errors": []} | done


class AnotherRoundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.until = datetime.now(timezone.utc) + timedelta(minutes=30)

    def test_a_made_set_aside_or_failed_reel_is_followed_by_another_round(self) -> None:
        for result in ("made", "set aside", "failed", "not sent back"):
            self.assertTrue(reel_host._another_round([_round(result=result)], self.until), result)

    def test_the_clock_the_quota_and_the_credits_end_the_job(self) -> None:
        for result in ("paused", "quota", "credits"):
            self.assertFalse(reel_host._another_round([_round(result=result)], self.until), result)

    def test_an_outage_a_crash_or_work_not_kept_waits_for_the_next_run(self) -> None:
        for done in ({"result": "failed", "transient": True}, {"exit": 124}, {"errors": ["not shipped"]}):
            self.assertFalse(reel_host._another_round([_round(**done)], self.until), done)

    def test_nothing_to_make_no_time_or_enough_rounds_ends_the_job(self) -> None:
        self.assertFalse(reel_host._another_round([_round(reel=None, result=None)], self.until))
        self.assertFalse(reel_host._another_round([_round()], datetime.now(timezone.utc) + timedelta(minutes=4)))
        self.assertFalse(reel_host._another_round([_round()] * reel_host.ROUNDS, self.until))
        self.assertFalse(reel_host._another_round([_round()], None))


class FactoryTests(unittest.TestCase):
    def _run(self, growth_due: bool, results: list[str]) -> tuple[dict, list[datetime | None]]:
        untils: list[datetime | None] = []

        def one_round(store, saved, force, until, number):
            untils.append(until)
            return saved, _round(result=results[number - 1], round=number)

        with patch.object(reel_host, "_started", return_value=object()), \
                patch.object(reel_host, "_read", return_value={}), \
                patch.object(reel_host, "_growth_due", return_value=growth_due), \
                patch.object(reel_host, "_factory_round", side_effect=one_round):
            return reel_host.factory(until=self.until), untils

    def setUp(self) -> None:
        self.until = datetime.now(timezone.utc) + timedelta(minutes=45)

    def test_rounds_go_on_until_one_is_stopped(self) -> None:
        done, untils = self._run(False, ["failed", "made", "paused"])
        self.assertEqual([r["result"] for r in done["rounds"]], ["failed", "made", "paused"])
        self.assertEqual(untils, [self.until] * 3)

    def test_the_rounds_leave_the_growth_desk_its_time_when_it_is_due(self) -> None:
        _, untils = self._run(True, ["paused"])
        self.assertEqual(untils, [self.until - reel_host.GROWTH_NEEDS])


if __name__ == "__main__":
    unittest.main()
