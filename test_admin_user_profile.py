import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
import main
import storage


class _Request:
    def __init__(self, cookies=None, data=None):
        self.cookies = cookies or {}
        self.state = SimpleNamespace()
        self._data = data or {}

    async def json(self):
        return self._data


class AdminUserProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_profile_contains_app_activity_and_team(self):
        storage.save_token("user-1", "token")
        storage.set_user_uid("user-1", "1001")
        storage.save_known_uid("1001", "user-1", "Ivan Ivanov", "BV3")
        storage.ensure_coins("1001")
        storage.add_watch("user-1", "event-1", "Event")

        profile = storage.get_admin_user_profile("1001")

        self.assertEqual(profile["full_name"], "Ivan Ivanov")
        self.assertEqual(profile["team"], "BV3")
        self.assertEqual(profile["watching_count"], 1)
        self.assertEqual(profile["registered_count"], 0)

    def test_team_is_taken_from_schedule_groups(self):
        storage.save_token("user-1", "token")
        storage.set_user_uid("user-1", "1001")
        storage.save_known_uid("1001", "user-1", "Ivan Ivanov")
        events = [
            SimpleNamespace(unions=["BV3"]),
            SimpleNamespace(unions=["BV3", "BV4"]),
            SimpleNamespace(unions=["BV4"]),
        ]

        main._save_schedule_team("user-1", events)

        self.assertEqual(storage.get_admin_user_profile("1001")["team"], "BV3")
        self.assertEqual(storage.get_known_team("1001"), "BV3")

    async def test_admin_profile_returns_watches_and_request_statistics(self):
        storage.save_token("admin", "verified-token")
        storage.set_user_uid("admin", "admin")
        storage.mark_token_verified("admin", "verified-token")
        storage.add_admin("admin")
        session_id = storage.create_session_for_user("admin")
        storage.save_token("user-1", "token")
        storage.set_user_uid("user-1", "1001")
        storage.save_known_uid("1001", "user-1", "Ivan Ivanov", "BV3")
        storage.add_watch("user-1", "event-1", "Morning training", snipe_priority="medium")
        storage.increment_sirius_request("user-1")
        storage.increment_sirius_request("user-1")

        response = await main.api_admin_user_profile("1001", _Request({"session_id": session_id}))
        payload = json.loads(response.body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["user"]["team"], "BV3")
        self.assertEqual(payload["user"]["siriusRequestCount"], 2)
        self.assertEqual(payload["user"]["watches"][0]["eventName"], "Morning training")

    async def test_admin_message_creates_user_feedback_thread(self):
        storage.save_token("admin", "verified-token")
        storage.set_user_uid("admin", "admin")
        storage.mark_token_verified("admin", "verified-token")
        storage.add_admin("admin")
        session_id = storage.create_session_for_user("admin")
        storage.save_token("user-1", "token")
        storage.set_user_uid("user-1", "1001")
        storage.save_known_uid("1001", "user-1", "Ivan Ivanov", "BV3")
        storage.save_known_uid("admin", "admin", "Admin Name")

        request = _Request({"session_id": session_id}, {"message": "Check this event"})
        with patch.object(main, "web_notify", new=AsyncMock()) as notify:
            response = await main.api_admin_message_user("1001", request)

        self.assertEqual(response.status_code, 200)
        feedback = storage.get_user_feedback_messages("user-1")[0]
        self.assertEqual(feedback["initiated_by"], "admin")
        self.assertEqual(feedback["initiator_name"], "Admin Name")
        notify.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
