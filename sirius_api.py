from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Self

log = logging.getLogger("sirius_api")

NGENIX_CHALLENGE_MARKER = "js-challenge-script"

SCHEDULE_URL = "https://my.sirius.online/api/activity/v0/schedule/student/record"
SUBSCRIBE_URL = "https://my.sirius.online/api/activity/v0/schedule/student/record/subscribe"
SCHEDULE_DAY_URL = "https://my.sirius.online/api/activity/v0/schedule/student"
AUTH_NAVIGATION_TIMEOUT_MS = 60_000
AUTH_FORM_TIMEOUT_MS = 45_000
AUTH_CHALLENGE_POLL_ATTEMPTS = 240
AUTH_COMPLETION_POLL_ATTEMPTS = 240

BASE_HEADERS = {
    "accept": "application/json;charset=utf-8",
    "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "origin": "https://my.sirius.online",
    "referer": "https://my.sirius.online/record-schedule",
    "content-type": "application/json",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
}


class _DescriptionTextParser(HTMLParser):
    _BLOCK_TAGS = {"address", "article", "blockquote", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "section"}
    _IGNORED_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def _newline(self):
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._IGNORED_TAGS:
            self.ignored_depth += 1
        elif not self.ignored_depth and (tag == "br" or tag in self._BLOCK_TAGS):
            self._newline()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._IGNORED_TAGS and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in self._BLOCK_TAGS:
            self._newline()

    def handle_data(self, data):
        if not self.ignored_depth:
            self.parts.append(data)


def clean_description(value: str | None) -> str:
    """Turn Sirius rich-text descriptions into safe, readable plain text."""
    if not value:
        return ""
    parser = _DescriptionTextParser()
    parser.feed(str(value))
    parser.close()
    lines = [" ".join(line.replace("\xa0", " ").split()) for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line).strip()


@dataclass
class EventInfo:
    event_id: str
    event_name: str
    day_iso: str
    event_start: str
    record_start: str | None
    record_end: str | None
    is_available: bool
    reasons: list[str]
    will_open_at: str | None
    is_recorded: bool
    is_reserved: bool
    people_current: int
    people_max: int
    description: str
    raw: dict
    unions: list[str] = field(default_factory=list)


@dataclass
class ScheduleEvent:
    event_id: str
    event_name: str
    event_type: str
    start_time: str
    end_time: str
    date_iso: str
    status: str
    location: list[str]
    tutors: list[str]
    description: str
    people_max: int
    people_current: int
    people_reserved: int
    record_end: str | None
    transport_info: dict | None
    departure_location: str
    arrival_location: str
    unions: list[str] = field(default_factory=list)


class SubscribeResult:
    def __init__(self, ok: bool, reserved: bool, status_code: int, body: str):
        self.ok = ok
        self.reserved = reserved
        self.status_code = status_code
        self.body = body


ERROR_MESSAGES = {
    "record_disabled": "запись ещё не открыта",
}

RETRYABLE_ERROR_KEYS = {"record_disabled"}


def classify_subscribe_result(result: SubscribeResult) -> tuple[str, str]:
    key = None
    message = ""
    try:
        data = json.loads(result.body)
        errors = data.get("error") or []
        if errors:
            key = errors[0].get("key")
            message = errors[0].get("message") or ""
    except Exception:
        pass

    # HTTP 5xx / 503 — server error, retryable
    if result.status_code >= 500:
        return "not_open_yet", f"HTTP {result.status_code} (временная ошибка сервера)"

    if result.ok and not key:
        return "ok", "записал"

    if key in RETRYABLE_ERROR_KEYS:
        return "not_open_yet", ERROR_MESSAGES.get(key, key)

    if key:
        text = ERROR_MESSAGES.get(key, message or key)
        return "terminal", text

    if result.ok:
        return "ok", "записал"

    return "terminal", (result.body[:150].strip() or f"HTTP {result.status_code}")


