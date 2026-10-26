import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ytc import auto, publish


def _episode(state: str, due: datetime | None = None, fmt: int = 2) -> dict:
    record = {"status": "scheduled", "due_at": due.isoformat()} if due else {}
    return {"id": "ep000", "state": state, "record": record, "held": {}, "topic": {}, "remote": {}, "format": fmt}


class InventoryTests(unittest.TestCase):
    """Days of Odd's stock: what Buffer has yet to send and what waits for a slot."""

    def setUp(self) -> None:
        self.now = datetime(2026, 10, 25, 3, 0, tzinfo=auto.IST)
        patcher = patch.object(auto, "_now", return_value=self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_post_already_due_is_not_stock_though_no_sync_marked_it_sent(self) -> None:
        eps = [_episode("scheduled", self.now - timedelta(days=3)), _episode("scheduled", self.now - timedelta(hours=1)),
               _episode("scheduled", self.now + timedelta(days=1)), _episode("waiting")]
        found = auto.inventory(eps)
        self.assertEqual((found["total"], found["in_buffer"], found["waiting"]), (2, 1, 1))

    def test_the_studio_wants_shorts_again_once_the_stale_queue_has_gone_out(self) -> None:
        eps = [_episode("scheduled", self.now - timedelta(days=n)) for n in range(1, 10)]
        eps += [_episode("scheduled", self.now + timedelta(days=1)), _episode("waiting")]
        self.assertGreaterEqual(auto.INVENTORY_TARGET - auto.inventory(eps)["total"], auto.BATCH)


class PaceTests(unittest.TestCase):
    """How many Shorts a day Days of Odd books: as many as the stock for the days ahead allows."""

    def test_a_short_kept_for_an_anniversary_days_off_does_not_double_up_the_days_before_it(self) -> None:
        today = datetime.now(publish.AUDIENCE_TZ).date()
        due = {f"p{n}": datetime.combine(today + timedelta(days=n), publish.SLOTS[0], publish.AUDIENCE_TZ)
               for n in (0, 1, 2, 5)}

        def episode(name: str, state: str, at: date | None = None, post: str | None = None) -> dict:
            return {"id": name, "title": name, "state": state, "format": auto.FORMAT, "series": "s",
                    "folder": Path("/nonexistent", name),
                    "record": {"buffer_post_id": post} if post else {}, "held": {"scores": {"hook": 5}},
                    "topic": {"at": at.isoformat()} if at else {}, "remote": {}}

        eps = [episode(f"ep{n}", "scheduled", post=f"p{n}") for n in (0, 1, 2)]
        eps += [episode("ep5", "scheduled", today + timedelta(days=5), "p5"), episode("ep7", "waiting", today + timedelta(days=7)),
                episode("ep9", "waiting")]
        queued = [{"id": post, "status": "scheduled", "dueAt": when.isoformat()} for post, when in due.items()]
        booked = {}
        with patch.object(auto, "episodes", return_value=eps), patch.object(auto, "slots_per_day", return_value=2), \
                patch.object(publish, "posts", return_value=queued), \
                patch.object(publish, "schedule", side_effect=lambda path, when: booked.setdefault(path.parent.name, when)
                             and {"due_at": when.isoformat()}):
            auto.publish_waiting(auto.Run("test"))
        self.assertEqual(booked["ep7"].date(), today + timedelta(days=7))
        self.assertEqual(booked["ep9"], datetime.combine(today + timedelta(days=3), publish.SLOTS[0], publish.AUDIENCE_TZ))


if __name__ == "__main__":
    unittest.main()
