import tempfile
import unittest
from pathlib import Path

import config
import storage


class LoginCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_login_code_is_one_time(self):
        code, expires_at = storage.create_login_code("user-1")

        self.assertGreater(expires_at, 0)
        self.assertEqual(storage.consume_login_code(code), "user-1")
        self.assertIsNone(storage.consume_login_code(code))

    def test_new_login_code_invalidates_previous_user_code(self):
        old_code, _ = storage.create_login_code("user-1")
        new_code, _ = storage.create_login_code("user-1")

        self.assertIsNone(storage.consume_login_code(old_code))
        self.assertEqual(storage.consume_login_code(new_code), "user-1")


if __name__ == "__main__":
    unittest.main()
