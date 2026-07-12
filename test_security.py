import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import config
import main
import storage


class _Request:
    headers = {"content-type": "application/json"}
    cookies = {}
    state = SimpleNamespace()

    def __init__(self, data):
        self.data = data
        self.state = SimpleNamespace()

    async def json(self):
        return self.data


class _RejectingSiriusClient:
    async def fetch_schedule(self, token):
        raise RuntimeError("HTTP 401")


class _InvalidJsonRequest(_Request):
    async def json(self):
        raise json.JSONDecodeError("invalid JSON", "{", 1)


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        self.old_client = main._sirius_client
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()

    def tearDown(self):
        main._sirius_client = self.old_client
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    async def test_forged_token_is_rejected_before_creating_session(self):
        payload = base64.urlsafe_b64encode(json.dumps({"id": "admin", "exp": 4_000_000_000}).encode()).rstrip(b"=").decode()
        main._sirius_client = _RejectingSiriusClient()

        response = await main.api_set_token(_Request({"token": f"header.{payload}.fake"}))

        self.assertEqual(response.status_code, 401)
        self.assertEqual(storage.get_all_users_with_tokens(), [])

    async def test_token_endpoint_rejects_malformed_json(self):
        response = await main.api_set_token(_InvalidJsonRequest({}))

        self.assertEqual(response.status_code, 400)

    async def test_coins_transfer_uses_saved_token_payload(self):
        storage.save_token("sender-uid", "not-a-jwt")
        storage.set_user_uid("sender-uid", "sender-uid")
        storage.ensure_coins("sender-uid")
        session_id = storage.create_session_for_user("sender-uid")
        request = _Request({"to_uid": "receiver-uid", "amount": 2})
        request.cookies = {"session_id": session_id}

        response = await main.api_coins_transfer(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {"ok": True, "new_balance": 1})
        self.assertEqual(storage.get_coins("receiver-uid"), 2)

    async def test_api_documentation_is_disabled(self):
        self.assertIsNone(main.app.docs_url)
        self.assertIsNone(main.app.redoc_url)
        self.assertIsNone(main.app.openapi_url)

    async def test_feedback_requires_session(self):
        response = await main.api_feedback(_Request({"message": "test"}))

        self.assertEqual(response.status_code, 401)

    async def test_admin_guard_ignores_jwt_claims_from_stored_token(self):
        payload = base64.urlsafe_b64encode(json.dumps({"id": "admin"}).encode()).rstrip(b"=").decode()
        storage.save_token("ordinary", f"header.{payload}.fake")
        storage.set_user_uid("ordinary", "ordinary")
        storage.add_admin("admin")
        session_id = storage.create_session_for_user("ordinary")
        request = _Request({})
        request.cookies = {"session_id": session_id}

        admin_uid, denied = main._require_admin(request)

        self.assertIsNone(admin_uid)
        self.assertEqual(denied.status_code, 403)

    async def test_verified_admin_session_keeps_access(self):
        token = "trusted-token"
        storage.save_token("admin", token)
        storage.set_user_uid("admin", "admin")
        storage.add_admin("admin")
        storage.mark_token_verified("admin", token)
        session_id = storage.create_session_for_user("admin")
        request = _Request({})
        request.cookies = {"session_id": session_id}

        admin_uid, denied = main._require_admin(request)

        self.assertEqual(admin_uid, "admin")
        self.assertIsNone(denied)

    async def test_security_update_preserves_existing_sessions(self):
        session_id, _ = storage.create_session()
        login_code, _ = storage.create_login_code("legacy-user")

        storage.init_db()

        self.assertIsNotNone(storage.get_user_by_session(session_id))
        self.assertEqual(storage.consume_login_code(login_code), "legacy-user")


if __name__ == "__main__":
    unittest.main()
