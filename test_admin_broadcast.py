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


class AdminBroadcastTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _admin_session(self):
        storage.save_token("admin", "verified-token")
        storage.set_user_uid("admin", "admin")
        storage.mark_token_verified("admin", "verified-token")
        storage.add_admin("admin")
        return storage.create_session_for_user("admin")

    async def test_broadcast_notifies_each_canonical_account_once(self):
        session_id = self._admin_session()
        storage.save_token("user-1", "token-1")
        storage.set_user_uid("user-1", "1001")
        storage.migrate_user_data("user-1", "1001")
        storage.save_token("1002", "token-2")
        storage.set_user_uid("1002", "1002")

        with patch.object(main, "web_notify", new=AsyncMock()) as notify:
            response = await main.api_admin_broadcast(
                _Request({"session_id": session_id}, {"message": "Сегодня обновление"})
            )

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["sent"], 3)
        self.assertEqual(notify.await_count, 3)
        self.assertEqual(
            {call.args[0] for call in notify.await_args_list}, {"admin", "1001", "1002"}
        )
        self.assertTrue(all(call.args[1] == "📣 Сегодня обновление" for call in notify.await_args_list))

    async def test_broadcast_rejects_empty_message(self):
        response = await main.api_admin_broadcast(
            _Request({"session_id": self._admin_session()}, {"message": "   "})
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
