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
        return {"email": "user@example.com", "password": "secret", "personal_data_consent": True}


class _SiriusClient:
    def __init__(self, token):
        self.token = token

    async def login(self, email, password):
        return self.token


class _EmailCodeSiriusClient:
    def __init__(self, token):
        self.token = token
        self.requested_emails = []

    async def begin_email_code_login(self, email):
        self.requested_emails.append(email)
        return "email-code-attempt"

    async def complete_email_code_login(self, attempt_id, code):
        if attempt_id != "email-code-attempt" or code != "123456":
            raise RuntimeError("invalid code")
        return self.token


class _AjaxLoginRequest(_LoginRequest):
    headers = {"content-type": "application/json", "x-requested-with": "XMLHttpRequest"}


class _AjaxFormLoginRequest:
    headers = {
        "content-type": "multipart/form-data; boundary=test-boundary",
        "x-requested-with": "XMLHttpRequest",
    }
    cookies = {}
    state = SimpleNamespace()

    async def form(self):
        return {"email": "user@example.com", "password": "secret", "referral_code": "", "personal_data_consent": "yes"}


class LoginSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        self.old_client = main._sirius_client
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()
        main._email_code_attempt_users.clear()

    def tearDown(self):
        main._sirius_client = self.old_client
        main._email_code_attempt_users.clear()
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
        consent = storage.get_personal_data_consent(uid)
        self.assertIsNotNone(consent)
        self.assertEqual(consent["version"], main.PERSONAL_DATA_CONSENT_VERSION)

    async def test_ajax_password_login_returns_redirect_and_session(self):
        uid = "sirius-user-43"
        payload = {"id": uid, "exp": int(time.time()) + 3600}
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        main._sirius_client = _SiriusClient(f"header.{payload_b64}.signature")

        response = await main.api_login(_AjaxLoginRequest())

        self.assertEqual(json.loads(response.body), {"ok": True, "redirect": "/schedule"})
        cookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        self.assertEqual(storage.get_user_by_session(cookie["session_id"].value), uid)

    async def test_password_login_requires_personal_data_consent(self):
        class _RequestWithoutConsent(_LoginRequest):
            headers = {"content-type": "application/json", "x-requested-with": "XMLHttpRequest"}

            async def json(self):
                return {"email": "user@example.com", "password": "secret"}

        class _ClientThatMustNotBeCalled:
            async def login(self, email, password):
                raise AssertionError("Sirius login must not run without consent")

        main._sirius_client = _ClientThatMustNotBeCalled()
        response = await main.api_login(_RequestWithoutConsent())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["ok"], False)

    async def test_ajax_form_login_returns_session_cookie(self):
        uid = "sirius-user-44"
        payload = {"id": uid, "exp": int(time.time()) + 3600}
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        main._sirius_client = _SiriusClient(f"header.{payload_b64}.signature")

        response = await main.api_login(_AjaxFormLoginRequest())

        self.assertEqual(json.loads(response.body), {"ok": True, "redirect": "/schedule"})
        cookie = SimpleCookie()
        cookie.load(response.headers["set-cookie"])
        self.assertEqual(storage.get_user_by_session(cookie["session_id"].value), uid)

    async def test_ajax_password_login_reports_missing_sirius_token_as_json(self):
        main._sirius_client = _SiriusClient(None)

        response = await main.api_login(_AjaxLoginRequest())

        self.assertEqual(response.status_code, 401)
        body = json.loads(response.body)
        self.assertEqual(body["ok"], False)
        self.assertIn("одноразовому коду", body["error"])

    async def test_email_code_login_saves_email_without_password(self):
        uid = "sirius-user-email-code"
        payload = {"id": uid, "exp": int(time.time()) + 3600}
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        main._sirius_client = _EmailCodeSiriusClient(f"header.{payload_b64}.signature")

        class _Request:
            headers = {"content-type": "application/json"}
            cookies = {}
            state = SimpleNamespace()

            def __init__(self, data):
                self.data = data

            async def json(self):
                return self.data

        email = "user@example.com"
        requested = await main.api_request_email_login_code(_Request({
            "email": email, "personal_data_consent": True,
        }))
        self.assertEqual(json.loads(requested.body)["ok"], True)
        response = await main.api_confirm_email_login_code(_Request({
            "attempt_id": "email-code-attempt", "code": "123456",
            "personal_data_consent": True,
        }))

        self.assertEqual(json.loads(response.body), {"ok": True, "redirect": "/schedule"})
        self.assertEqual(storage.get_login_type(uid), "email_code")
        self.assertEqual(storage.get_login_email(uid), email)


if __name__ == "__main__":
    unittest.main()
