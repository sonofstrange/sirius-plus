import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import config
import main
import storage


class PartnerWalletStorageTests(unittest.TestCase):
    partner = "dronebet"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()
        storage.ensure_coins("sirius-user")
        storage.add_coins("sirius-user", 7)

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def _link(self, external_user_id="dronebet-user"):
        code, _ = storage.create_partner_link_code(self.partner, "sirius-user")
        ok, status, uid = storage.claim_partner_link_code(self.partner, code, external_user_id)
        self.assertTrue(ok, status)
        self.assertEqual(uid, "sirius-user")

    def test_link_code_is_one_use_and_links_accounts(self):
        code, _ = storage.create_partner_link_code(self.partner, "sirius-user")
        self.assertTrue(storage.claim_partner_link_code(self.partner, code, "dronebet-user")[0])
        self.assertEqual(
            storage.claim_partner_link_code(self.partner, code, "another-user")[1],
            "invalid_link_code",
        )
        self.assertEqual(
            storage.get_partner_link(self.partner, "dronebet-user")["uid"], "sirius-user"
        )

    def test_credit_is_idempotent(self):
        self._link()
        first = storage.partner_coin_transaction(
            self.partner, "dronebet-user", "credit", 3, "credit-operation-001"
        )
        repeat = storage.partner_coin_transaction(
            self.partner, "dronebet-user", "credit", 3, "credit-operation-001"
        )
        self.assertTrue(first["ok"])
        self.assertFalse(first["replayed"])
        self.assertTrue(repeat["ok"])
        self.assertTrue(repeat["replayed"])
        self.assertEqual(storage.get_coins("sirius-user"), 13)

    def test_debit_respects_reserved_coins(self):
        self._link()
        self.assertTrue(storage.reserve_coins("sirius-user", 8))
        result = storage.partner_coin_transaction(
            self.partner, "dronebet-user", "debit", 3, "debit-operation-001"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "insufficient_coins")
        self.assertEqual(result["balance"], 2)

    def test_same_idempotency_key_cannot_change_operation(self):
        self._link()
        storage.partner_coin_transaction(
            self.partner, "dronebet-user", "credit", 1, "conflict-key-001"
        )
        result = storage.partner_coin_transaction(
            self.partner, "dronebet-user", "debit", 1, "conflict-key-001"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "idempotency_key_conflict")


class _PartnerRequest:
    def __init__(self, data=None):
        self.headers = {"authorization": "Bearer test-partner-secret", "x-real-ip": "127.0.0.2"}
        self.client = SimpleNamespace(host="127.0.0.2")
        self.data = data or {}

    async def json(self):
        return self.data


class PartnerApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        self.old_token = config.DRONEBET_PARTNER_TOKEN
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        config.DRONEBET_PARTNER_TOKEN = "test-partner-secret"
        storage.init_db()
        storage.ensure_coins("sirius-user")

    def tearDown(self):
        config.DRONEBET_PARTNER_TOKEN = self.old_token
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    async def test_partner_can_claim_then_credit_linked_account(self):
        code, _ = storage.create_partner_link_code("dronebet", "sirius-user")
        claim = await main.partner_dronebet_claim_link(_PartnerRequest({
            "code": code, "external_user_id": "dronebet-user",
        }))
        self.assertEqual(claim.status_code, 200)

        credit = await main.partner_dronebet_credit(_PartnerRequest({
            "external_user_id": "dronebet-user",
            "amount": 2,
            "idempotency_key": "partner-api-credit-001",
        }))
        self.assertEqual(credit.status_code, 200)
        self.assertEqual(storage.get_coins("sirius-user"), 5)


if __name__ == "__main__":
    unittest.main()
