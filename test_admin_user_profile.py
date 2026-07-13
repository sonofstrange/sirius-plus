import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace

import config
import main
import storage


class _Request:
    cookies = {}
    state = SimpleNamespace()


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
            SimpleNamespace(unions=["БВ3"]),
            SimpleNamespace(unions=["БВ3", "БВ4"]),
            SimpleNamespace(unions=["БВ4"]),
        ]

        main._save_schedule_team("user-1", events)

        self.assertEqual(storage.get_admin_user_profile("1001")["team"], "БВ3")

    async def test_admin_profile_api_requires_verified_admin_session(self):
        storage.save_token("admin", "verified-token")
        storage.set_user_uid("admin", "admin")
        storage.mark_token_verified("admin", "verified-token")
        storage.add_admin("admin")
        session_id = storage.create_session_for_user("admin")
        storage.save_token("user-1", "token")
        storage.set_user_uid("user-1", "1001")
        storage.save_known_uid("1001", "user-1", "Ivan Ivanov", "BV3")

        request = _Request()
        request.cookies = {"session_id": session_id}
        response = await main.api_admin_user_profile("1001", request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["user"]["team"], "BV3")


if __name__ == "__main__":
    unittest.main()
