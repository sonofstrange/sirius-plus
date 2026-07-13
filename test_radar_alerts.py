import tempfile
import unittest
from pathlib import Path

import config
import sirius_radar
import storage


class RadarAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()
        for user_id in ("alice", "bob"):
            storage.save_token(user_id, f"token-{user_id}")

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    async def test_alert_is_broadcast_once_then_clear(self):
        sent = []

        async def notify(user_id, text, kind):
            sent.append((user_id, text, kind))

        active = {"active": True, "event": {"message": "Threat"}}
        self.assertTrue(await sirius_radar.process_radar_state(active, notify))
        self.assertEqual({entry[0] for entry in sent}, {"alice", "bob"})
        self.assertTrue(all(entry[2] == "alarm" for entry in sent))

        self.assertFalse(await sirius_radar.process_radar_state(active, notify))
        self.assertEqual(len(sent), 2)

        self.assertTrue(await sirius_radar.process_radar_state({"active": False}, notify))
        self.assertEqual(len(sent), 4)
        self.assertTrue(all(entry[2] == "info" for entry in sent[2:]))

    def test_mobile_push_token_moves_to_current_account(self):
        token = "fcm-token-12345678901234567890"
        storage.save_mobile_push_device("alice", token)
        self.assertEqual([row["token"] for row in storage.get_mobile_push_devices("alice")], [token])

        storage.save_mobile_push_device("bob", token)

        self.assertEqual(storage.get_mobile_push_devices("alice"), [])
        self.assertEqual([row["token"] for row in storage.get_mobile_push_devices("bob")], [token])
