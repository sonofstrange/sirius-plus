import json
import re
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


class LoginFrontendTests(unittest.TestCase):
    def test_password_form_posts_formdata_and_follows_json_redirect(self):
        source = (Path(__file__).parent / "templates" / "login.html").read_text(encoding="utf-8")
        match = re.search(
            r"async function submitPasswordLogin\(form\) \{.*?\n}\n\nasync function saveToken",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        script = match.group(0).replace("\n\nasync function saveToken", "")
        script = script.replace("window.location.replace(data.redirect);", "window.__redirect = data.redirect;")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            requests = []
            page.route("https://test.local/", lambda route: route.fulfill(body="""
                <form id="login-form"><input name="email" value="user@example.com">
                <input name="password" value="secret"></form>
                <button id="login-btn">Войти</button>
                <div id="login-progress"></div><div id="login-step"></div><div id="login-substep"></div>
            """, content_type="text/html"))
            page.route("https://test.local/api/login", lambda route: (
                requests.append(route.request),
                route.fulfill(body=json.dumps({"ok": True, "redirect": "/events?tab=register"}), content_type="application/json"),
            ))
            page.goto("https://test.local/")
            page.add_script_tag(content=script)
            page.evaluate("window.startLoginProgress = () => true")
            result = page.evaluate("""async () => {
                await submitPasswordLogin(document.getElementById('login-form'));
                return {redirect: window.__redirect};
            }""")
            browser.close()

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "POST")
        self.assertIn('name="email"', requests[0].post_data)
        self.assertIn("user@example.com", requests[0].post_data)
        self.assertEqual(result["redirect"], "/events?tab=register")


if __name__ == "__main__":
    unittest.main()
