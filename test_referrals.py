import tempfile
import unittest
from pathlib import Path

import config
import storage


class ReferralTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_referral_rewards_both_accounts_once(self):
        storage.ensure_coins("referrer")
        storage.ensure_coins("new-user")
        code = storage.get_or_create_referral_code("referrer")

        self.assertTrue(storage.apply_referral(code.lower(), "new-user"))
        self.assertEqual(storage.get_coins_total("referrer"), storage.STARTING_COINS + 5)
        self.assertEqual(storage.get_coins_total("new-user"), storage.STARTING_COINS + 5)
        self.assertEqual(storage.get_referral_count("referrer"), 1)
        self.assertEqual(storage.get_referrer_uid("new-user"), "referrer")

        self.assertFalse(storage.apply_referral(code, "new-user"))
        self.assertEqual(storage.get_coins_total("referrer"), storage.STARTING_COINS + 5)
        self.assertFalse(storage.apply_referral(code, "referrer"))


if __name__ == "__main__":
    unittest.main()
