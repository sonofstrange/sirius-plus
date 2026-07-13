import base64
import json
import tempfile
import time
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace

import config
import main
import storage


class _LoginRequest:
    headers = {"content-type": "application/json"}
    cookies = {}
    state = SimpleNamespace()

    async def json(self):
        return {"email": "user@example.com", "password": "secret"}


class _SiriusClient:
    def __init__(self, token):
        self.token = token

    async def login(self, email, password):
        return self.token


class _AjaxLoginRequest(_LoginRequest):
    headers = {"content-type": "application/json", "x-requested-with": "XMLHttpRequest"}


class LoginSessionTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_first_password_login_creates_session_for_sirius_uid(self):
        uid = "sirius-user-42"
        payload = {"id": uid, "exp": int(time.time()) + 3600}
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        main._sirius_client = _SiriusClient(f"header.{payload_b64}.signature")

        response = await main.api_login(_LoginRequest())

        cookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        session_id = cookie["session_id"].value
        self.assertEqual(storage.get_user_by_session(session_id), uid)
        self.assertIsNotNone(storage.get_token(uid))

    async def test_ajax_password_login_returns_redirect_and_session(self):
        uid = "sirius-user-43"
        payload = {"id": uid, "exp": int(time.time()) + 3600}
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        main._sirius_client = _SiriusClient(f"header.{payload_b64}.signature")

        response = await main.api_login(_AjaxLoginRequest())

        self.assertEqual(json.loads(response.body), {"ok": True, "redirect": "/events?tab=register"})
        cookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        self.assertEqual(storage.get_user_by_session(cookie["session_id"].value), uid)


if __name__ == "__main__":
    unittest.main()