def _contains_reserved_success(data) -> bool:
    if isinstance(data, dict):
        for key, value in data.items():
            key_l = str(key).lower()
            if key_l in {"isreserved", "isreserve", "reserved"} and value is True:
                return True
            if key_l in {"recordtype", "recordstatus", "status", "state"} and isinstance(value, str):
                value_l = value.lower()
                if "reserve" in value_l or "резерв" in value_l:
                    return True
            if key_l in {"message", "text"} and isinstance(value, str):
                value_l = value.lower()
                if any(marker in value_l for marker in (
                    "записан в резерв",
                    "записали в резерв",
                    "добавлен в резерв",
                    "added to reserve",
                )):
                    return True
            if _contains_reserved_success(value):
                return True
    elif isinstance(data, list):
        return any(_contains_reserved_success(item) for item in data)
    return False


def detect_reserved_success(body: str) -> bool:
    body = (body or "").strip()
    if not body:
        return False
    try:
        return _contains_reserved_success(json.loads(body))
    except Exception:
        text = body.lower()
        return any(marker in text for marker in (
            "записан в резерв",
            "записали в резерв",
            "добавлен в резерв",
            "added to reserve",
        ))


def _parse_events(data: dict) -> list[EventInfo]:
    events: list[EventInfo] = []
    for day in data.get("success", []):
        day_iso = day.get("dayISO")
        for ev in day.get("events", []):
            avail = ev.get("availability", {}) or {}
            reasons_raw = avail.get("reason") or []
            reasons = [r.get("type") for r in reasons_raw]
            will_open_at = None
            for r in reasons_raw:
                if r.get("type") == "willOpen" and r.get("atISO"):
                    will_open_at = r["atISO"]
            events.append(EventInfo(
                event_id=str(ev.get("eventId")),
                event_name=ev.get("eventName", "???"),
                day_iso=day_iso,
                event_start=ev.get("eventStart"),
                record_start=ev.get("recordStart"),
                record_end=ev.get("recordEnd"),
                is_available=bool(avail.get("isAvailable")),
                reasons=reasons,
                will_open_at=will_open_at,
                is_recorded=bool(ev.get("isRecorded")),
                is_reserved=bool(ev.get("isReserved")),
                people_current=ev.get("peopleCurrent", 0),
                people_max=ev.get("peopleMax", 0),
                description=clean_description(ev.get("description") or ev.get("eventDescription")),
                raw=ev,
                unions=ev.get("unions") or [],
            ))
    return events


def _parse_schedule_events(data: dict) -> list[ScheduleEvent]:
    groups = (data.get("success") or {}).get("eventsGroups") or []
    result: list[ScheduleEvent] = []
    for group in groups:
        for ev in group.get("events") or []:
            etype = ev.get("eventType", "")
            ti = ev.get("transportInfo")
            ename = ev.get("eventName", "")
            if not ename and etype == "transferEvent":
                ename = "Трансфер"
            result.append(ScheduleEvent(
                event_id=str(ev.get("eventId", "")),
                event_name=ename,
                event_type=etype,
                start_time=ev.get("startTimeISO", ""),
                end_time=ev.get("endTimeISO", ""),
                date_iso=ev.get("dateISO", ""),
                status=ev.get("status", ""),
                location=ev.get("eventLocation") or [],
                tutors=ev.get("tutors") or [],
                description=clean_description(ev.get("description")),
                people_max=ev.get("peopleMax", 0),
                people_current=ev.get("peopleCurrent", 0),
                people_reserved=ev.get("peopleReserved", 0),
                record_end=ev.get("recordEnd"),
                transport_info=ti if ti else None,
                departure_location=ev.get("departureLocation", ""),
                arrival_location=ev.get("arrivalLocation", ""),
                unions=ev.get("unions") or [],
            ))
    return result


_MSK = dt.timezone(dt.timedelta(hours=3))


def parse_sirius_time(iso_str: str | None) -> dt.datetime | None:
    if not iso_str:
        return None
    s = iso_str.replace("Z", "+00:00")
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=_MSK)
    return d.astimezone(dt.timezone.utc)


def token_expiry(token: str) -> dt.datetime | None:
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        if exp:
            return dt.datetime.fromtimestamp(float(exp), tz=dt.timezone.utc)
    except Exception:
        return None
    return None


