import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from ytc import auto


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


if __name__ == "__main__":
    unittest.main()
