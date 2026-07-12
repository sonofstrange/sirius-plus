import tempfile
import time
import unittest
from pathlib import Path

import config
import storage


class PolymarketStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()
        for uid in ("alice", "bob", "carol"):
            storage.ensure_coins(uid)
            storage.add_coins(uid, 7)

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_choice_market_splits_full_pool_between_winners(self):
        market_id = storage.create_prediction_market(
            "Будет ли дождь?", "", "choice", '["Да", "Нет"]', None, None, 0, 0, "admin"
        )
        self.assertTrue(storage.place_prediction_bet("alice", market_id, "Да", None, 2)[0])
        self.assertTrue(storage.place_prediction_bet("bob", market_id, "Нет", None, 1)[0])

        ok, error, payouts = storage.resolve_prediction_market(market_id, correct_option="Да")

        self.assertTrue(ok, error)
        self.assertEqual(dict(payouts), {"alice": 3, "bob": 0})
        self.assertEqual(storage.get_coins("alice"), 11)
        self.assertEqual(storage.get_coins("bob"), 9)
        self.assertFalse(storage.resolve_prediction_market(market_id, correct_option="Да")[0])

    def test_number_market_rewards_closer_prediction_more(self):
        market_id = storage.create_prediction_market(
            "Сколько?", "", "number", "[]", 0, 100, 0, 0, "admin"
        )
        for uid, value, amount in (("alice", 50, 2), ("bob", 0, 1), ("carol", 100, 1)):
            self.assertTrue(storage.place_prediction_bet(uid, market_id, str(value), float(value), amount)[0])

        ok, error, payouts = storage.resolve_prediction_market(market_id, correct_value=50)

        self.assertTrue(ok, error)
        payout_by_uid = dict(payouts)
        self.assertGreater(payout_by_uid["alice"], payout_by_uid["bob"])
        self.assertGreater(payout_by_uid["alice"], payout_by_uid["carol"])
        self.assertEqual(sum(payout_by_uid.values()), 4)

    def test_betting_close_blocks_new_bets(self):
        market_id = storage.create_prediction_market(
            "Поздняя ставка", "", "choice", '["A", "B"]', None, None, 0, int(time.time()) - 1, "admin"
        )

        ok, error, balance = storage.place_prediction_bet("alice", market_id, "A", None, 1)

        self.assertFalse(ok)
        self.assertEqual(error, "Приём ставок уже закрыт")
        self.assertEqual(balance, 0)
        self.assertEqual(storage.get_coins("alice"), 10)

    def test_cancelling_market_refunds_every_bet(self):
        market_id = storage.create_prediction_market(
            "Отмена", "", "choice", '["A", "B"]', None, None, 0, 0, "admin"
        )
        storage.place_prediction_bet("alice", market_id, "A", None, 2)
        storage.place_prediction_bet("bob", market_id, "B", None, 3)

        ok, error, refunds = storage.cancel_prediction_market(market_id)

        self.assertTrue(ok, error)
        self.assertEqual(dict(refunds), {"alice": 2, "bob": 3})
        self.assertEqual(storage.get_coins("alice"), 10)
        self.assertEqual(storage.get_coins("bob"), 10)


if __name__ == "__main__":
    unittest.main()