class SiriusClient:
    _instance: Self | None = None

    def __init__(self, bearer_token: str | None = None, headless: bool = True):
        self.token = bearer_token
        self.headless = headless
        self._pw = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._ready = asyncio.Event()
        self._restart_lock = asyncio.Lock()
        self._login_cleanup_tasks: set[asyncio.Task] = set()
        self._email_code_logins: dict[str, tuple[object, object, float]] = {}
        self.clock_skew = dt.timedelta(0)

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc) + self.clock_skew

    def _update_clock_skew(self, date_header: str | None):
        if not date_header:
            return
        try:
            server_time = parsedate_to_datetime(date_header)
            if server_time.tzinfo is None:
                server_time = server_time.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError):
            return
        skew = server_time - dt.datetime.now(dt.timezone.utc)
        skew_s = skew.total_seconds()
        if abs(skew_s) > 10:
            log.warning("Обнаружен рассинхрон часов с сервером Sirius: %+.1fс — учитываю поправку", skew_s)
        self.clock_skew = skew

    @classmethod
    def get_instance(cls) -> Self | None:
        return cls._instance

    async def start(self):
        from playwright.async_api import async_playwright

        self._pw = async_playwright()
        self._playwright = await self._pw.__aenter__()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=BASE_HEADERS["user-agent"],
        )
        challenge_page = await self._context.new_page()
        try:
            await challenge_page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=10000)
        except Exception:
            pass
        for _ in range(150):
            cookies = await self._context.cookies()
            if any(c["name"].startswith("ngenix_jscv_") for c in cookies):
                break
            await asyncio.sleep(0.1)
        await challenge_page.close()
        self._page = await self._context.new_page()
        try:
            await self._page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=10000)
        except Exception:
            pass
        for _ in range(50):
            try:
                url = self._page.url
                if "my.sirius.online" in url and url.startswith(SCHEDULE_URL):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.1)
        self._ready.set()

    async def wait_ready(self):
        await self._ready.wait()

    @staticmethod
    def _is_recoverable_error(msg: str) -> bool:
        return any(s in msg for s in (
            "Execution context was destroyed", "Target closed",
            "Target page, context or browser has been closed",
            "has been closed", "Failed to fetch", "TypeError: fetch",
            "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION_REFUSED",
        ))

    @staticmethod
    def _is_browser_dead_error(msg: str) -> bool:
        return any(s in msg for s in (
            "Target page, context or browser has been closed",
            "Browser has been closed", "Connection closed",
        ))

    async def _restart(self):
        async with self._restart_lock:
            log.warning("Sirius browser выглядит мёртвым, пересоздаю")
            try:
                if self._browser:
                    await self._browser.close()
            except Exception:
                pass
            try:
                if self._pw:
                    await self._pw.__aexit__(None, None, None)
            except Exception:
                pass
            self._browser = None
            self._context = None
            self._page = None
            self._ready.clear()
            try:
                await self.start()
            except Exception:
                self._ready.set()  # не блокируем всех если старт упал
                raise

    async def _js_fetch(self, url: str, method: str = "GET", body: dict | None = None, token: str | None = None) -> dict:
        tk = token or self.token
        ngenix_retries = 2
        for attempt in range(3):
            try:
                result = await self._page.evaluate("""
                    async ([url, method, body, token]) => {
                        const headers = {
                            authorization: 'Bearer ' + token,
                            accept: 'application/json;charset=utf-8',
                        };
                        const opts = {method, headers, credentials: 'include'};
                        if (body) {
                            headers['content-type'] = 'application/json';
                            opts.body = JSON.stringify(body);
                        }
                        const resp = await fetch(url, opts);
                        const text = await resp.text();
                        let json = null;
                        try { json = JSON.parse(text); } catch {}
                        return {status: resp.status, ok: resp.ok, body: text, json, date: resp.headers.get('date')};
                    }
                """, [url, method, body, tk])
                self._update_clock_skew(result.get("date"))
                log.info("Sirius %s %s — %s %s", method, url.split("my.sirius.online")[-1],
                         result.get("status"), result.get("body", "")[:80].replace("\n", " "))
                if result.get("status") == 503 and NGENIX_CHALLENGE_MARKER in (result.get("body") or ""):
                    if ngenix_retries > 0:
                        ngenix_retries -= 1
                        log.warning("Ngenix challenge (503), refreshing session…")
                        await self.refresh_session()
                        continue
                    raise Exception(f"Ngenix challenge not resolved after retries: HTTP 503")
                return result
            except Exception as e:
                msg = str(e)
                if not self._is_recoverable_error(msg):
                    raise
                if self._is_browser_dead_error(msg):
                    await self._restart()
                elif "Failed to fetch" in msg or "TypeError: fetch" in msg:
                    # ngenix challenge blocking requests at browser level
                    log.warning("Fetch blocked (ngenix?), refreshing session…")
                    await self.refresh_session()
                if attempt == 2:
                    raise
                await asyncio.sleep(0.3)
        raise RuntimeError("unreachable")

    async def fetch_schedule(self, token: str | None = None) -> list[EventInfo]:
        await self.wait_ready()
        result = await self._js_fetch(SCHEDULE_URL, token=token)
        if not result["ok"]:
            raise Exception(f"HTTP {result['status']}: {result['body']}")
        return _parse_events(result["json"] or {})

    async def fetch_schedule_day(self, date: str, token: str | None = None) -> list[ScheduleEvent]:
        await self.wait_ready()
        url = f"{SCHEDULE_DAY_URL}?date={date}"
        result = await self._js_fetch(url, token=token)
        if not result["ok"]:
            raise Exception(f"HTTP {result['status']}: {result['body']}")
        return _parse_schedule_events(result["json"])

    async def subscribe(self, event_id: str, token: str | None = None) -> SubscribeResult:
        await self.wait_ready()
        result = await self._js_fetch(SUBSCRIBE_URL, method="POST", body={"eventId": str(event_id)}, token=token)
        ok = result["status"] in (200, 201, 204)
        body = result["body"]
        reserved = ok and detect_reserved_success(body)
        return SubscribeResult(ok=ok, reserved=reserved, status_code=result["status"], body=body)

    async def unsubscribe(self, event_id: str, token: str | None = None) -> bool:
        await self.wait_ready()
        result = await self._js_fetch(SUBSCRIBE_URL, method="DELETE", body={"eventId": str(event_id)}, token=token)
        return result["status"] in (200, 201, 204)

    async def batch_fetch_schedule(self, tokens: list[str]) -> list[list[EventInfo]]:
        await self.wait_ready()
        results = await self._page.evaluate("""
            async (tokens, url) => {
                const fetches = tokens.map(token =>
                    fetch(url, {
                        headers: {
                            authorization: 'Bearer ' + token,
                            accept: 'application/json;charset=utf-8',
                        },
                        credentials: 'include',
                    }).then(async r => {
                        const text = await r.text();
                        let json = null;
                        try { json = JSON.parse(text); } catch {}
                        return {ok: r.ok, status: r.status, body: text, json};
                    })
                );
                return await Promise.all(fetches);
            }
        """, [tokens, SCHEDULE_URL])
        parsed = []
        for r in results:
            if r.get("ok") and r.get("json"):
                parsed.append(_parse_events(r["json"]))
            else:
                parsed.append([])
        return parsed

    async def login(self, email: str, password: str) -> str | None:
        await self.wait_ready()

        # Create a FRESH browser context for login — no shared cookies
        log.info("login: создаю новый контекст браузера для свежего входа")
        login_context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=BASE_HEADERS["user-agent"],
        )
        login_page = await login_context.new_page()
        try:
            token = await self._do_login_on_page(login_page, email, password)
            return token
        finally:
            cleanup = asyncio.create_task(self._close_login_context(login_context))
            self._login_cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._login_cleanup_tasks.discard)

    async def begin_email_code_login(self, email: str) -> str:
        """Ask Sirius to send an email code and retain only its isolated browser context."""
        await self.wait_ready()
        await self._cleanup_expired_email_code_logins()
        login_context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=BASE_HEADERS["user-agent"],
        )
        page = await login_context.new_page()
        try:
            await page.goto("https://auth.sirius.online/password", wait_until="domcontentloaded",
                            timeout=AUTH_NAVIGATION_TIMEOUT_MS)

            one_time_selector = 'span.ui-button__content:has-text("Одноразовый код")'
            for attempt in range(AUTH_CHALLENGE_POLL_ATTEMPTS):
                try:
                    button = page.locator(one_time_selector).first
                    if await button.count() and await button.is_visible():
                        await button.click()
                        break
                except Exception:
                    pass
                if attempt == AUTH_CHALLENGE_POLL_ATTEMPTS // 2:
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=AUTH_NAVIGATION_TIMEOUT_MS)
                    except Exception:
                        pass
                await asyncio.sleep(0.25)
            else:
                raise RuntimeError("Sirius не открыл форму входа по одноразовому коду")

            email_input = page.locator('input[name="email"]')
            await email_input.wait_for(state="visible", timeout=AUTH_FORM_TIMEOUT_MS)
            await email_input.fill(email)

            request_selectors = (
                'button:has-text("Получить код")',
                'button:has-text("Отправить код")',
                'button:has-text("Продолжить")',
                '.ui-button:has-text("Получить код")',
                '.ui-button:has-text("Отправить код")',
                '.ui-button:has-text("Продолжить")',
            )
            requested = False
            for selector in request_selectors:
                button = page.locator(selector).first
                try:
                    if await button.count() and await button.is_visible() and await button.is_enabled():
                        await button.click()
                        log.info("email-code: нажата кнопка отправки кода: %s", (await button.inner_text()).strip())
                        requested = True
                        break
                except Exception:
                    continue
            if not requested:
                # Sirius changes the submit button caption from time to time.  The
                # active primary button is more stable than its visible label.
                primary_buttons = page.locator(
                    'button.ui-button-mode-primary, button[type="submit"], [role="button"].ui-button-mode-primary'
                )
                button_snapshot = await page.locator("button, [role=button]").evaluate_all(
                    """elements => elements.slice(0, 20).map(element => ({
                        text: (element.innerText || '').trim(),
                        disabled: Boolean(element.disabled),
                        className: String(element.className || '')
                    }))"""
                )
                log.info("email-code: кнопки после ввода почты: %s", button_snapshot)
                for index in range(await primary_buttons.count()):
                    button = primary_buttons.nth(index)
                    try:
                        caption = (await button.inner_text()).strip()
                        if caption in {"Одноразовый код", "По паролю"}:
                            continue
                        if await button.is_visible() and await button.is_enabled():
                            await button.click()
                            log.info("email-code: нажата основная кнопка отправки: %s", caption)
                            requested = True
                            break
                    except Exception:
                        continue
            if not requested:
                log.info("email-code: основная кнопка не найдена, отправляю форму клавишей Enter")
                await email_input.press("Enter")

            try:
                await page.locator('input[placeholder="Код из письма"]').wait_for(
                    state="visible", timeout=AUTH_FORM_TIMEOUT_MS
                )
            except Exception as exc:
                log.warning("email-code: Sirius не показал поле кода после отправки: %s", exc)
                raise RuntimeError("Sirius не показал поле кода после отправки письма.") from exc
            attempt_id = secrets.token_urlsafe(32)
            self._email_code_logins[attempt_id] = (login_context, page, time.monotonic())
            log.info("login: одноразовый код запрошен для %s", email)
            return attempt_id
        except Exception:
            await self._close_login_context(login_context)
            raise

    async def complete_email_code_login(self, attempt_id: str, code: str) -> str | None:
        entry = self._email_code_logins.get(attempt_id)
        if not entry:
            raise RuntimeError("Срок действия запроса кода истёк. Запроси новый код.")
        login_context, page, created_at = entry
        if time.monotonic() - created_at > 10 * 60:
            await self.cancel_email_code_login(attempt_id)
            raise RuntimeError("Срок действия запроса кода истёк. Запроси новый код.")

        code_input = page.locator('input[placeholder="Код из письма"]')
        await code_input.fill(code.strip())
        confirm_button = page.locator('button:has-text("Подтвердить")').first
        await confirm_button.wait_for(state="visible", timeout=AUTH_FORM_TIMEOUT_MS)
        for _ in range(40):
            if await confirm_button.is_enabled():
                break
            await asyncio.sleep(0.15)
        if not await confirm_button.is_enabled():
            raise RuntimeError("Sirius не принял код. Проверь его и попробуй ещё раз.")
        await confirm_button.click()

        token = await self._extract_auth_token(page)
        if token:
            self._email_code_logins.pop(attempt_id, None)
            cleanup = asyncio.create_task(self._close_login_context(login_context))
            self._login_cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._login_cleanup_tasks.discard)
            return token
        raise RuntimeError("Sirius не подтвердил код. Проверь код или запроси новый.")

    async def cancel_email_code_login(self, attempt_id: str) -> None:
        entry = self._email_code_logins.pop(attempt_id, None)
        if entry:
            await self._close_login_context(entry[0])

    async def _cleanup_expired_email_code_logins(self) -> None:
        expired = [attempt_id for attempt_id, (_, _, created_at) in self._email_code_logins.items()
                   if time.monotonic() - created_at > 10 * 60]
        for attempt_id in expired:
            await self.cancel_email_code_login(attempt_id)

    async def _extract_auth_token(self, page) -> str | None:
        for _ in range(30):
            try:
                token = await page.evaluate(
                    "() => localStorage.getItem('AuthToken') || sessionStorage.getItem('AuthToken')"
                )
                if token:
                    self.token = token
                    return token
            except Exception:
                pass
            try:
                for cookie in await page.context.cookies():
                    if cookie["name"] == "AuthToken" and cookie["value"]:
                        self.token = cookie["value"]
                        return cookie["value"]
            except Exception:
                pass
            await asyncio.sleep(0.5)

        try:
            await page.goto("https://my.sirius.online/", wait_until="domcontentloaded",
                            timeout=AUTH_NAVIGATION_TIMEOUT_MS)
        except Exception:
            return None
        for _ in range(20):
            try:
                token = await page.evaluate(
                    "() => localStorage.getItem('AuthToken') || sessionStorage.getItem('AuthToken')"
                )
                if token:
                    self.token = token
                    return token
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return None

    async def _close_login_context(self, context) -> None:
        try:
            await context.close()
        except Exception as e:
            log.warning("login: не удалось закрыть временный контекст: %s", e)

    async def _do_login_on_page(self, page: 'Page', email: str, password: str) -> str | None:
        login_url = "https://auth.sirius.online/password"
        log.info("login: перехожу на %s", login_url)
        try:
            await page.goto(login_url, wait_until="domcontentloaded", timeout=AUTH_NAVIGATION_TIMEOUT_MS)
        except Exception as e:
            log.warning("login: не удалось открыть страницу — %s", e)
            return None

        log.info("login: страница загружена, url=%s", page.url)

        # Wait for ngenix challenge to resolve if present
        for _ in range(AUTH_CHALLENGE_POLL_ATTEMPTS):
            try:
                url = page.url
                cookies = await page.context.cookies()
                has_ngenix = any(c["name"].startswith("ngenix_jscv_") for c in cookies)
                if has_ngenix and "auth.sirius.online" in url and "password" in url:
                    log.info("login: ngenix пройден, на странице логина")
                    break
                elif "challenge=" in url:
                    await asyncio.sleep(0.5)
                    continue
                elif "auth.sirius.online" in url and "password" in url:
                    await asyncio.sleep(0.25)
                    continue
                else:
                    await asyncio.sleep(0.25)
            except Exception:
                await asyncio.sleep(0.25)
        else:
            log.info("login: таймаут ожидания ngenix, url=%s", page.url)
            # Force-reload the login page now that ngenix cookies should be set
            try:
                await page.goto(login_url, wait_until="domcontentloaded", timeout=AUTH_NAVIGATION_TIMEOUT_MS)
                await asyncio.sleep(0.5)
                log.info("login: перезагрузил страницу логина, url=%s", page.url)
            except Exception:
                pass

        # Check if we were redirected to my.sirius.online (already logged in via cookies)
        if "my.sirius.online" in page.url:
            log.info("login: редирект на my.sirius.online — уже авторизован по кукам")
            for _ in range(10):
                try:
                    token = await page.evaluate("() => localStorage.getItem('AuthToken') || sessionStorage.getItem('AuthToken')")
                    if token:
                        log.info("login: токен из localStorage (длина %d)", len(token))
                        self.token = token
                        return token
                except Exception:
                    pass
                # Also check cookies
                try:
                    cookies = await page.context.cookies()
                    for c in cookies:
                        if c["name"] == "AuthToken" and c["value"]:
                            log.info("login: токен из cookie (длина %d)", len(c["value"]))
                            self.token = c["value"]
                            return c["value"]
                except Exception:
                    pass
                await asyncio.sleep(0.3)

        # If still on auth.sirius.online, proceed with login form
        if "auth.sirius.online" not in page.url:
            log.warning("login: не на странице логина, url=%s", page.url)
            return None

        # Wait for ngenix / JS to finish
        for i in range(40):
            await asyncio.sleep(0.25)
            try:
                url = page.url
                if "auth.sirius.online" in url:
                    log.info("login: застабилизировалась за %.1fс", 0.25 * (i + 1))
                    break
                elif "my.sirius.online" in url:
                    log.info("login: улетели на my.sirius.online — уже авторизован")
                    for _ in range(10):
                        try:
                            token = await page.evaluate("() => localStorage.getItem('AuthToken') || sessionStorage.getItem('AuthToken')")
                            if token:
                                log.info("login: токен из localStorage (длина %d)", len(token))
                                self.token = token
                                return token
                        except Exception:
                            pass
                        try:
                            cookies = await page.context.cookies()
                            for c in cookies:
                                if c["name"] == "AuthToken" and c["value"]:
                                    log.info("login: токен из cookie (длина %d)", len(c["value"]))
                                    self.token = c["value"]
                                    return c["value"]
                        except Exception:
                            pass
                        await asyncio.sleep(0.3)
                    log.warning("login: не удалось извлечь токен с my.sirius.online")
                    return None
            except Exception:
                pass

        # Dump page state for debugging
        try:
            title = await page.title()
            log.info("login: title=%s", title)
        except Exception:
            pass

        # Wait for email field
        log.info("login: жду input[name=email]")
        try:
            await page.wait_for_selector('input[name="email"]', timeout=AUTH_FORM_TIMEOUT_MS)
            log.info("login: input[name=email] найден")
        except Exception:
            log.warning("login: input[name=email] НЕ найден на %s", page.url)
            # Maybe redirected to my.sirius.online — try extracting token
            if "my.sirius.online" in page.url:
                try:
                    token = await page.evaluate("() => localStorage.getItem('AuthToken')")
                    if token:
                        log.info("login: токен из localStorage после редиректа (длина %d)", len(token))
                        self.token = token
                        return token
                except Exception:
                    pass
                try:
                    cookies = await page.context.cookies()
                    for c in cookies:
                        if c["name"] == "AuthToken" and c["value"]:
                            log.info("login: токен из cookie после редиректа (длина %d)", len(c["value"]))
                            return c["value"]
                except Exception:
                    pass
            try:
                html = await page.evaluate("() => document.body.innerText.substring(0, 500)")
                log.warning("login: фрагмент страницы: %s", html)
            except Exception:
                pass
            return None

        await asyncio.sleep(0.3)

        # Fill email first
        log.info("login: заполняю email")
        try:
            await page.click('input[name="email"]')
            await page.fill('input[name="email"]', "")
            await page.type('input[name="email"]', email, delay=50)
            log.info("login: email введён")
        except Exception as e:
            log.warning("login: ошибка email — %s", e)
            return None

        # Click "По паролю" to reveal the password field
        log.info("login: ищу кнопку «По паролю»")
        try:
            await page.click('span.ui-button__content:text("По паролю")')
            log.info("login: кнопка «По паролю» нажата")
        except Exception:
            try:
                await page.click('button:has-text("По паролю"), .ui-button:has-text("По паролю")')
                log.info("login: кнопка «По паролю» нажата (fallback)")
            except Exception as e:
                log.warning("login: не удалось нажать «По паролю» — %s", e)
                try:
                    html = await page.evaluate("() => document.body.innerText.substring(0, 500)")
                    log.warning("login: фрагмент страницы: %s", html)
                except Exception:
                    pass
                return None

        await asyncio.sleep(0.5)

        # Now wait for password field to appear
        log.info("login: жду input[name=password]")
        try:
            await page.wait_for_selector('input[name="password"]', timeout=AUTH_FORM_TIMEOUT_MS)
            log.info("login: input[name=password] найден")
        except Exception:
            log.warning("login: input[name=password] НЕ найден после «По паролю»")
            try:
                html = await page.evaluate("() => document.body.innerText.substring(0, 500)")
                log.warning("login: фрагмент страницы: %s", html)
            except Exception:
                pass
            return None

        # Fill password
        log.info("login: заполняю пароль")
        try:
            await page.click('input[name="password"]')
            await page.fill('input[name="password"]', "")
            await page.type('input[name="password"]', password, delay=50)
            log.info("login: пароль введён")
        except Exception as e:
            log.warning("login: ошибка пароля — %s", e)
            return None

        # Click submit
        log.info("login: нажимаю Войти")
        try:
            await page.click('button[type="submit"]')
            log.info("login: кнопка нажата")
        except Exception as e:
            log.warning("login: не удалось нажать — %s", e)
            return None

        # Wait for auth to complete (URL changes: /password -> /auth/callback -> /)
        # Also wait for ngenix challenge to resolve
        for _ in range(AUTH_COMPLETION_POLL_ATTEMPTS):
            await asyncio.sleep(0.25)
            try:
                url = page.url
                # Check for ngenix challenge - wait for it to resolve
                cookies = await page.context.cookies()
                has_ngenix = any(c["name"].startswith("ngenix_jscv_") for c in cookies)

                if url.rstrip("/") == "https://auth.sirius.online":
                    log.info("login: аутентификация завершена, на странице профиля")
                    # Wait a bit for localStorage to be populated
                    await asyncio.sleep(1)
                    break
                elif "my.sirius.online" in url and has_ngenix:
                    log.info("login: аутентификация завершена (my.sirius.online), ngenix пройден")
                    break
            except Exception:
                pass

        # Extract token from auth.sirius.online (where we are after login)
        log.info("login: ищу AuthToken на текущем домене")
        for _ in range(20):
            try:
                token = await page.evaluate("""
                    () => localStorage.getItem('AuthToken') || sessionStorage.getItem('AuthToken')
                """)
                if token:
                    log.info("login: токен получен из localStorage (длина %d)", len(token))
                    self.token = token
                    return token
            except Exception:
                pass
            # Also check cookies for token
            try:
                cookies = await page.context.cookies()
                for c in cookies:
                    if c["name"] == "AuthToken" and c["value"]:
                        log.info("login: токен получен из cookie (длина %d)", len(c["value"]))
                        self.token = c["value"]
                        return c["value"]
            except Exception:
                pass
            await asyncio.sleep(0.5)

        # Fallback: navigate to my.sirius.online to trigger token propagation
        log.info("login: не нашёл токен на auth.sirius.online, пробую my.sirius.online")
        try:
            await page.goto("https://my.sirius.online/", wait_until="domcontentloaded", timeout=AUTH_NAVIGATION_TIMEOUT_MS)
            # Wait for ngenix to pass
            for _ in range(AUTH_CHALLENGE_POLL_ATTEMPTS):
                await asyncio.sleep(0.25)
                cookies = await page.context.cookies()
                if any(c["name"].startswith("ngenix_jscv_") for c in cookies):
                    break
            await asyncio.sleep(2)
            for _ in range(20):
                try:
                    token = await page.evaluate("""
                        () => localStorage.getItem('AuthToken') || sessionStorage.getItem('AuthToken')
                    """)
                    if token:
                        log.info("login: токен получен с my.sirius.online из localStorage (длина %d)", len(token))
                        return token
                except Exception:
                    pass
                # Also check cookies
                try:
                    cookies = await page.context.cookies()
                    for c in cookies:
                        if c["name"] == "AuthToken" and c["value"]:
                            log.info("login: токен получен с my.sirius.online из cookie (длина %d)", len(c["value"]))
                            return c["value"]
                except Exception:
                    pass
                await asyncio.sleep(0.5)
        except Exception:
            pass

        log.warning("login: токен не найден, url=%s", page.url)
        return None

    async def stop(self):
        for attempt_id in list(self._email_code_logins):
            await self.cancel_email_code_login(attempt_id)
        for task in self._login_cleanup_tasks:
            task.cancel()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.__aexit__(None, None, None)
        SiriusClient._instance = None

    async def get_page_url(self) -> str:
        return self._page.url

    async def refresh_session(self):
        self._ready.clear()
        try:
            await self._page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=10000)
        except Exception:
            pass
        for _ in range(150):
            cookies = await self._context.cookies()
            if any(c["name"].startswith("ngenix_jscv_") for c in cookies):
                break
            await asyncio.sleep(0.1)
        for _ in range(50):
            try:
                url = self._page.url
                if "my.sirius.online" in url and url.startswith(SCHEDULE_URL):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.1)
        self._ready.set()
