import tempfile
import unittest
from pathlib import Path

import config
import dronebet
import storage


class DroneBetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()
        storage.ensure_coins("alice")
        storage.add_coins("alice", 7)

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    async def test_creates_once_and_resolves_from_radar_duration(self):
        notifications = []

        async def notify(uid, text, kind):
            notifications.append((uid, text, kind))

        active = {
            "active": True,
            "updated_at": "2026-07-13T10:00:00Z",
            "event": {"id": "radar-1", "message": "Угроза", "source_url": "https://t.me/ok_sos/1"},
        }
        await dronebet.sync_radar_state(active, notify)
        await dronebet.sync_radar_state(active, notify)

        alert = storage.get_active_drone_alert()
        self.assertIsNotNone(alert)
        self.assertEqual(len(storage.get_drone_alerts(include_active=True)), 1)
        market_id = int(alert["market_id"])
        self.assertTrue(storage.place_prediction_bet("alice", market_id, "30–60 минут", None, 2)[0])

        clear = {
            "active": False,
            "updated_at": "2026-07-13T10:35:00Z",
            "event": {"id": "radar-2", "message": "Отбой"},
        }
        await dronebet.sync_radar_state(clear, notify)

        history = storage.get_drone_alerts()
        self.assertFalse(storage.get_drone_radar_state()["active"])
        self.assertEqual(history[0]["result_option"], "30–60 минут")
        self.assertEqual(storage.get_prediction_market(market_id)["status"], "resolved")
        self.assertEqual(storage.get_prediction_markets(), [])
        self.assertEqual(storage.get_coins("alice"), 10)


if __name__ == "__main__":
    unittest.main()
