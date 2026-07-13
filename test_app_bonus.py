import tempfile
import unittest
from pathlib import Path

import config
import storage


class AppBonusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()
        storage.ensure_coins("android-user")

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_app_bonus_is_credited_once(self):
        self.assertTrue(storage.claim_app_usage_bonus("android-user"))
        self.assertEqual(storage.get_coins_total("android-user"), storage.STARTING_COINS + 2)
        self.assertFalse(storage.claim_app_usage_bonus("android-user"))
        self.assertEqual(storage.get_coins_total("android-user"), storage.STARTING_COINS + 2)


if __name__ == "__main__":
    unittest.main()
