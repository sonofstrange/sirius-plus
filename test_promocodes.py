import tempfile
import unittest
from pathlib import Path

import config
import storage


class PromocodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_promo_is_limited_and_single_use_per_account(self):
        self.assertEqual(storage.create_promo_code("test1", 3, 2, "admin"), "TEST1")

        self.assertEqual(storage.redeem_promo_code("test1", "user-a"), (True, "Промокод активирован: +3 Сириус Коинов", 6))
        self.assertFalse(storage.redeem_promo_code("TEST1", "user-a")[0])
        self.assertTrue(storage.redeem_promo_code("TEST1", "user-b")[0])
        self.assertFalse(storage.redeem_promo_code("TEST1", "user-c")[0])


if __name__ == "__main__":
    unittest.main()
