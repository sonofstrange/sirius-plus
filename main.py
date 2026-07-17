from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import math
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from urllib.parse import quote
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import jinja2
import uvicorn
from fastapi import FastAPI, Request, Response, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import config as app_config
import poller
import sirius_radar
import storage
from sirius_api import EventInfo, SiriusClient, token_expiry, parse_sirius_time, classify_subscribe_result, clean_description

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

# Suppress uvicorn access logs for specific endpoints
class UvicornAccessFilter(logging.Filter):
    def filter(self, record):
        return "/api/notifications" not in record.getMessage()

uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.addFilter(UvicornAccessFilter())

BASE_DIR = Path(__file__).parent

_sirius_client: SiriusClient | None = None
_notification_queue: dict[str, list[str]] = {}
_firebase_app = None
_partner_request_windows: dict[str, deque[float]] = {}
_email_code_attempt_users: dict[str, tuple[str | None, str, float]] = {}

CACHE_TTL = 600
_schedule_cache: dict[str, tuple[float, list]] = {}

PAGE_SIZE = 20
DRONEBET_PARTNER = "dronebet"
DRONEBET_MAX_EXCHANGE_COINS = 500

_MSK = dt.timezone(dt.timedelta(hours=3))
REFERRAL_COOKIE = "sirius_referral_code"
PERSONAL_DATA_CONSENT_VERSION = "2026-07-16"

_ERROR_MSGS = {
    "401": "Токен протух — обнови его на странице входа.",
    "403": "Доступ запрещён. Токен протух — обнови на странице входа.",
    "503": "Сайт Sirius временно недоступен — попробуй позже.",
    "name resolution": "Не удалось соединиться с Sirius. Проверь подключение к интернету.",
    "connection refused": "Сайт Sirius не отвечает. Возможно, он на обслуживании.",
    "timeout": "Сайт Sirius не ответил вовремя — попробуй позже.",
    "econnreset": "Соединение с Sirius разорвано. Попробуй ещё раз.",
    "econnaborted": "Соединение с Sirius прервано. Попробуй ещё раз.",
}
def _friendly_error(err: str | Exception) -> str:
    s = str(err).lower()
    for key, msg in _ERROR_MSGS.items():
        if key in s:
            return msg
    return str(err)


def _has_personal_data_consent(value) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _personal_data_consent_required(wants_json: bool = False):
    message = "Подтверди согласие на обработку персональных данных."
    if wants_json:
        return JSONResponse({"ok": False, "error": message}, status_code=400)
    return RedirectResponse(url=f"/?error={quote(message)}", status_code=303)


def _login_error_response(message: str, wants_json: bool = False, status_code: int = 400):
    """Return an error in the same format as the login form expects."""
    if wants_json:
        return JSONResponse({"ok": False, "error": message}, status_code=status_code)
    return RedirectResponse(url=f"/?error={quote(message)}", status_code=303)


def _remember_email_code_attempt(attempt_id: str, user_id: str | None, email: str) -> None:
    now = time.monotonic()
    for old_id, (_, _, created_at) in list(_email_code_attempt_users.items()):
        if now - created_at > 10 * 60:
            _email_code_attempt_users.pop(old_id, None)
    _email_code_attempt_users[attempt_id] = (user_id, email, now)


def _finish_email_code_login(request: Request, token: str, email: str, referral_code: str = ""):
    """Persist a token obtained from a Sirius email one-time code and create a session."""
    exp = token_expiry(token)
    if not exp:
        return JSONResponse({"ok": False, "error": "Sirius вернул некорректный токен."}, status_code=400)
    payload = _decode_jwt(token)
    if not payload or not payload.get("id"):
        return JSONResponse({"ok": False, "error": "Не удалось определить аккаунт Sirius."}, status_code=400)

    previous_user_id = get_user_id(request)
    is_new_session = previous_user_id is None
    session_id = request.cookies.get("session_id") if previous_user_id else None
    uid = payload["id"]
    ban = storage.get_account_ban(uid)
    if ban:
        return _banned_account_response(request, ban, wants_json=True)
    is_first_sirius_login = storage.get_user_by_uid(uid) is None
    if is_new_session:
        user_id = storage.get_user_by_uid(uid) or uid
    else:
        user_id = _resolve_uid_session(request, uid, previous_user_id, session_id)

    storage.save_token(user_id, token)
    storage.set_user_uid(user_id, uid)
    storage.mark_token_verified(user_id, token)
    storage.save_email_code_login(user_id, email)
    storage.ensure_coins(uid)
    storage.record_personal_data_consent(uid, PERSONAL_DATA_CONSENT_VERSION)
    full_name = " ".join(filter(None, [payload.get("lastName"), payload.get("firstName"), payload.get("middleName")]))
    storage.save_known_uid(uid, user_id, full_name)

    referral_code = storage.normalize_referral_code(referral_code or request.cookies.get(REFERRAL_COOKIE, ""))
    if is_first_sirius_login and storage.apply_referral(referral_code, uid):
        storage.add_notification(uid, "🎁 Реферальный код применён: тебе начислено 5 Сириус Коинов.", "success")
        referrer_uid = storage.get_referrer_uid(uid)
        if referrer_uid:
            storage.add_notification(referrer_uid, "🎁 Друг зарегистрировался по твоей ссылке: начислено 5 Сириус Коинов.", "success")

    response = JSONResponse({"ok": True, "redirect": "/schedule"})
    if is_new_session:
        session_id = storage.create_session_for_user(user_id)
        response.set_cookie(key="session_id", value=session_id, max_age=86400 * 365, httponly=True, samesite="lax")
    if referral_code:
        response.delete_cookie(REFERRAL_COOKIE)
    log.info("email-code login: session ready for uid=%s", uid)
    return response


def _now():
    if _sirius_client is not None:
        return _sirius_client.now()
    return dt.datetime.now(dt.timezone.utc)


def fmt_dt(iso: str | None) -> str:
    d = parse_sirius_time(iso)
    if not d:
        return "—"
    d = d.astimezone(_MSK)
    return d.strftime("%d.%m %H:%M")

def fmt_time(iso: str | None) -> str:
    d = parse_sirius_time(iso)
    if not d:
        return "—"
    d = d.astimezone(_MSK)
    return d.strftime("%H:%M")


def _community_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_MSK)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _community_event_bounds(event) -> tuple[dt.datetime | None, dt.datetime | None]:
    start = _community_datetime(
        f"{event['date_iso']}T{event['start_time']}" if event["start_time"] else ""
    )
    end = _community_datetime(
        f"{event['date_iso']}T{event['end_time']}" if event["end_time"] else ""
    )
    # An end time after midnight belongs to the following calendar day.
    if start and end and end <= start:
        end += dt.timedelta(days=1)
    return start, end or start


def _community_event_finished(event, now: dt.datetime | None = None) -> bool:
    _, end = _community_event_bounds(event)
    return bool(end and (now or _now()) >= end)


def _community_registration_open(event) -> bool:
    now = _now()
    starts_at = _community_datetime(event["registration_open_at"])
    closes_at = _community_datetime(event["registration_close_at"])
    _, event_ends_at = _community_event_bounds(event)
    if starts_at and now < starts_at:
        return False
    if closes_at and now >= closes_at:
        return False
    if event_ends_at and now >= event_ends_at:
        return False
    return not event["people_max"] or event["people_current"] < event["people_max"]


def _community_event_payload(event, user_id: str, is_admin: bool = False) -> dict:
    coorganizers = storage.get_community_coorganizers(event["id"])
    names = [row["full_name"] or row["display_name"] or row["uid"] for row in coorganizers]
    is_registered = bool(event["is_registered"]) if "is_registered" in event.keys() else False
    can_manage = bool(event["can_manage"]) if "can_manage" in event.keys() else storage.can_manage_community_event(event["id"], user_id)
    start, end = _community_event_bounds(event)
    now = _now()
    status = "finished" if end and now >= end else "ongoing" if start and now >= start else ""
    return {
        "event_id": f"community_{event['id']}",
        "community_id": event["id"],
        "event_name": event["event_name"],
        "event_type": "communityEvent",
        "start_time": f"{event['date_iso']}T{event['start_time']}:00+03:00" if event["start_time"] else "",
        "end_time": f"{event['date_iso']}T{event['end_time']}:00+03:00" if event["end_time"] else "",
        "date_iso": event["date_iso"],
        "status": status,
        "location": [event["location"]] if event["location"] else [],
        "community_contact": event["contact"],
        "tutors": [],
        "description": event["description"],
        "people_max": event["people_max"],
        "people_current": event["people_current"],
        "people_reserved": 0,
        "record_end": event["registration_close_at"],
        "transport_info": None,
        "departure_location": "",
        "arrival_location": "",
        "unions": [],
        "community_owner": event["owner_name"] or event["owner_uid"],
        "community_coorganizers": names,
        "community_is_registered": is_registered,
        "community_can_manage": can_manage or is_admin,
        "community_registration_open": _community_registration_open(event),
        "community_is_finished": status == "finished",
        "registration_open_at": event["registration_open_at"],
        "registration_close_at": event["registration_close_at"],
    }


def _community_event_for_events_page(event, user_id: str, is_admin: bool = False):
    payload = _community_event_payload(event, user_id, is_admin)
    payload.update({
        "day_iso": event["date_iso"],
        "raw": {"eventEnd": payload["end_time"]},
        "event_start": payload["start_time"],
        "event_end": payload["end_time"],
        "is_recorded": payload["community_is_registered"],
        "is_reserved": False,
        "is_available": payload["community_registration_open"],
        "will_open_at": event["registration_open_at"],
        "reasons": [],
        "_watched": False,
        "_watch": None,
        "_conflict": False,
        "_conflict_with": [],
    })
    return type("_CommunityEvent", (), payload)()


def _fmt_countdown(delta: dt.timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    if total < 60:
        return f"{total}с"
    if total < 3600:
        return f"{total // 60}м"
    h = total // 3600
    m = (total % 3600) // 60
    return f"{h}ч {m}м" if m else f"{h}ч"


async def web_notify(user_id: str, text: str, ntype: str = "info"):
    if user_id not in _notification_queue:
        _notification_queue[user_id] = []
    _notification_queue[user_id].append(text)
    try:
        storage.add_notification(user_id, text, ntype)
    except Exception as e:
        log.warning("Failed to save notification: %s", e)
    try:
        asyncio.create_task(_send_push_to_user(user_id, text, ntype))
        asyncio.create_task(_send_mobile_push_to_user(user_id, text, ntype))
    except Exception as e:
        log.warning("Failed to schedule push notification: %s", e)
    if text.startswith("✅ Ты теперь записан"):
        _schedule_cache.pop(f"events:{user_id}", None)
    log.info("NOTIFY %s [%s]: %s", user_id, ntype, text)


async def _verify_existing_admin_tokens(client: SiriusClient):
    """Trust pre-existing admin sessions only after Sirius accepts their stored token."""
    for user_id in storage.get_admin_users_with_tokens():
        token = storage.get_token(user_id)
        if not token:
            continue
        try:
            storage.increment_sirius_request(user_id)
            await asyncio.wait_for(client.fetch_schedule(token=token), timeout=15)
            storage.mark_token_verified(user_id, token)
        except Exception as e:
            log.warning("Admin token verification failed for %s: %s", user_id, _friendly_error(e))


def _get_cached(key: str) -> list | None:
    now = time.time()
    entry = _schedule_cache.get(key)
    if entry and now - entry[0] < CACHE_TTL:
        return entry[1]
    return None


def _get_cached_any(key: str) -> list | None:
    entry = _schedule_cache.get(key)
    return entry[1] if entry else None


def _set_cached(key: str, data: list):
    _schedule_cache[key] = (time.time(), data)


def _serialize_events(events: list[EventInfo]) -> str:
    return json.dumps([asdict(e) for e in events], ensure_ascii=False)


def _deserialize_events(data_json: str) -> list[EventInfo]:
    items = json.loads(data_json)
    events = []
    for item in items:
        item = {k: item.get(k) for k in EventInfo.__dataclass_fields__}
        item["description"] = clean_description(item.get("description"))
        events.append(EventInfo(**item))
    return events


def _set_events_cached(user_id: str, events: list[EventInfo]):
    _set_cached(f"events:{user_id}", events)
    _save_schedule_team(user_id, events)
    try:
        storage.set_events_cache(user_id, _serialize_events(events))
    except Exception as e:
        log.warning("Failed to persist events cache for %s: %s", user_id, e)


def _get_persistent_events_cache(user_id: str) -> list[EventInfo] | None:
    try:
        row = storage.get_events_cache(user_id)
        if not row:
            return None
        events = _deserialize_events(row["data_json"])
        _set_cached(f"events:{user_id}", events)
        return events
    except Exception as e:
        log.warning("Failed to load persistent events cache for %s: %s", user_id, e)
        return None


def _invalidate_events_cache(user_id: str):
    _schedule_cache.pop(f"events:{user_id}", None)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _ensure_vapid_private_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key_path = app_config.VAPID_PRIVATE_KEY_FILE
    if key_path.exists():
        return
    key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path.write_bytes(pem)


def _get_vapid_public_key() -> str:
    from cryptography.hazmat.primitives import serialization

    _ensure_vapid_private_key()
    private_key = serialization.load_pem_private_key(
        app_config.VAPID_PRIVATE_KEY_FILE.read_bytes(),
        password=None,
    )
    pub = private_key.public_key().public_numbers()
    raw = b"\x04" + pub.x.to_bytes(32, "big") + pub.y.to_bytes(32, "big")
    return _b64url(raw)


async def _send_push_to_user(user_id: str, text: str, ntype: str = "info"):
    rows = storage.get_push_subscriptions(user_id)
    if not rows:
        return {"sent": 0, "failed": 0, "removed": 0}
    try:
        from pywebpush import WebPushException, webpush
    except Exception as e:
        log.warning("Web Push disabled, pywebpush unavailable: %s", e)
        return {"sent": 0, "failed": len(rows), "removed": 0, "error": "pywebpush unavailable"}

    payload = json.dumps({
        "title": "Пирожковый Диспетчер",
        "body": text,
        "type": ntype,
        "url": "/events?tab=register",
        "is_alarm": ntype == "alarm",
    }, ensure_ascii=False)

    sent = 0
    failed = 0
    removed = 0
    for row in rows:
        endpoint = row["endpoint"]
        try:
            subscription = json.loads(row["subscription_json"])
            await asyncio.to_thread(
                webpush,
                subscription_info=subscription,
                data=payload,
                vapid_private_key=str(app_config.VAPID_PRIVATE_KEY_FILE),
                vapid_claims={"sub": app_config.WEB_PUSH_SUBJECT},
            )
            sent += 1
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                storage.delete_push_subscription(endpoint)
                removed += 1
            else:
                failed += 1
                log.warning("Web Push failed for %s: %s", user_id, e)
        except Exception as e:
            failed += 1
            log.warning("Web Push failed for %s: %s", user_id, e)
    return {"sent": sent, "failed": failed, "removed": removed}


def _fcm_is_configured() -> bool:
    return app_config.FCM_SERVICE_ACCOUNT_FILE.exists()


def _send_mobile_push_blocking(rows, text: str, ntype: str) -> dict:
    global _firebase_app
    from firebase_admin import credentials, get_app, initialize_app, messaging

    if _firebase_app is None:
        try:
            _firebase_app = get_app()
        except ValueError:
            _firebase_app = initialize_app(credentials.Certificate(str(app_config.FCM_SERVICE_ACCOUNT_FILE)))

    is_alarm = ntype == "alarm"
    sent = failed = removed = 0
    for row in rows:
        try:
            message = messaging.Message(
                data={
                    "title": "Пирожковый Диспетчер",
                    "body": text,
                    "is_alarm": "1" if is_alarm else "0",
                    "url": "/events?tab=register",
                },
                android=messaging.AndroidConfig(priority="high"),
                token=row["token"],
            )
            messaging.send(message, app=_firebase_app)
            sent += 1
        except Exception as exc:
            message = str(exc).lower()
            if "registration-token-not-registered" in message or "invalid-registration-token" in message:
                storage.delete_mobile_push_device(row["token"])
                removed += 1
            else:
                failed += 1
                log.warning("FCM push failed for %s: %s", row["token"][:12], exc)
    return {"sent": sent, "failed": failed, "removed": removed}


async def _send_mobile_push_to_user(user_id: str, text: str, ntype: str = "info"):
    rows = storage.get_mobile_push_devices(user_id)
    if not rows:
        return {"sent": 0, "failed": 0, "removed": 0}
    if not _fcm_is_configured():
        return {"sent": 0, "failed": 0, "removed": 0, "disabled": True}
    try:
        return await asyncio.to_thread(_send_mobile_push_blocking, rows, text, ntype)
    except Exception as exc:
        log.warning("FCM push is unavailable: %s", exc)
        return {"sent": 0, "failed": len(rows), "removed": 0, "error": str(exc)}


def _is_past_event_start(event_start: str | None) -> bool:
    ev_start = parse_sirius_time(event_start)
    return bool(ev_start and ev_start < _now())


def _is_past_event(ev) -> bool:
    return _is_past_event_start(getattr(ev, "event_start", None))


async def _find_event_for_user(user_id: str, token: str, event_id: str):
    cached = _get_cached(f"events:{user_id}")
    if cached is None:
        if not _sirius_client:
            return None
        storage.increment_sirius_request(user_id)
        cached = await _sirius_client.fetch_schedule(token=token)
        _set_events_cached(user_id, cached)
    return next((e for e in cached if e.event_id == event_id), None)


def get_user_id(request: Request) -> str | None:
    if hasattr(request.state, '_user_id'):
        uid = request.state._user_id
        return uid if uid else None
    session_id = request.cookies.get("session_id")
    uid = storage.get_user_by_session(session_id) if session_id else None
    request.state._user_id = uid
    if uid:
        request.state._token = None
    return uid


def _account_ban_for_user(user_id: str | None):
    uid = storage.get_user_uid(user_id) if user_id else None
    return storage.get_account_ban(uid or "")


def _banned_account_response(request: Request, ban, wants_json: bool = False):
    if wants_json:
        return JSONResponse({"ok": False, "error": "Аккаунт заблокирован", "reason": ban["reason"]}, status_code=403)
    return templates.TemplateResponse(request, "banned.html", {"reason": ban["reason"]}, status_code=403)


def _cancel_banned_user_auto_registrations(uid: str):
    """Stop background Sirius activity and release unused reservations on a ban."""
    user_id = storage.get_user_by_uid(uid)
    if not user_id:
        return
    for watch in storage.get_watchlist(user_id):
        removed = storage.take_active_watch(user_id, watch["event_id"])
        if not removed:
            continue
        poller.cancel_snipe(user_id, removed["event_id"])
        cost = int(removed["coin_cost"] or 0)
        if cost:
            storage.release_coins(uid, cost)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sirius_client
    app.state.ready = False
    storage.init_db()
    storage.ensure_encryption_key()
    try:
        _ensure_vapid_private_key()
    except Exception as e:
        log.warning("Не удалось подготовить Web Push VAPID ключ: %s", e)
    if _fcm_is_configured():
        log.info("Android FCM push включён")
    else:
        log.warning("Android FCM push выключен: не найден %s", app_config.FCM_SERVICE_ACCOUNT_FILE)

    _sirius_client = SiriusClient(headless=True)
    try:
        await _sirius_client.start()
        log.info("Sirius браузер запущен")
        await _verify_existing_admin_tokens(_sirius_client)
    except Exception as e:
        log.error("Не удалось запустить Sirius браузер: %s", e)
        _sirius_client = None

    poller_task = asyncio.create_task(
        poller.run_poller(web_notify, _sirius_client)
    ) if _sirius_client else None

    reminder_task = asyncio.create_task(
        poller.run_reminder_checker(web_notify, _now)
    ) if _sirius_client else None
    radar_task = asyncio.create_task(sirius_radar.run_radar_alert_monitor(web_notify))
    app.state.ready = True
    try:
        yield
    finally:
        # Nginx turns this 503 into the shared maintenance page immediately.
        app.state.ready = False
        for t in [poller_task, reminder_task, radar_task]:
            if t:
                t.cancel()
        if _sirius_client:
            await _sirius_client.stop()


app = FastAPI(
    title="Пирожковый Диспетчер",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=jinja2.select_autoescape(),
    cache_size=0,
)
templates = Jinja2Templates(env=jinja_env)

templates.env.filters["nl2br"] = lambda v: v.replace("\n", "<br>\n")


@app.get("/.well-known/assetlinks.json")
async def android_asset_links():
    return JSONResponse([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": "ru.sonofstrange.siriusplus",
            "sha256_cert_fingerprints": [
                "6A:48:60:26:BE:83:F6:40:A5:8F:12:90:9B:2D:1B:0A:36:FC:9F:C9:13:3D:56:FF:F8:12:F2:CF:CF:DB:BE:11"
            ],
        },
    }])


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    """Require the private activation file and the official host."""
    if not getattr(request.app.state, "ready", False):
        return HTMLResponse(
            "<h1>Сервис перезагружается</h1><p>Подожди немного и обнови страницу.</p>",
            status_code=503,
        )
    if not app_config.instance_seal_is_valid():
        return HTMLResponse(
            "<h1>Экземпляр не активирован</h1>"
            "<p>Откройте официальный сервис через его домен.</p>",
            status_code=503,
        )
    host = (request.url.hostname or "").lower()
    if host != app_config.CANONICAL_HOST:
        return HTMLResponse(
            "<h1>Этот экземпляр не настроен</h1>"
            "<p>Откройте официальный сервис через его домен.</p>",
            status_code=403,
        )
    # Keep the session so an unblocked account can continue without a new login,
    # but deny every dynamic route while the UID is blocked.
    if not request.url.path.startswith("/static/") and request.url.path != "/healthz":
        ban = _account_ban_for_user(get_user_id(request))
        if ban:
            return _banned_account_response(request, ban, request.url.path.startswith("/api/"))
    return await call_next(request)

def fmt_ts(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, tz=_MSK).strftime("%d.%m %H:%M")
templates.env.filters["fmt_ts"] = fmt_ts


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        BASE_DIR / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/healthz", include_in_schema=False, status_code=204)
async def health_check():
    return Response(status_code=204 if getattr(app.state, "ready", False) else 503)


def _decode_jwt(token: str) -> dict | None:
    """Decode claims for display only; never use them as standalone authentication."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return None


def _team_from_schedule(events: list) -> str:
    counts: dict[str, int] = {}
    for event in events:
        for raw_team in getattr(event, "unions", []) or []:
            if isinstance(raw_team, dict):
                raw_team = raw_team.get("name") or raw_team.get("title") or raw_team.get("unionName")
            if not isinstance(raw_team, str) or not raw_team.strip():
                continue
            team = raw_team.strip()
            counts[team] = counts.get(team, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def _save_schedule_team(user_id: str, events: list):
    team = _team_from_schedule(events)
    if team:
        storage.update_known_team(user_id, team)


def _session_uid(user_id: str) -> str | None:
    if not storage.get_token(user_id):
        return None
    return storage.get_user_uid(user_id)


def _require_admin(request: Request) -> tuple[str | None, JSONResponse | None]:
    user_id = get_user_id(request)
    if not user_id:
        return None, JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    token = storage.get_token(user_id)
    if not token:
        return None, JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)
    uid = storage.get_user_uid(user_id)
    if not storage.is_admin(uid):
        return None, JSONResponse({"ok": False, "error": "Доступ запрещён"}, status_code=403)
    if not storage.is_token_verified(user_id, token):
        return None, JSONResponse({"ok": False, "error": "Токен администратора ещё не подтверждён Sirius"}, status_code=403)
    return uid, None


def _resolve_uid_session(request: Request, uid: str, current_user_id: str, session_id: str | None = None):
    """Привязывает сессию к стабильному UID: если пользователь с таким UID уже был,
    переносит туда сессию и данные."""
    if session_id is None:
        session_id = request.cookies.get("session_id")
    existing_user_id = storage.get_user_by_uid(uid)
    if existing_user_id and existing_user_id != current_user_id:
        log.info("Найден существующий пользователь uid=%s user_id=%s, мигрирую данные из %s",
                 uid, existing_user_id, current_user_id)
        storage.migrate_user_data(current_user_id, existing_user_id)
        storage.set_user_uid(existing_user_id, uid)
        if session_id:
            storage.update_session_user_id(session_id, existing_user_id)
        return existing_user_id
    else:
        storage.set_user_uid(current_user_id, uid)
        return current_user_id


def _render(template: str, request: Request, **kwargs):
    user_id = get_user_id(request)
    token = None
    if user_id:
        if hasattr(request.state, '_token') and request.state._token is not None:
            token = request.state._token
        else:
            token = storage.get_token(user_id)
            request.state._token = token
    exp = None
    token_status = "ok"
    if token:
        e = token_expiry(token)
        if e:
            exp = e.astimezone(_MSK).strftime("%d.%m.%Y %H:%M")
            remaining = (e - _now()).total_seconds()
            if remaining < 0:
                token_status = "expired"
            elif remaining < 24 * 3600:
                token_status = "warning"
    notifications = []
    if user_id and user_id in _notification_queue:
        notifications = _notification_queue.pop(user_id, [])
    user_info = _decode_jwt(token) if token else None
    session_uid = _session_uid(user_id) if user_id else None
    coins_balance = 0
    is_admin = False
    if session_uid:
        coins_balance = storage.get_coins(session_uid)
        is_admin = storage.is_admin(session_uid)
    login_type = storage.get_login_type(user_id)
    return templates.TemplateResponse(request, template, {
        "user_id": user_id,
        "user_info": user_info,
        "has_token": bool(token),
        "token_expiry": exp,
        "token_status": token_status,
        "coins_balance": coins_balance,
        "is_admin": is_admin,
        "login_type": login_type,
        "referral_code": request.cookies.get(REFERRAL_COOKIE, ""),
        "notifications": notifications,
    "fmt_dt": fmt_dt,
    "fmt_time": fmt_time,
    "fmt_countdown": _fmt_countdown,
    "now_utc": _now,
        **kwargs,
    })


# ---------- Pages ----------

def _decorate_auto_registration(event, watch, user_id: str, now: dt.datetime) -> None:
    """Attach presentation-only auto-registration state to an event."""
    priority = str(watch["snipe_priority"] or "high")
    priority_names = {
        "high": "Высокий приоритет",
        "medium": "Средний приоритет",
        "low": "Низкий приоритет",
    }
    if getattr(event, "event_type", "") == "communityEvent":
        target = _community_datetime(getattr(event, "registration_open_at", ""))
    else:
        target = poller.event_target_time(event)
    target = target or parse_sirius_time(watch["event_start"])
    is_active = poller.is_sniping(user_id, event.event_id)
    label = "Автозапись ожидает обновления"
    if target:
        remaining = (target - now).total_seconds()
        opening = target.astimezone(_MSK).strftime("%d.%m %H:%M")
        if is_active:
            label = "Ловит открытие сейчас"
        elif remaining > poller.WARMUP_WINDOW:
            label = f"Запланирована до открытия {opening}"
        elif remaining > 0:
            label = f"Подготовка к открытию {opening}"
        else:
            label = f"Ожидает запись, открытие было {opening}"

    event._watched = True
    event._watch = {
        "priority_code": priority,
        "priority": priority_names.get(priority, "Высокий приоритет"),
        "cost": int(watch["coin_cost"] or 0),
        "status": label,
        "active": is_active,
        "target": target.astimezone(_MSK).isoformat() if target else "",
    }

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    referral_code = storage.normalize_referral_code(request.query_params.get("ref", ""))
    if referral_code:
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            key=REFERRAL_COOKIE,
            value=referral_code,
            max_age=30 * 86400,
            httponly=True,
            samesite="lax",
        )
        return response
    user_id = get_user_id(request)
    if not user_id:
        error = request.query_params.get("error", "")
        return _render("login.html", request, error=error)
    return RedirectResponse(url="/schedule")


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, tab: str = "register", status: str = "all", date: str = "", q: str = "", sub: str = "current", page: int = 1, sort: str = "date"):
    user_id = get_user_id(request)
    if not user_id:
        return RedirectResponse(url="/")
    if tab == "notifications":
        return RedirectResponse(url="/events?tab=register", status_code=303)

    token = storage.get_token(user_id)
    if not token:
        return _render("login.html", request, error="Войди, чтобы продолжить")

    cache_key = f"events:{user_id}"
    all_events = _get_cached(cache_key)
    error = None
    refresh_message = None
    background_refresh = False
    if all_events is None:
        stale_events = _get_cached_any(cache_key)
        if stale_events is not None:
            all_events = stale_events
            background_refresh = True
            refresh_message = "Обновляю..."
        else:
            stored_events = _get_persistent_events_cache(user_id)
            if stored_events is not None:
                all_events = stored_events
                background_refresh = True
                refresh_message = "Обновляю..."
            elif _sirius_client:
                all_events = []
                background_refresh = True
                refresh_message = "Сейчас данные загружаются из Sirius. Обычно это занимает до 1 минуты."
            else:
                all_events = []
                error = "Sirius клиент не запущен"
    elif _sirius_client is None:
        error = "Sirius клиент не запущен"

    session_uid = _session_uid(user_id) or ""
    now = _now()
    watches = {w["event_id"]: w for w in storage.get_watchlist(user_id)}

    community_events = [
        _community_event_for_events_page(event, user_id, storage.is_admin(session_uid))
        for event in storage.get_community_events_for_user(user_id)
    ]
    all_events = list(all_events) + community_events

    for ev in all_events:
        ev._watched = False
        ev._watch = None
        watch = watches.get(ev.event_id)
        if watch:
            _decorate_auto_registration(ev, watch, user_id, now)

    # Compute overlaps: which events overlap with user's registered/watched events
    _user_event_names = {ev.event_id: ev.event_name for ev in all_events}
    user_ids = {ev.event_id for ev in all_events if ev.is_recorded or ev.is_reserved or ev.event_id in watches}
    user_ranges = []
    for ev in all_events:
        if ev.event_id in user_ids:
            s = parse_sirius_time(ev.event_start)
            e_raw = ev.raw.get("eventEnd")
            e = parse_sirius_time(e_raw) if e_raw else None
            if s and e:
                user_ranges.append((ev.event_id, s, e))
    for ev in all_events:
        ev._conflict = False
        ev._conflict_with = []
        if ev.event_id in user_ids:
            continue
        s = parse_sirius_time(ev.event_start)
        e_raw = ev.raw.get("eventEnd")
        e = parse_sirius_time(e_raw) if e_raw else None
        if not s or not e:
            continue
        for uid, us, ue in user_ranges:
                if s < ue and us < e:
                    ev._conflict = True
                    ev._conflict_with.append(_user_event_names.get(uid, uid))

    filtered = []
    for ev in all_events:
        is_past = (
            bool(getattr(ev, "community_is_finished", False))
            if getattr(ev, "event_type", "") == "communityEvent"
            else bool(parse_sirius_time(ev.event_start) and parse_sirius_time(ev.event_start) < now)
        )
        if tab == "my":
            in_my = ev.event_id in watches or ev.is_recorded or ev.is_reserved
            if not in_my:
                continue
            if sub == "current" and is_past:
                continue
            if sub == "past" and not is_past:
                continue
            if sub == "watch" and ev.event_id not in watches:
                continue
        else:
            if status == "all" and is_past:
                continue
            if status == "past" and not is_past:
                continue
            if status == "open" and not ev.is_available:
                continue
            if status == "willopen":
                t = parse_sirius_time(ev.will_open_at)
                if not t or t < now:
                    continue
            if status == "reserve":
                if is_past or not ev.is_available or ev.people_current < ev.people_max:
                    continue
        if date and ev.day_iso != date:
            continue
        filtered.append(ev)

    if q:
        ql = q.lower()
        filtered = [ev for ev in filtered if ql in ev.event_name.lower()]

    # Сортировка
    if sort == "register":
        filtered.sort(key=lambda ev: parse_sirius_time(ev.will_open_at) or dt.datetime.max.replace(tzinfo=_MSK))
    elif sort == "alpha":
        filtered.sort(key=lambda ev: ev.event_name.lower())
    elif sort == "capacity":
        filtered.sort(key=lambda ev: ev.people_max or 0, reverse=True)
    elif sort == "free":
        filtered.sort(key=lambda ev: (ev.people_max or 0) - (ev.people_current or 0), reverse=True)
    else:  # date (default)
        filtered.sort(key=lambda ev: parse_sirius_time(ev.event_start) or dt.datetime.max.replace(tzinfo=_MSK))

    for ev in filtered:
        ev._is_past = (
            bool(getattr(ev, "community_is_finished", False))
            if getattr(ev, "event_type", "") == "communityEvent"
            else _is_past_event(ev)
        )

    dates = sorted(set(e.day_iso for e in all_events if e.day_iso))

    total = len(filtered)
    offset = (page - 1) * PAGE_SIZE
    paged = filtered[offset:offset + PAGE_SIZE]
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    return _render("events.html", request, events=paged, error=error,
                   cur_status=status, cur_date=date, dates=dates, search_q=q,
                   cur_tab=tab, cur_sub=sub, page=page, total_pages=total_pages, total=total,
                   cur_sort=sort, background_refresh=background_refresh, refresh_message=refresh_message)


@app.get("/schedule", response_class=HTMLResponse)
async def schedule_page(request: Request, date: str = "", q: str = ""):
    user_id = get_user_id(request)
    if not user_id:
        return RedirectResponse(url="/")

    token = storage.get_token(user_id)
    if not token:
        return _render("login.html", request, error="Войди, чтобы продолжить")

    now = _now().astimezone(_MSK)
    if not date:
        date = now.strftime("%Y-%m-%d")

    WEEKDAYS = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    date_list = []
    for i in range(-7, 15):
        d = now + dt.timedelta(days=i)
        date_list.append({
            "iso": d.strftime("%Y-%m-%d"),
            "day": d.strftime("%d"),
            "weekday": WEEKDAYS[d.weekday()] if i != 0 else "Сегодня",
            "is_today": i == 0,
        })

    cache_key = f"schedule:{user_id}:{date}"
    cached = _get_cached(cache_key)
    error = None
    if cached is None:
        cached = []
        if _sirius_client:
            try:
                storage.increment_sirius_request(user_id)
                cached = await _sirius_client.fetch_schedule_day(date, token=token)
                _set_cached(cache_key, cached)
                _save_schedule_team(user_id, cached)
            except Exception as e:
                error = str(e)
        else:
            error = "Sirius клиент не запущен"
    elif _sirius_client is None:
        error = "Sirius клиент не запущен"
    events = list(cached)

    def _to_iso(d: str, t: str) -> str:
        if not t:
            return ""
        t = t.strip()
        if "T" in t or "+" in t:
            return t
        parts = t.split(":")
        if len(parts) == 2:
            return f"{d}T{t}:00+03:00"
        return f"{d}T{t}+03:00"

    custom_evs = storage.get_custom_events_for_date(user_id, date)
    for ce in custom_evs:
        st = _to_iso(date, ce["start_time"])
        et = _to_iso(date, ce["end_time"])
        events.append(type("_CustomEvent", (), {
            "event_id": f"custom_{ce['id']}",
            "event_name": ce["event_name"],
            "event_type": "customEvent",
            "start_time": st,
            "end_time": et,
            "date_iso": date,
            "status": "",
            "location": [ce["location"]] if ce["location"] else [],
            "tutors": [],
            "description": ce["description"],
            "people_max": 0,
            "people_current": 0,
            "people_reserved": 0,
            "record_end": None,
            "transport_info": None,
            "departure_location": "",
            "arrival_location": "",
            "unions": [],
        })())

    session_uid = _session_uid(user_id) or ""
    for community_event in storage.get_community_events_for_date(user_id, date):
        if not community_event["is_registered"]:
            continue
        events.append(type("_CommunityEvent", (), _community_event_payload(
            community_event, user_id, storage.is_admin(session_uid)
        ))())

    if q.strip():
        needle = q.casefold().strip()
        def _schedule_matches(event) -> bool:
            parts = [event.event_name, event.description]
            parts.extend(event.location or [])
            parts.extend(event.tutors or [])
            parts.extend(event.unions or [])
            if event.event_type == "communityEvent":
                parts.extend([event.community_owner, event.community_contact])
            return needle in " ".join(str(part or "") for part in parts).casefold()
        events = [event for event in events if _schedule_matches(event)]

    events.sort(key=lambda e: parse_sirius_time(e.start_time) or dt.datetime.max.replace(tzinfo=dt.timezone.utc))

    groups = sorted(set(g for ev in events for g in ev.unions))
    reminder_event_ids = {r["event_id"] for r in storage.get_reminders_for_user(user_id)}

    return _render(
        "schedule.html",
        request,
        events=events,
        error=error,
        cur_date=date,
        dates=date_list,
        groups=groups,
        search_q=q,
        reminder_event_ids=reminder_event_ids,
    )


@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page(request: Request):
    return RedirectResponse(url="/events?tab=my&sub=watch", status_code=303)


@app.get("/custom-events", response_class=HTMLResponse)
async def custom_events_page(request: Request, date: str = ""):
    user_id = get_user_id(request)
    if not user_id:
        return RedirectResponse(url="/")
    events = storage.get_custom_events(user_id, date) if date else storage.get_custom_events(user_id)
    community_events = storage.get_managed_community_events(user_id)
    today = _now().astimezone(_MSK).strftime("%Y-%m-%d")
    return _render(
        "custom_events.html", request, custom_events=events,
        community_events=community_events, cur_date=today,
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user_id = get_user_id(request)
    if user_id:
        return RedirectResponse(url="/events?tab=register")
    return _render("login.html", request)


@app.get("/howto", response_class=HTMLResponse)
async def howto_page(request: Request):
    return _render("howto.html", request)


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    return _render("help.html", request)


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return _render("privacy.html", request, consent_version=PERSONAL_DATA_CONSENT_VERSION)


@app.get("/coins-info", response_class=HTMLResponse)
async def coins_info_page(request: Request):
    user_id = get_user_id(request)
    uid = _session_uid(user_id) if user_id else None
    tab = request.query_params.get("tab", "coins")
    if tab == "polymarket":
        if not user_id:
            return RedirectResponse(url="/")
        return _render("polymarket.html", request, coins_tab="polymarket")
    return _render("coins_info.html", request,
                   app_bonus_claimed=bool(uid and storage.has_claimed_app_usage_bonus(uid)),
                   coins_tab="dronebet" if tab == "dronebet" else "coins")


@app.get("/polymarket", response_class=HTMLResponse)
async def polymarket_page(request: Request):
    return RedirectResponse(url="/coins-info?tab=polymarket", status_code=303)


@app.get("/dronebet", response_class=HTMLResponse)
async def dronebet_page(request: Request):
    return RedirectResponse(url="/coins-info?tab=dronebet", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return RedirectResponse(url="/")
    admin_uid, denied = _require_admin(request)
    if denied or not admin_uid:
        return _render("admin_denied.html", request)
    return _render("admin.html", request)


# ---------- API ----------

@app.post("/api/token")
async def api_set_token(request: Request):
    user_id = get_user_id(request)
    is_new_session = user_id is None
    session_id = request.cookies.get("session_id") if user_id else None

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "Тело запроса должно быть JSON"}, status_code=400)
    token = data.get("token", "").strip()
    if not _has_personal_data_consent(data.get("personal_data_consent")):
        return _personal_data_consent_required(wants_json=True)
    if not token:
        return JSONResponse({"ok": False, "error": "Токен не может быть пустым"}, status_code=400)

    exp = token_expiry(token)
    if not exp:
        return JSONResponse({"ok": False, "error": "Неверный формат токена"}, status_code=400)

    if not _sirius_client:
        return JSONResponse({"ok": False, "error": "Sirius клиент не запущен"}, status_code=503)
    try:
        await _sirius_client.fetch_schedule(token=token)
    except Exception:
        return JSONResponse({"ok": False, "error": "Sirius не подтвердил этот токен"}, status_code=401)

    payload = _decode_jwt(token)
    if not payload or not payload.get("id"):
        return JSONResponse({"ok": False, "error": "Не удалось определить аккаунт Sirius"}, status_code=400)

    uid = payload["id"]
    ban = storage.get_account_ban(uid)
    if ban:
        return _banned_account_response(request, ban, wants_json=True)
    if is_new_session:
        user_id = storage.get_user_by_uid(uid) or uid
    else:
        user_id = _resolve_uid_session(request, uid, user_id, session_id)
    storage.increment_sirius_request(user_id)
    storage.save_token(user_id, token)
    storage.set_user_uid(user_id, uid)
    storage.mark_token_verified(user_id, token)
    storage.set_login_type(user_id, "token")

    storage.ensure_coins(uid)
    storage.record_personal_data_consent(uid, PERSONAL_DATA_CONSENT_VERSION)
    full_name = " ".join(filter(None, [payload.get("lastName"), payload.get("firstName"), payload.get("middleName")]))
    storage.save_known_uid(uid, user_id, full_name)

    response = JSONResponse({"ok": True, "token_set": True})
    if is_new_session:
        session_id = storage.create_session_for_user(user_id)
        response.set_cookie(key="session_id", value=session_id, max_age=86400 * 365, httponly=True, samesite="lax")
    return response


@app.post("/api/login")
async def api_login(request: Request):
    user_id = get_user_id(request)
    is_new_session = user_id is None
    session_id = request.cookies.get("session_id") if user_id else None
    wants_json = request.headers.get("x-requested-with") == "XMLHttpRequest"

    if not _sirius_client:
        return _login_error_response("Сервис входа временно недоступен. Попробуй через минуту.", wants_json, 503)

    # Accept both JSON (old clients) and form data (new form)
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        email = data.get("email", "").strip()
        password = data.get("password", "").strip()
        referral_code = data.get("referral_code", "").strip()
        personal_data_consent = data.get("personal_data_consent")
    else:
        form_data = await request.form()
        email = form_data.get("email", "").strip()
        password = form_data.get("password", "").strip()
        referral_code = form_data.get("referral_code", "").strip()
        personal_data_consent = form_data.get("personal_data_consent")

    if not email or not password:
        return _login_error_response("Укажи email и пароль от Sirius.", wants_json)

    if not _has_personal_data_consent(personal_data_consent):
        return _personal_data_consent_required(wants_json)

    try:
        token = await _sirius_client.login(email, password)
    except Exception as e:
        return _login_error_response(f"Не удалось войти: {_friendly_error(e)}", wants_json)

    if not token:
        return _login_error_response(
            "Sirius не подтвердил вход по паролю. Проверь email и пароль. "
            "Если они верные, войди по одноразовому коду из письма: Sirius иногда требует этот способ.",
            wants_json,
            401,
        )

    exp = token_expiry(token)
    if not exp:
        return _login_error_response("Sirius вернул некорректный токен. Попробуй войти ещё раз.", wants_json)

    payload = _decode_jwt(token)
    if not payload or not payload.get("id"):
        return _login_error_response("Не удалось определить аккаунт Sirius. Попробуй войти ещё раз.", wants_json)

    uid = payload["id"]
    ban = storage.get_account_ban(uid)
    if ban:
        if wants_json:
            return _banned_account_response(request, ban, wants_json=True)
        return _banned_account_response(request, ban)
    is_first_sirius_login = storage.get_user_by_uid(uid) is None
    if is_new_session:
        user_id = storage.get_user_by_uid(uid) or uid
    else:
        user_id = _resolve_uid_session(request, uid, user_id, session_id)

    storage.save_token(user_id, token)
    storage.set_user_uid(user_id, uid)
    storage.mark_token_verified(user_id, token)
    storage.save_login_credentials(user_id, email, password)
    log.info("login: токен и данные входа сохранены для uid=%s", uid)

    storage.ensure_coins(uid)
    storage.record_personal_data_consent(uid, PERSONAL_DATA_CONSENT_VERSION)
    full_name = " ".join(filter(None, [payload.get("lastName"), payload.get("firstName"), payload.get("middleName")]))
    storage.save_known_uid(uid, user_id, full_name)

    referral_code = storage.normalize_referral_code(referral_code or request.cookies.get(REFERRAL_COOKIE, ""))
    referral_applied = is_first_sirius_login and storage.apply_referral(referral_code, uid)
    if referral_applied:
        storage.add_notification(uid, "🎁 Реферальный код применён: тебе начислено 5 Сириус Коинов.", "success")
        referrer_uid = storage.get_referrer_uid(uid)
        if referrer_uid:
            storage.add_notification(referrer_uid, "🎁 Друг зарегистрировался по твоей ссылке: начислено 5 Сириус Коинов.", "success")

    redirect_url = "/schedule"
    response = JSONResponse({"ok": True, "redirect": redirect_url}) if wants_json else RedirectResponse(
        url=redirect_url, status_code=303
    )
    if is_new_session:
        session_id = storage.create_session_for_user(user_id)
        response.set_cookie(key="session_id", value=session_id, max_age=86400 * 365, httponly=True, samesite="lax")
    log.info("login: сессия готова для uid=%s, новая=%s", uid, is_new_session)
    if referral_code:
        response.delete_cookie(REFERRAL_COOKIE)
    return response


@app.post("/api/login-email-code/request")
async def api_request_email_login_code(request: Request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "Тело запроса должно быть JSON."}, status_code=400)
    if not _has_personal_data_consent(data.get("personal_data_consent")):
        return _personal_data_consent_required(wants_json=True)
    email = str(data.get("email", "")).strip()
    if not email or len(email) > 254 or "@" not in email:
        return JSONResponse({"ok": False, "error": "Введи корректный email."}, status_code=400)
    if not _sirius_client:
        return JSONResponse({"ok": False, "error": "Sirius клиент не запущен."}, status_code=503)
    try:
        attempt_id = await _sirius_client.begin_email_code_login(email)
    except Exception as exc:
        log.warning("email-code login request failed: %s", exc)
        return JSONResponse({"ok": False, "error": _friendly_error(exc)}, status_code=503)
    _remember_email_code_attempt(attempt_id, None, email)
    return JSONResponse({"ok": True, "attempt_id": attempt_id})


@app.post("/api/login-email-code/confirm")
async def api_confirm_email_login_code(request: Request):
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "Тело запроса должно быть JSON."}, status_code=400)
    if not _has_personal_data_consent(data.get("personal_data_consent")):
        return _personal_data_consent_required(wants_json=True)
    attempt_id = str(data.get("attempt_id", "")).strip()
    code = str(data.get("code", "")).strip()
    attempt = _email_code_attempt_users.get(attempt_id)
    if not attempt or attempt[0] is not None:
        return JSONResponse({"ok": False, "error": "Запрос кода не найден. Запроси новый код."}, status_code=400)
    if not code or len(code) > 32:
        return JSONResponse({"ok": False, "error": "Введи код из письма."}, status_code=400)
    if not _sirius_client:
        return JSONResponse({"ok": False, "error": "Sirius клиент не запущен."}, status_code=503)
    try:
        token = await _sirius_client.complete_email_code_login(attempt_id, code)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": _friendly_error(exc)}, status_code=400)
    if not token:
        return JSONResponse({"ok": False, "error": "Sirius не подтвердил код. Запроси новый."}, status_code=400)
    _email_code_attempt_users.pop(attempt_id, None)
    return _finish_email_code_login(request, token, attempt[1], str(data.get("referral_code", "")))


@app.post("/api/login-code/create")
async def api_create_login_code(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)

    code, expires_at = storage.create_login_code(user_id)
    return JSONResponse({
        "ok": True,
        "code": code,
        "expires_at": expires_at,
        "ttl_seconds": max(0, expires_at - int(time.time())),
    })


@app.post("/api/login-code")
async def api_login_code(request: Request):
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        code = data.get("code", "").strip()
        personal_data_consent = data.get("personal_data_consent")
        wants_json = True
    else:
        form_data = await request.form()
        code = form_data.get("code", "").strip()
        personal_data_consent = form_data.get("personal_data_consent")
        wants_json = False

    if not _has_personal_data_consent(personal_data_consent):
        return _personal_data_consent_required(wants_json)

    user_id = storage.consume_login_code(code)
    if not user_id:
        if wants_json:
            return JSONResponse({"ok": False, "error": "Код неверный или уже истёк"}, status_code=400)
        return RedirectResponse(url=f"/?error={quote('Код неверный или уже истёк')}", status_code=303)

    ban = _account_ban_for_user(user_id)
    if ban:
        return _banned_account_response(request, ban, wants_json=wants_json)

    session_id = storage.create_session_for_user(user_id)
    uid = _session_uid(user_id)
    if uid:
        storage.record_personal_data_consent(uid, PERSONAL_DATA_CONSENT_VERSION)
    if wants_json:
        response = JSONResponse({"ok": True})
    else:
        response = RedirectResponse(url="/schedule", status_code=303)
    response.set_cookie(key="session_id", value=session_id, max_age=86400 * 365, httponly=True, samesite="lax")
    return response


@app.post("/api/refresh-token/request-code")
async def api_request_refresh_email_code(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован."}, status_code=401)
    if storage.get_login_type(user_id) != "email_code":
        return JSONResponse({"ok": False, "error": "Этот способ входа не использует код из письма."}, status_code=400)
    email = storage.get_login_email(user_id)
    if not email:
        return JSONResponse({"ok": False, "error": "Email для обновления токена не сохранён. Войди заново."}, status_code=400)
    if not _sirius_client:
        return JSONResponse({"ok": False, "error": "Sirius клиент не запущен."}, status_code=503)
    try:
        attempt_id = await _sirius_client.begin_email_code_login(email)
    except Exception as exc:
        log.warning("email-code token refresh request failed for %s: %s", user_id, exc)
        return JSONResponse({"ok": False, "error": _friendly_error(exc)}, status_code=503)
    _remember_email_code_attempt(attempt_id, user_id, email)
    return JSONResponse({"ok": True, "attempt_id": attempt_id})


@app.post("/api/refresh-token/confirm-code")
async def api_confirm_refresh_email_code(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован."}, status_code=401)
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "Тело запроса должно быть JSON."}, status_code=400)
    attempt_id = str(data.get("attempt_id", "")).strip()
    code = str(data.get("code", "")).strip()
    attempt = _email_code_attempt_users.get(attempt_id)
    if not attempt or attempt[0] != user_id:
        return JSONResponse({"ok": False, "error": "Запрос кода не найден. Запроси новый код."}, status_code=400)
    if not code or len(code) > 32:
        return JSONResponse({"ok": False, "error": "Введи код из письма."}, status_code=400)
    if not _sirius_client:
        return JSONResponse({"ok": False, "error": "Sirius клиент не запущен."}, status_code=503)
    try:
        token = await _sirius_client.complete_email_code_login(attempt_id, code)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": _friendly_error(exc)}, status_code=400)
    if not token or not token_expiry(token):
        return JSONResponse({"ok": False, "error": "Sirius не подтвердил код. Запроси новый."}, status_code=400)
    _email_code_attempt_users.pop(attempt_id, None)
    storage.save_token(user_id, token)
    storage.mark_token_verified(user_id, token)
    storage.set_last_token_refresh(user_id)
    return JSONResponse({"ok": True})


@app.post("/api/refresh-token")
async def api_refresh_token(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)

    login_type = storage.get_login_type(user_id)
    if login_type == "password":
        log.info("token refresh requested for user_id=%s", user_id)
        # Автообновление через пароль с КД 4 часа
        last = storage.get_last_token_refresh(user_id)
        if time.time() - last < 4 * 3600:
            remaining = 4 * 3600 - int(time.time() - last)
            h = remaining // 3600
            m = (remaining % 3600) // 60
            return JSONResponse({"ok": False, "error": f"Подожди ещё {h}ч {m}м перед следующим обновлением"}, status_code=429)

        creds = storage.get_login_credentials(user_id)
        if not creds:
            return JSONResponse({"ok": False, "error": "Нет сохранённых данных для входа"}, status_code=400)

        if not _sirius_client:
            return JSONResponse({"ok": False, "error": "Sirius клиент не запущен"}, status_code=503)

        try:
            token = await _sirius_client.login(creds[0], creds[1])
        except Exception as e:
            log.warning("token refresh failed for user_id=%s: %s", user_id, e)
            return JSONResponse({"ok": False, "error": f"Ошибка входа: {_friendly_error(e)}"}, status_code=500)

        if not token:
            log.warning("token refresh returned no token for user_id=%s", user_id)
            return JSONResponse({"ok": False, "error": "Sirius не успел завершить вход. Возможно, сайт отвечает медленно — попробуй ещё раз."}, status_code=503)

        storage.save_token(user_id, token)
        storage.mark_token_verified(user_id, token)
        storage.set_last_token_refresh(user_id)
        log.info("token refresh succeeded for user_id=%s", user_id)
        return JSONResponse({"ok": True, "method": "auto"})

    elif login_type == "token":
        # Ручной ввод нового токена
        data = await request.json()
        new_token = data.get("token", "").strip()
        if not new_token:
            return JSONResponse({"ok": False, "error": "Токен не может быть пустым"}, status_code=400)

        exp = token_expiry(new_token)
        if not exp:
            return JSONResponse({"ok": False, "error": "Неверный формат токена"}, status_code=400)

        storage.save_token(user_id, new_token)
        return JSONResponse({"ok": True, "method": "manual"})

    else:
        return JSONResponse({"ok": False, "error": "Неизвестный тип входа"}, status_code=400)


def _prediction_market_view(market, viewer_uid: str) -> dict:
    try:
        options = json.loads(market["options_json"])
    except (TypeError, json.JSONDecodeError):
        options = []
    bets = storage.get_prediction_bets(int(market["id"]))
    is_admin_viewer = storage.is_admin(viewer_uid)
    total_pool = sum(int(bet["amount"]) for bet in bets)
    now = int(time.time())
    betting_open = (
        market["status"] == "open"
        and (not market["end_at"] or now < market["end_at"])
        and (not market["betting_closes_at"] or now < market["betting_closes_at"])
    )
    view = {
        "id": market["id"],
        "title": market["title"],
        "description": market["description"],
        "type": market["market_type"],
        "status": market["status"],
        "end_at": market["end_at"],
        "betting_closes_at": market["betting_closes_at"],
        "created_at": market["created_at"],
        "correct_option": market["correct_option"] if market["status"] == "resolved" else "",
        "correct_value": market["correct_value"] if market["status"] == "resolved" else None,
        "total_pool": total_pool,
        "bet_count": len(bets),
        "betting_open": betting_open,
        "my_bets": [
            {
                "selection": bet["selection"],
                "value": bet["value"],
                "amount": bet["amount"],
                "payout": bet["payout"],
            }
            for bet in bets if bet["uid"] == viewer_uid
        ],
    }
    if market["market_type"] == "choice":
        totals = {option: 0 for option in options}
        for bet in bets:
            totals[bet["selection"]] = totals.get(bet["selection"], 0) + int(bet["amount"])
        view["options"] = [
            {
                "name": option,
                "pool": totals.get(option, 0),
                "multiplier": round(total_pool / totals[option], 2) if totals.get(option, 0) else 1.0,
            }
            for option in options
        ]
    else:
        minimum = float(market["min_value"])
        maximum = float(market["max_value"])
        bin_count = 60
        bins = [0] * bin_count
        span = maximum - minimum
        for bet in bets:
            value = float(bet["value"])
            index = min(bin_count - 1, max(0, int((value - minimum) / span * bin_count)))
            bins[index] += int(bet["amount"])
        busiest = max(bins, default=0)
        view.update({
            "min_value": minimum,
            "max_value": maximum,
            "unit": market["unit"],
            "heat_max_pool": busiest,
            "preset_correct_value": market["correct_value"] if is_admin_viewer and market["status"] == "open" else None,
            "heat_bins": [
                {"pool": amount, "density": round(amount / busiest, 3) if busiest else 0}
                for amount in bins
            ],
        })
    return view


@app.get("/api/polymarket")
async def api_polymarket(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    uid = _session_uid(user_id)
    if not uid:
        return JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)
    return JSONResponse({
        "ok": True,
        "coins": storage.get_coins(uid),
        "is_admin": storage.is_admin(uid),
        "markets": [_prediction_market_view(market, uid) for market in storage.get_prediction_markets()],
    })


@app.post("/api/polymarket/{market_id}/bet")
async def api_place_polymarket_bet(market_id: int, request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    uid = _session_uid(user_id)
    if not uid:
        return JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)
    market = storage.get_prediction_market(market_id)
    if not market:
        return JSONResponse({"ok": False, "error": "Рынок не найден"}, status_code=404)
    try:
        data = await request.json()
        amount = int(data.get("amount", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return JSONResponse({"ok": False, "error": "Некорректная ставка"}, status_code=400)

    selection = ""
    value = None
    if market["market_type"] == "choice":
        selection = str(data.get("selection", "")).strip()
        try:
            options = json.loads(market["options_json"])
        except json.JSONDecodeError:
            options = []
        if selection not in options:
            return JSONResponse({"ok": False, "error": "Выбери вариант из списка"}, status_code=400)
    else:
        try:
            value = float(data.get("value"))
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "Введи число"}, status_code=400)
        if not math.isfinite(value) or value < float(market["min_value"]) or value > float(market["max_value"]):
            return JSONResponse({"ok": False, "error": "Число вне допустимого диапазона"}, status_code=400)
        selection = f"{value:g}"

    ok, error, balance = storage.place_prediction_bet(uid, market_id, selection, value, amount)
    if not ok:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    return JSONResponse({"ok": True, "new_balance": balance})


@app.post("/api/admin/polymarket")
async def api_create_polymarket(request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "Тело запроса должно быть JSON"}, status_code=400)
    title = str(data.get("title", "")).strip()
    description = str(data.get("description", "")).strip()
    market_type = data.get("type")
    if not title or len(title) > 180:
        return JSONResponse({"ok": False, "error": "Укажи вопрос до 180 символов"}, status_code=400)
    if market_type not in ("choice", "number"):
        return JSONResponse({"ok": False, "error": "Неизвестный тип рынка"}, status_code=400)
    try:
        end_at = int(data.get("end_at") or 0)
        betting_closes_at = int(data.get("betting_closes_at") or 0)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "Некорректное время"}, status_code=400)
    now = int(time.time())
    if end_at and end_at <= now:
        return JSONResponse({"ok": False, "error": "Время окончания должно быть в будущем"}, status_code=400)
    if betting_closes_at and betting_closes_at <= now:
        return JSONResponse({"ok": False, "error": "Время приёма ставок должно быть в будущем"}, status_code=400)
    if end_at and betting_closes_at and betting_closes_at > end_at:
        return JSONResponse({"ok": False, "error": "Приём ставок не может закончиться позже рынка"}, status_code=400)

    options_json, minimum, maximum, unit, correct_value = "[]", None, None, "", None
    if market_type == "choice":
        options = [str(option).strip() for option in data.get("options", []) if str(option).strip()]
        if len(options) < 2 or len(options) > 10 or len(set(options)) != len(options):
            return JSONResponse({"ok": False, "error": "Добавь от 2 до 10 уникальных вариантов"}, status_code=400)
        options_json = json.dumps(options, ensure_ascii=False)
    else:
        try:
            minimum = float(data.get("min_value"))
            maximum = float(data.get("max_value"))
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "Укажи числовой диапазон"}, status_code=400)
        if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum >= maximum:
            return JSONResponse({"ok": False, "error": "Минимум должен быть меньше максимума"}, status_code=400)
        unit = str(data.get("unit", "")).strip()
        if len(unit) > 24:
            return JSONResponse({"ok": False, "error": "Единица измерения слишком длинная"}, status_code=400)
        preset = data.get("correct_value")
        if preset not in (None, ""):
            try:
                correct_value = float(preset)
            except (ValueError, TypeError):
                return JSONResponse({"ok": False, "error": "Некорректный правильный ответ"}, status_code=400)
            if not math.isfinite(correct_value) or correct_value < minimum or correct_value > maximum:
                return JSONResponse({"ok": False, "error": "Правильный ответ вне диапазона"}, status_code=400)

    market_id = storage.create_prediction_market(
        title, description, market_type, options_json, minimum, maximum, end_at, betting_closes_at,
        admin_uid, unit, correct_value
    )
    return JSONResponse({"ok": True, "market_id": market_id})


@app.post("/api/admin/polymarket/{market_id}/resolve")
async def api_resolve_polymarket(market_id: int, request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied
    market = storage.get_prediction_market(market_id)
    if not market:
        return JSONResponse({"ok": False, "error": "Рынок не найден"}, status_code=404)
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "Тело запроса должно быть JSON"}, status_code=400)
    option, value = "", None
    if market["market_type"] == "choice":
        option = str(data.get("correct_option", "")).strip()
        try:
            options = json.loads(market["options_json"])
        except json.JSONDecodeError:
            options = []
        if option not in options:
            return JSONResponse({"ok": False, "error": "Выбери правильный вариант"}, status_code=400)
    else:
        preset = data.get("correct_value", market["correct_value"])
        try:
            value = float(preset)
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "Укажи правильное число"}, status_code=400)
        if not math.isfinite(value) or value < float(market["min_value"]) or value > float(market["max_value"]):
            return JSONResponse({"ok": False, "error": "Правильное число вне диапазона"}, status_code=400)
    ok, error, payouts = storage.resolve_prediction_market(market_id, option, value)
    if not ok:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    for uid, payout in payouts:
        recipient = storage.get_user_by_uid(uid)
        if recipient:
            text = (
                f"📈 Рынок «{market['title']}» рассчитан. Ты получил {payout} Сириус Коин(ов)."
                if payout else f"📉 Рынок «{market['title']}» рассчитан. Эта ставка не принесла коинов."
            )
            await web_notify(recipient, text)
            storage.add_notification(recipient, text)
    return JSONResponse({"ok": True})


@app.delete("/api/admin/polymarket/{market_id}")
async def api_delete_polymarket(market_id: int, request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied
    market = storage.get_prediction_market(market_id)
    ok, error = storage.delete_prediction_market(market_id)
    if not ok:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    return JSONResponse({"ok": True})


@app.post("/api/admin/polymarket/{market_id}/cancel")
async def api_cancel_polymarket(market_id: int, request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied
    market = storage.get_prediction_market(market_id)
    if not market:
        return JSONResponse({"ok": False, "error": "Рынок не найден"}, status_code=404)
    ok, error, refunds = storage.cancel_prediction_market(market_id)
    if not ok:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    for uid, refund in refunds:
        recipient = storage.get_user_by_uid(uid)
        if recipient:
            text = f"↩️ Рынок «{market['title']}» отменён. Возвращено {refund} Сириус Коин(ов)."
            await web_notify(recipient, text)
            storage.add_notification(recipient, text)
    return JSONResponse({"ok": True})


@app.get("/api/coins/balance")
async def api_coins_balance(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    uid = _session_uid(user_id)
    if not uid:
        return JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)
    return JSONResponse({"ok": True, "coins": storage.get_coins(uid), "total": storage.get_coins_total(uid), "reserved": storage.get_coins_reserved(uid), "uid": uid})


def _partner_client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def _partner_request_allowed(request: Request) -> bool:
    """Small application-level backstop; Nginx does the primary rate limiting."""
    now = time.monotonic()
    ip = _partner_client_ip(request)
    window = _partner_request_windows.setdefault(ip, deque())
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= 60:
        return False
    window.append(now)
    return True


def _require_dronebet_partner(request: Request) -> JSONResponse | None:
    expected = app_config.DRONEBET_INBOUND_TOKEN
    if not expected:
        log.error("DroneBet partner API is requested but DRONEBET_INBOUND_TOKEN is not configured")
        return JSONResponse({"ok": False, "code": "partner_api_disabled"}, status_code=503)
    authorization = request.headers.get("authorization", "")
    supplied = authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
    if not supplied or not secrets.compare_digest(supplied, expected):
        return JSONResponse({"ok": False, "code": "unauthorized"}, status_code=401)
    if not _partner_request_allowed(request):
        return JSONResponse({"ok": False, "code": "rate_limited"}, status_code=429)
    return None


def _partner_json_error(code: str, status_code: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, **extra}, status_code=status_code)


async def _dronebet_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict, bool]:
    """Call DroneBet from the server. The boolean means that retry is needed."""
    token = app_config.DRONEBET_OUTBOUND_TOKEN
    if not token:
        return 503, {"ok": False, "code": "integration_not_configured"}, False
    url = f"{app_config.DRONEBET_API_BASE}{path}"

    def send() -> tuple[int, dict, bool]:
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                     **({"Content-Type": "application/json"} if body is not None else {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                raw = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw) if raw else {}
                return response.status, data if isinstance(data, dict) else {}, False
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {}
            return exc.code, data if isinstance(data, dict) else {}, False
        except (urllib.error.URLError, TimeoutError, OSError):
            return 0, {"ok": False, "code": "partner_unreachable"}, True

    return await asyncio.to_thread(send)


def _dronebet_error_message(status: int, data: dict) -> str:
    code = str(data.get("code") or data.get("error") or "")
    messages = {
        "invalid_link_code": "Код DroneBet неверный, истёк или уже использован.",
        "user_not_found": "Код DroneBet не найден, истёк или уже использован.",
        "sirius_account_already_linked": "Этот аккаунт уже связан с другим DroneBet-аккаунтом.",
        "drone_account_already_linked": "Этот DroneBet-аккаунт уже связан с другим пользователем.",
        "account_not_linked": "Сначала свяжи аккаунт с DroneBet.",
        "insufficient_coins": "На DroneBet недостаточно печенек для обмена.",
        "integration_not_configured": "Интеграция DroneBet ещё не настроена на сервере.",
        "partner_unreachable": "DroneBet временно не отвечает. Попробуй ещё раз.",
    }
    if code in messages:
        return messages[code]
    if status == 401:
        return "Не удалось авторизоваться в DroneBet. Сообщи администратору."
    if status == 429:
        return "DroneBet временно ограничил запросы. Попробуй чуть позже."
    if status >= 500 or status == 0:
        return "DroneBet временно не отвечает. Попробуй ещё раз."
    return "DroneBet отклонил операцию. Попробуй ещё раз."


async def _complete_dronebet_exchange(exchange: dict) -> tuple[bool, dict, int]:
    """Perform one exchange without applying either local side twice after a retry."""
    key = exchange["idempotency_key"]
    local_key = f"{key}:sirius"
    if exchange["direction"] == "coins_to_cookies":
        local = storage.partner_coin_transaction(
            DRONEBET_PARTNER, exchange["external_user_id"], "debit", exchange["coins"], local_key,
            f"Обмен {exchange['coins']} Сириус Коин(ов) на печеньки DroneBet",
        )
        if not local["ok"]:
            storage.fail_partner_exchange(DRONEBET_PARTNER, key)
            return False, {"error": "Недостаточно доступных Сириус Коинов." if local["code"] == "insufficient_coins" else "Не удалось списать Сириус Коины."}, 409
        status, remote, retryable = await _dronebet_request("POST", "/credit.php", {
            "sirius_uid": exchange["uid"], "amount": exchange["cookies"], "idempotency_key": key,
            "reason": f"Обмен {exchange['coins']} Сириус Коин(ов) в Sirius Plus",
        })
        if 200 <= status < 300 and remote.get("ok"):
            remote_balance = remote.get("balance") if isinstance(remote.get("balance"), int) else None
            if remote_balance is not None:
                storage.cache_partner_balance(DRONEBET_PARTNER, exchange["uid"], remote_balance)
            storage.complete_partner_exchange(DRONEBET_PARTNER, key, local["balance"], remote_balance)
            return True, {"coins": local["balance"], "cookies": remote_balance}, 200
        if not retryable and status < 500:
            storage.partner_coin_transaction(
                DRONEBET_PARTNER, exchange["external_user_id"], "credit", exchange["coins"], f"{key}:rollback",
                "Возврат после отклонённого обмена DroneBet",
            )
            storage.fail_partner_exchange(DRONEBET_PARTNER, key)
            return False, {"error": _dronebet_error_message(status, remote)}, status or 400
        return False, {"pending": True, "error": "Обмен ожидает подтверждения DroneBet. Нажми «Повторить» через несколько секунд."}, 503

    status, remote, retryable = await _dronebet_request("POST", "/debit.php", {
        "sirius_uid": exchange["uid"], "amount": exchange["cookies"], "idempotency_key": key,
        "reason": f"Обмен печенек DroneBet на {exchange['coins']} Сириус Коин(ов)",
    })
    if not (200 <= status < 300 and remote.get("ok")):
        if not retryable and status < 500:
            storage.fail_partner_exchange(DRONEBET_PARTNER, key)
        if retryable or status >= 500:
            return False, {"pending": True, "error": "Обмен ожидает подтверждения DroneBet. Нажми «Повторить» через несколько секунд."}, 503
        return False, {"error": _dronebet_error_message(status, remote)}, status or 400
    local = storage.partner_coin_transaction(
        DRONEBET_PARTNER, exchange["external_user_id"], "credit", exchange["coins"], local_key,
        f"Обмен печенек DroneBet на {exchange['coins']} Сириус Коин(ов)",
    )
    if not local["ok"]:
        return False, {"pending": True, "error": "Печеньки уже списаны. Обмен ожидает подтверждения, повтори попытку."}, 503
    remote_balance = remote.get("balance") if isinstance(remote.get("balance"), int) else None
    if remote_balance is not None:
        storage.cache_partner_balance(DRONEBET_PARTNER, exchange["uid"], remote_balance)
    storage.complete_partner_exchange(DRONEBET_PARTNER, key, local["balance"], remote_balance)
    return True, {"coins": local["balance"], "cookies": remote_balance}, 200


@app.get("/api/dronebet/link")
async def api_dronebet_link(request: Request):
    user_id = get_user_id(request)
    uid = _session_uid(user_id) if user_id else None
    if not uid:
        return _partner_json_error("unauthorized", 401)
    link = storage.get_partner_link_for_uid(DRONEBET_PARTNER, uid)
    return JSONResponse({
        "ok": True,
        "linked": bool(link),
        "dronebet_user_id": link["external_user_id"] if link else None,
    })


@app.get("/api/dronebet/summary")
async def api_dronebet_summary(request: Request):
    user_id = get_user_id(request)
    uid = _session_uid(user_id) if user_id else None
    if not uid:
        return _partner_json_error("unauthorized", 401)
    link = storage.get_partner_link_for_uid(DRONEBET_PARTNER, uid)
    cached_balance = storage.get_partner_balance_cache(DRONEBET_PARTNER, uid)
    response = {
        "ok": True, "configured": bool(app_config.DRONEBET_OUTBOUND_TOKEN), "linked": bool(link),
        "coins": storage.get_coins(uid), "cookies": cached_balance["balance"] if cached_balance else None,
        "cookies_updated_at": cached_balance["updated_at"] if cached_balance else None,
        "rate": app_config.DRONEBET_COOKIE_RATE,
    }
    status, remote, _ = await _dronebet_request(
        "GET", f"/balance.php?sirius_uid={urllib.parse.quote(uid, safe='')}"
    )
    if link:
        response["dronebet_user_id"] = link["external_user_id"]
    if 200 <= status < 300 and remote.get("ok"):
        remote_balance = remote.get("balance")
        if isinstance(remote_balance, int):
            storage.cache_partner_balance(DRONEBET_PARTNER, uid, remote_balance)
            response["cookies"] = remote_balance
            response["cookies_updated_at"] = int(time.time())
    else:
        response["remote_error"] = _dronebet_error_message(status, remote)
        response["remote_status"] = status
        response["remote_code"] = str(remote.get("code") or remote.get("error") or "")[:64]
        response["remote_unavailable"] = status == 0 or status >= 500
    return JSONResponse(response)


@app.post("/api/dronebet/claim-link")
async def api_dronebet_claim_link(request: Request):
    user_id = get_user_id(request)
    uid = _session_uid(user_id) if user_id else None
    if not uid:
        return _partner_json_error("unauthorized", 401)
    try:
        code = str((await request.json()).get("code") or "").strip()
    except ValueError:
        return JSONResponse({"ok": False, "error": "Неверный формат запроса."}, status_code=400)
    if len(code) < 8:
        return JSONResponse({"ok": False, "error": "Введи одноразовый код из DroneBet."}, status_code=400)
    status, remote, _ = await _dronebet_request("POST", "/links_claim.php", {"code": code, "sirius_uid": uid})
    if not (200 <= status < 300 and remote.get("ok")):
        log.warning("DroneBet link claim rejected: status=%s code=%s", status, remote.get("code") or remote.get("error") or "unknown")
        return JSONResponse({"ok": False, "error": _dronebet_error_message(status, remote)}, status_code=status or 503)
    external_user_id = str(remote.get("user_id") or "").strip()
    if not external_user_id:
        log.error("DroneBet link claim succeeded without user_id")
        return JSONResponse({"ok": False, "error": "DroneBet подтвердил код без идентификатора аккаунта. Попробуй ещё раз позже."}, status_code=502)
    linked, reason = storage.link_partner_account(DRONEBET_PARTNER, uid, external_user_id)
    if not linked:
        return JSONResponse({"ok": False, "error": _dronebet_error_message(409, {"code": reason})}, status_code=409)
    return JSONResponse({"ok": True, "linked": True})


@app.post("/api/dronebet/exchange")
async def api_dronebet_exchange(request: Request):
    user_id = get_user_id(request)
    uid = _session_uid(user_id) if user_id else None
    if not uid:
        return _partner_json_error("unauthorized", 401)
    link = storage.get_partner_link_for_uid(DRONEBET_PARTNER, uid)
    if not link:
        return JSONResponse({"ok": False, "error": "Сначала свяжи аккаунт с DroneBet."}, status_code=409)
    try:
        data = await request.json()
        coins = int(data.get("coins"))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "Укажи целое количество коинов."}, status_code=400)
    direction = str(data.get("direction") or "")
    if direction not in {"coins_to_cookies", "cookies_to_coins"} or not 1 <= coins <= DRONEBET_MAX_EXCHANGE_COINS:
        return JSONResponse({"ok": False, "error": "Можно обменять от 1 до 500 целых Сириус Коинов."}, status_code=400)
    exchange, state = storage.begin_partner_exchange(
        DRONEBET_PARTNER, uid, link["external_user_id"], direction, coins,
        coins * app_config.DRONEBET_COOKIE_RATE, str(data.get("idempotency_key") or uuid.uuid4()),
    )
    if not exchange:
        return JSONResponse({"ok": False, "error": "Не удалось подготовить обмен. Попробуй ещё раз."}, status_code=409)
    if exchange["status"] == "completed":
        return JSONResponse({"ok": True, "replayed": True, "coins": exchange["local_balance"], "cookies": exchange["remote_balance"]})
    ok, result, status = await _complete_dronebet_exchange(exchange)
    return JSONResponse({"ok": ok, "replayed": state != "created", **result}, status_code=status)


@app.post("/api/dronebet/link-code")
async def api_dronebet_link_code(request: Request):
    user_id = get_user_id(request)
    uid = _session_uid(user_id) if user_id else None
    if not uid:
        return _partner_json_error("unauthorized", 401)
    if storage.get_partner_link_for_uid(DRONEBET_PARTNER, uid):
        return _partner_json_error("already_linked", 409)
    code, expires_at = storage.create_partner_link_code(
        DRONEBET_PARTNER, uid, app_config.DRONEBET_LINK_CODE_TTL_SECONDS
    )
    return JSONResponse({"ok": True, "code": code, "expires_at": expires_at})


@app.get("/api/partner/dronebet/accounts/{external_user_id}/balance")
async def partner_dronebet_balance(external_user_id: str, request: Request):
    denied = _require_dronebet_partner(request)
    if denied:
        return denied
    link = storage.get_partner_link(DRONEBET_PARTNER, external_user_id)
    if not link:
        return _partner_json_error("account_not_linked", 404)
    return JSONResponse({"ok": True, "external_user_id": external_user_id, "coins": storage.get_coins(link["uid"])})


@app.post("/api/partner/dronebet/links/claim")
async def partner_dronebet_claim_link(request: Request):
    denied = _require_dronebet_partner(request)
    if denied:
        return denied
    try:
        data = await request.json()
    except ValueError:
        return _partner_json_error("invalid_json")
    ok, code, uid = storage.claim_partner_link_code(
        DRONEBET_PARTNER, str(data.get("code") or ""), str(data.get("external_user_id") or "")
    )
    if not ok:
        statuses = {
            "invalid_link_code": 400,
            "external_account_already_linked": 409,
            "sirius_account_already_linked": 409,
        }
        return _partner_json_error(code, statuses.get(code, 400))
    return JSONResponse({"ok": True, "status": "linked", "sirius_uid": uid})


async def _partner_dronebet_coin_operation(request: Request, direction: str):
    denied = _require_dronebet_partner(request)
    if denied:
        return denied
    try:
        data = await request.json()
        amount = int(data.get("amount"))
    except (ValueError, TypeError):
        return _partner_json_error("invalid_amount")
    result = storage.partner_coin_transaction(
        DRONEBET_PARTNER,
        str(data.get("external_user_id") or ""),
        direction,
        amount,
        str(data.get("idempotency_key") or ""),
        str(data.get("reason") or ""),
    )
    if not result["ok"]:
        statuses = {
            "account_not_linked": 404,
            "insufficient_coins": 409,
            "idempotency_key_conflict": 409,
            "invalid_direction": 400,
            "invalid_amount": 400,
            "invalid_idempotency_key": 400,
            "invalid_external_user_id": 400,
        }
        return _partner_json_error(result["code"], statuses.get(result["code"], 400), balance=result.get("balance"))
    if not result["replayed"]:
        recipient = storage.get_user_by_uid(result["uid"])
        if recipient:
            verb = "начислил" if direction == "credit" else "списал"
            await web_notify(recipient, f"DroneBet {verb} {amount} Сириус Коин(ов).")
    return JSONResponse({
        "ok": True,
        "external_user_id": str(data.get("external_user_id") or ""),
        "direction": direction,
        "amount": amount,
        "coins": result["balance"],
        "replayed": result["replayed"],
    })


@app.post("/api/partner/dronebet/coins/credit")
async def partner_dronebet_credit(request: Request):
    return await _partner_dronebet_coin_operation(request, "credit")


@app.post("/api/partner/dronebet/coins/debit")
async def partner_dronebet_debit(request: Request):
    return await _partner_dronebet_coin_operation(request, "debit")


@app.post("/api/app-bonus")
async def api_app_bonus(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    if "SiriusPlusAndroid/" not in request.headers.get("user-agent", ""):
        return JSONResponse({"ok": False, "error": "Бонус доступен только в приложении"}, status_code=403)
    uid = _session_uid(user_id)
    if not uid:
        return JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)
    storage.ensure_coins(uid)
    claimed = storage.claim_app_usage_bonus(uid)
    return JSONResponse({"ok": True, "claimed": claimed, "coins": storage.get_coins(uid)})


@app.get("/api/referral")
async def api_referral(request: Request):
    user_id = get_user_id(request)
    uid = _session_uid(user_id) if user_id else None
    if not uid:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    code = storage.get_or_create_referral_code(uid)
    return JSONResponse({
        "ok": True,
        "code": code,
        "url": f"https://{app_config.CANONICAL_HOST}/?ref={code}",
        "invited": storage.get_referral_count(uid),
        "reward": 5,
    })


@app.get("/api/referral/qr")
async def api_referral_qr(request: Request):
    user_id = get_user_id(request)
    uid = _session_uid(user_id) if user_id else None
    if not uid:
        return Response(status_code=401)
    code = storage.get_or_create_referral_code(uid)
    try:
        import qrcode
        import qrcode.image.svg

        image = qrcode.make(
            f"https://{app_config.CANONICAL_HOST}/?ref={code}",
            image_factory=qrcode.image.svg.SvgPathImage,
            border=1,
        )
        return Response(content=image.to_string(), media_type="image/svg+xml")
    except Exception as e:
        log.warning("Could not build referral QR code: %s", e)
        return Response(status_code=503)


@app.post("/api/coins/transfer")
async def api_coins_transfer(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    from_uid = _session_uid(user_id)
    if not from_uid:
        return JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)

    data = await request.json()
    raw_uid = str(data.get("to_uid") or "").strip()
    to_uid = raw_uid or str(data.get("recipient") or "").strip()
    try:
        amount = int(data.get("amount", 0))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "Некорректное количество"}, status_code=400)

    if not to_uid or to_uid == from_uid:
        return JSONResponse({"ok": False, "error": "Некорректный получатель"}, status_code=400)
    if amount < 1:
        return JSONResponse({"ok": False, "error": "Количество должно быть > 0"}, status_code=400)

    if not raw_uid:
        matches = storage.resolve_known_recipient(to_uid)
        if not matches:
            if not to_uid.isdigit():
                return JSONResponse({"ok": False, "error": "Получатель не найден. Введи точный UID или полное ФИО."}, status_code=404)
        elif len(matches) > 1:
            return JSONResponse({"ok": False, "error": "Нашлось несколько пользователей с таким ФИО. Введи UID."}, status_code=409)
        else:
            to_uid = matches[0]["uid"]
    if to_uid == from_uid:
        return JSONResponse({"ok": False, "error": "Нельзя перевести коины самому себе"}, status_code=400)

    available = storage.get_coins(from_uid)
    if available < amount:
        return JSONResponse({"ok": False, "error": f"Недостаточно коинов. Доступно: {available}"}, status_code=402)

    payload = _decode_jwt(storage.get_token(user_id) or "") or {}
    sender_name = " ".join(filter(None, [payload.get("lastName"), payload.get("firstName")]))
    if not sender_name:
        sender_name = from_uid[:8] + "..."

    # Get receiver info for notification
    receiver_name = to_uid[:8] + "..."
    receiver_user_id = storage.get_user_by_uid(to_uid)

    # Perform transfer
    storage.add_coins(from_uid, -amount)
    storage.add_coins(to_uid, amount)

    # Notify sender (always online)
    await web_notify(user_id, f"📤 Ты отправил {amount} Сириус Коин(ов) пользователю {receiver_name}")

    # Notify receiver (may be offline — stored in DB)
    if receiver_user_id:
        await web_notify(receiver_user_id, f"📥 {sender_name} отправил тебе {amount} Сириус Коин(ов)!")
        storage.add_notification(receiver_user_id, f"📥 {sender_name} отправил тебе {amount} Сириус Коин(ов)!")

    return JSONResponse({"ok": True, "new_balance": storage.get_coins(from_uid)})


@app.post("/api/promocodes/redeem")
async def api_redeem_promocode(request: Request):
    user_id = get_user_id(request)
    uid = _session_uid(user_id) if user_id else None
    if not uid:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    data = await request.json()
    ok, message, new_balance = storage.redeem_promo_code(data.get("code", ""), uid)
    if not ok:
        return JSONResponse({"ok": False, "error": message}, status_code=400)
    await web_notify(user_id, f"🎟 {message}", "success")
    return JSONResponse({"ok": True, "message": message, "new_balance": new_balance})


@app.get("/api/admin/users")
async def api_admin_users(request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied

    users = storage.get_all_known_uids()
    return JSONResponse({
        "ok": True,
        "users": [
            {
                "uid": u["uid"],
                "name": u["full_name"],
                "team": u["team"],
                "coins": u["coins"],
                "reserved_coins": u["reserved_coins"],
                "trust_level": 0 if u["is_admin"] else u["trust_level"],
                "is_admin": bool(u["is_admin"]),
                "is_banned": bool(u["is_banned"]),
                "ban_reason": u["ban_reason"],
            }
            for u in users
        ]
    })


@app.get("/api/admin/users/{target_uid}")
async def api_admin_user_profile(target_uid: str, request: Request):
    _, denied = _require_admin(request)
    if denied:
        return denied

    profile = storage.get_admin_user_profile(target_uid)
    if not profile:
        return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=404)

    priority_names = {"high": "Высокий", "medium": "Средний", "low": "Низкий"}
    watches = storage.get_watchlist(profile["user_id"])
    return JSONResponse({
        "ok": True,
        "user": {
            "uid": profile["uid"],
            "name": profile["full_name"],
            "team": profile["team"],
            "coins": profile["coins"],
            "reservedCoins": profile["reserved_coins"],
            "trustLevel": 0 if profile["is_admin"] else profile["trust_level"],
            "isAdmin": bool(profile["is_admin"]),
            "isBanned": bool(profile["is_banned"]),
            "banReason": profile["ban_reason"],
            "createdAt": profile["created_at"],
            "lastActive": profile["last_active"],
            "loginType": profile["login_type"],
            "watchingCount": profile["watching_count"],
            "registeredCount": profile["registered_count"],
            "reminderCount": profile["reminder_count"],
            "siriusRequestCount": storage.get_sirius_request_count(profile["user_id"]),
            "watches": [
                {
                    "eventId": row["event_id"],
                    "eventName": row["event_name"],
                    "eventStart": fmt_dt(row["event_start"]),
                    "priority": priority_names.get(str(row["snipe_priority"] or "high"), "Высокий"),
                    "isSniping": poller.is_sniping(profile["user_id"], row["event_id"]),
                }
                for row in watches
            ],
        },
    })


@app.post("/api/admin/users/{target_uid}/message")
async def api_admin_message_user(target_uid: str, request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied
    profile = storage.get_admin_user_profile(target_uid)
    if not profile:
        return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=404)
    data = await request.json()
    message = str(data.get("message", "")).strip()
    if not message:
        return JSONResponse({"ok": False, "error": "Сообщение не может быть пустым"}, status_code=400)
    if len(message) > 4000:
        return JSONResponse({"ok": False, "error": "Сообщение слишком длинное"}, status_code=400)

    admin_name = storage.get_known_name(admin_uid) or "Администратор"
    feedback_id = storage.create_admin_feedback(profile["user_id"], message, admin_name)
    await web_notify(profile["user_id"], f"💬 {admin_name} написал тебе в обращениях.")
    return JSONResponse({"ok": True, "feedbackId": feedback_id})


@app.get("/api/admin/promocodes")
async def api_admin_promocodes(request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied
    return JSONResponse({
        "ok": True,
        "promocodes": [dict(row) for row in storage.get_promo_codes()],
    })


@app.post("/api/admin/promocodes")
async def api_admin_create_promocode(request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied
    data = await request.json()
    try:
        amount = int(data.get("coin_amount", 0))
        max_uses = int(data.get("max_uses", 0))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "Коины и лимит должны быть числами"}, status_code=400)
    code = storage.create_promo_code(data.get("code", ""), amount, max_uses, admin_uid)
    if not code:
        return JSONResponse({"ok": False, "error": "Проверь код, лимит и количество коинов. Такой код уже существует."}, status_code=400)
    return JSONResponse({"ok": True, "code": code})


@app.post("/api/admin/grant-coins")
async def api_admin_grant_coins(request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied

    data = await request.json()
    target_uid = data.get("uid", "").strip()
    amount = data.get("amount", 0)
    if not target_uid:
        return JSONResponse({"ok": False, "error": "UID не указан"}, status_code=400)
    if not isinstance(amount, int) or amount <= 0:
        return JSONResponse({"ok": False, "error": "Количество должно быть положительным числом"}, status_code=400)

    new_balance = storage.add_coins(target_uid, amount)
    return JSONResponse({"ok": True, "new_balance": new_balance})


@app.post("/api/admin/set-trust")
async def api_admin_set_trust(request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied

    data = await request.json()
    target_uid = data.get("uid", "").strip()
    trust_level = int(data.get("trust_level", 2))
    if not target_uid:
        return JSONResponse({"ok": False, "error": "UID не указан"}, status_code=400)
    if storage.is_admin(target_uid):
        return JSONResponse({"ok": False, "error": "У админа уровень 0 и режим доверенного аккаунта"}, status_code=400)
    if trust_level not in (1, 2, 3):
        return JSONResponse({"ok": False, "error": "Уровень должен быть 1, 2 или 3"}, status_code=400)
    storage.set_trust_level(target_uid, trust_level)
    return JSONResponse({"ok": True, "trust_level": trust_level})


@app.post("/api/admin/set-admin")
async def api_admin_set_admin(request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied

    data = await request.json()
    target_uid = data.get("uid", "").strip()
    make_admin = bool(data.get("is_admin"))
    if not target_uid:
        return JSONResponse({"ok": False, "error": "UID не указан"}, status_code=400)
    if target_uid == admin_uid and not make_admin:
        return JSONResponse({"ok": False, "error": "Нельзя снять админку с самого себя"}, status_code=400)

    if make_admin:
        storage.add_admin(target_uid)
    else:
        storage.remove_admin(target_uid)
    return JSONResponse({"ok": True, "is_admin": make_admin})


@app.post("/api/admin/set-ban")
async def api_admin_set_ban(request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied
    data = await request.json()
    target_uid = str(data.get("uid", "")).strip()
    blocked = bool(data.get("blocked"))
    reason = str(data.get("reason", "")).strip()
    if not target_uid.isdigit() or not 6 <= len(target_uid) <= 32:
        return JSONResponse({"ok": False, "error": "Укажи корректный Sirius UID"}, status_code=400)
    if target_uid == admin_uid:
        return JSONResponse({"ok": False, "error": "Нельзя заблокировать самого себя"}, status_code=400)
    if storage.is_admin(target_uid):
        return JSONResponse({"ok": False, "error": "Администраторов блокировать нельзя"}, status_code=400)
    if blocked:
        if not reason:
            return JSONResponse({"ok": False, "error": "Укажи причину блокировки"}, status_code=400)
        if len(reason) > 1000:
            return JSONResponse({"ok": False, "error": "Причина слишком длинная"}, status_code=400)
        storage.ban_account(target_uid, reason, admin_uid)
        _cancel_banned_user_auto_registrations(target_uid)
    else:
        storage.unban_account(target_uid)
    return JSONResponse({"ok": True, "is_banned": blocked})


def _feedback_replies_view(message) -> list[dict]:
    replies = []
    if message["answer"]:
        legacy_name = storage.get_known_name(message["answered_by"]) or "Администратор"
        replies.append({
            "sender_type": "admin",
            "sender_name": legacy_name,
            "message": message["answer"],
            "created_at": message["answered_at"],
        })
    replies.extend({
        "sender_type": reply["sender_type"],
        "sender_name": reply["sender_name"],
        "message": reply["message"],
        "created_at": reply["created_at"],
    } for reply in storage.get_feedback_replies(message["id"]))
    return replies


@app.get("/api/admin/feedback")
async def api_admin_feedback(request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied

    messages = storage.get_feedback_messages()
    return JSONResponse({
        "ok": True,
        "messages": [
            {
                "id": m["id"],
                "user_id": m["user_id"],
                "name": m["full_name"],
                "message": m["message"],
                "initiated_by": m["initiated_by"],
                "initiator_name": m["initiator_name"],
                "answer": m["answer"],
                "answered_at": m["answered_at"],
                "replies": _feedback_replies_view(m),
                "is_read": bool(m["is_read"]),
                "created_at": m["created_at"],
            }
            for m in messages
        ],
    })


@app.post("/api/admin/feedback/{feedback_id}/read")
async def api_admin_feedback_read(feedback_id: int, request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied
    data = await request.json()
    storage.mark_feedback_read(feedback_id, bool(data.get("is_read", True)))
    return JSONResponse({"ok": True})


@app.post("/api/admin/feedback/{feedback_id}/answer")
async def api_admin_feedback_answer(feedback_id: int, request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied
    data = await request.json()
    answer = data.get("answer", "").strip()
    if not answer:
        return JSONResponse({"ok": False, "error": "Ответ не может быть пустым"}, status_code=400)
    admin_name = storage.get_known_name(admin_uid) or "Администратор"
    row = storage.add_feedback_reply(feedback_id, "admin", admin_name, admin_uid, answer)
    if not row:
        return JSONResponse({"ok": False, "error": "Обращение не найдено"}, status_code=404)
    target_user_id = row["user_id"]
    if target_user_id:
        text = f"💬 {admin_name}:\n{answer}"
        await web_notify(target_user_id, text)
        storage.add_notification(target_user_id, text)
    return JSONResponse({"ok": True})


@app.delete("/api/admin/feedback/{feedback_id}")
async def api_admin_feedback_delete(feedback_id: int, request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied
    storage.delete_feedback(feedback_id)
    return JSONResponse({"ok": True})


@app.post("/api/admin/feedback/{feedback_id}/hide")
async def api_admin_feedback_hide(feedback_id: int, request: Request):
    admin_uid, denied = _require_admin(request)
    if denied:
        return denied
    storage.hide_feedback_for_admin(feedback_id)
    return JSONResponse({"ok": True})


@app.get("/api/feedback/my")
async def api_my_feedback(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    messages = storage.get_user_feedback_messages(user_id)
    return JSONResponse({
        "ok": True,
        "messages": [
            {
                "id": m["id"],
                "message": m["message"],
                "initiated_by": m["initiated_by"],
                "initiator_name": m["initiator_name"],
                "answer": m["answer"],
                "answered_at": m["answered_at"],
                "created_at": m["created_at"],
                "replies": _feedback_replies_view(m),
            }
            for m in messages
        ],
    })


@app.post("/api/feedback/{feedback_id}/hide")
async def api_my_feedback_hide(feedback_id: int, request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    storage.hide_feedback_for_user(user_id, feedback_id)
    return JSONResponse({"ok": True})


@app.post("/api/feedback/{feedback_id}/reply")
async def api_my_feedback_reply(feedback_id: int, request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "Тело запроса должно быть JSON"}, status_code=400)
    message = str(data.get("message", "")).strip()
    if not message:
        return JSONResponse({"ok": False, "error": "Сообщение не может быть пустым"}, status_code=400)
    if len(message) > 4000:
        return JSONResponse({"ok": False, "error": "Сообщение слишком длинное"}, status_code=400)
    if not storage.get_user_feedback_message(user_id, feedback_id):
        return JSONResponse({"ok": False, "error": "Обращение не найдено"}, status_code=404)
    sender_uid = _session_uid(user_id) or ""
    sender_name = storage.get_known_name(sender_uid) or "Ты"
    storage.add_feedback_reply(feedback_id, "user", sender_name, user_id, message)
    return JSONResponse({"ok": True})


@app.post("/api/subscribe")
async def api_subscribe(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    token = storage.get_token(user_id)
    if not token:
        return JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)

    data = await request.json()
    event_id = data.get("event_id")
    if not event_id:
        return JSONResponse({"ok": False, "error": "event_id обязателен"}, status_code=400)

    if not _sirius_client:
        return JSONResponse({"ok": False, "error": "Sirius клиент не запущен"}, status_code=503)

    try:
        ev = await _find_event_for_user(user_id, token, event_id)
        storage.increment_sirius_request(user_id)
        result = await _sirius_client.subscribe(event_id, token=token)
        outcome, text = classify_subscribe_result(result)
        if outcome == "ok":
            final_reserved = result.reserved
            try:
                storage.increment_sirius_request(user_id)
                fresh_events = await _sirius_client.fetch_schedule(token=token)
                _set_events_cached(user_id, fresh_events)
                actual = next((e for e in fresh_events if e.event_id == event_id), None)
                if actual:
                    final_reserved = bool(actual.is_reserved)
                    if actual.is_recorded and not actual.is_reserved:
                        final_reserved = False
            except Exception as e:
                log.warning("Failed to verify subscribe status for %s: %s", event_id, e)
            # A direct registration replaces a pending auto-registration. Its
            # reserved coins were not used by the sniper and must be returned.
            watch = storage.take_active_watch(user_id, event_id)
            if watch:
                poller.cancel_snipe(user_id, event_id)
                uid = _session_uid(user_id) or ""
                if uid and watch["coin_cost"]:
                    storage.release_coins(uid, int(watch["coin_cost"]))
            _invalidate_events_cache(user_id)
            return JSONResponse({"ok": True, "reserved": final_reserved, "text": text, "auto_registration_cancelled": bool(watch)})
        else:
            return JSONResponse({"ok": False, "outcome": outcome, "text": text})
    except Exception as e:
        return JSONResponse({"ok": False, "error": _friendly_error(e)}, status_code=500)


@app.post("/api/unsubscribe")
async def api_unsubscribe(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    token = storage.get_token(user_id)
    if not token:
        return JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)

    data = await request.json()
    event_id = data.get("event_id")
    if not event_id:
        return JSONResponse({"ok": False, "error": "event_id обязателен"}, status_code=400)

    if not _sirius_client:
        return JSONResponse({"ok": False, "error": "Sirius клиент не запущен"}, status_code=503)

    try:
        ev = await _find_event_for_user(user_id, token, event_id)
        if not ev:
            return JSONResponse({"ok": False, "error": "Событие не найдено в актуальном расписании"}, status_code=404)
        if ev and _is_past_event(ev):
            return JSONResponse({"ok": False, "error": "Событие уже прошло"}, status_code=400)

        storage.increment_sirius_request(user_id)
        ok = await _sirius_client.unsubscribe(event_id, token=token)
        if ok:
            storage.remove_watch(user_id, event_id)
        return JSONResponse({"ok": ok})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/watch")
async def api_watch(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)

    data = await request.json()
    event_id = data.get("event_id")
    event_name = data.get("event_name", "???")
    event_start = data.get("event_start", "")
    snipe_priority = data.get("snipe_priority", "high")
    if snipe_priority not in ("high", "medium", "low"):
        snipe_priority = "high"
    coin_cost = storage.snipe_priority_cost(snipe_priority)
    if not event_id:
        return JSONResponse({"ok": False, "error": "event_id обязателен"}, status_code=400)

    if _is_past_event_start(event_start):
        return JSONResponse({"ok": False, "error": "Событие уже прошло"}, status_code=400)

    if str(event_id).startswith("community_"):
        try:
            community_id = int(str(event_id).split("_", 1)[1])
        except (ValueError, IndexError):
            return JSONResponse({"ok": False, "error": "Некорректное событие сообщества"}, status_code=400)
        community_event = storage.get_community_event(community_id)
        if not community_event:
            return JSONResponse({"ok": False, "error": "Событие сообщества не найдено"}, status_code=404)
        target = _community_datetime(community_event["registration_open_at"])
        event_starts_at = _community_datetime(
            f"{community_event['date_iso']}T{community_event['start_time']}" if community_event["start_time"] else ""
        )
        now = _now()
        if not target or target <= now:
            return JSONResponse({"ok": False, "error": "Регистрация для этого события уже не ожидается"}, status_code=400)
        if event_starts_at and event_starts_at <= now:
            return JSONResponse({"ok": False, "error": "Событие уже прошло"}, status_code=400)
        closes_at = _community_datetime(community_event["registration_close_at"])
        if closes_at and closes_at <= target:
            return JSONResponse({"ok": False, "error": "Регистрация закончится до открытия"}, status_code=400)

        uid = _session_uid(user_id) or ""
        if uid and not storage.reserve_coins(uid, coin_cost):
            return JSONResponse({"ok": False, "error": f"Недостаточно Сириус Коинов. Нужно: {coin_cost}."}, status_code=402)
        event_name = community_event["event_name"]
        event_start = f"{community_event['date_iso']}T{community_event['start_time']}:00+03:00" if community_event["start_time"] else ""
        storage.add_watch(user_id, event_id, event_name, event_start=event_start, snipe_priority=snipe_priority)
        poller.schedule_community_snipe(
            user_id, event_id, event_name, target, uid, web_notify, coin_cost
        )
        return JSONResponse({"ok": True, "community": True})

    token = storage.get_token(user_id)
    ev = None
    if _sirius_client and token:
        try:
            ev = await _find_event_for_user(user_id, token, event_id)
            if not ev:
                return JSONResponse({"ok": False, "error": "Событие не найдено в актуальном расписании"}, status_code=404)
            if ev and _is_past_event(ev):
                return JSONResponse({"ok": False, "error": "Событие уже прошло"}, status_code=400)
        except Exception as e:
            log.warning("Failed to fetch schedule for watch validation: %s", e)
            return JSONResponse({"ok": False, "error": "Не удалось проверить актуальность события"}, status_code=503)

        uid = storage.get_user_uid(user_id) or ""
        if uid and not storage.reserve_coins(uid, coin_cost):
            return JSONResponse({"ok": False, "error": f"Недостаточно Сириус Коинов. Нужно: {coin_cost}. Получи их на странице «Как получить Сириус Коины»."}, status_code=402)

    storage.add_watch(user_id, event_id, event_name, event_start=event_start, snipe_priority=snipe_priority)

    if _sirius_client and token:
        try:
            if ev is None:
                ev = await _find_event_for_user(user_id, token, event_id)
        except Exception as e:
            log.warning("Failed to fetch schedule for watch: %s", e)
        if ev:
            poller.on_watch_added(user_id, token, event_id, ev, _sirius_client, web_notify, snipe_priority, coin_cost)

    return JSONResponse({"ok": True})


@app.post("/api/watch/priority")
async def api_watch_priority(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)

    data = await request.json()
    event_id = data.get("event_id")
    snipe_priority = data.get("snipe_priority", "high")
    if not event_id:
        return JSONResponse({"ok": False, "error": "event_id обязателен"}, status_code=400)
    if snipe_priority not in ("high", "medium", "low"):
        return JSONResponse({"ok": False, "error": "Неверный приоритет"}, status_code=400)

    watch = storage.get_watch(user_id, event_id)
    if not watch or watch["status"] != "watching":
        return JSONResponse({"ok": False, "error": "Автозапись не найдена"}, status_code=404)

    old_cost = watch["coin_cost"] if "coin_cost" in watch.keys() else storage.snipe_priority_cost(watch["snipe_priority"])
    new_cost = storage.snipe_priority_cost(snipe_priority)
    uid = _session_uid(user_id) or ""
    token = storage.get_token(user_id)

    if uid and new_cost > old_cost:
        if not storage.reserve_coins(uid, new_cost - old_cost):
            return JSONResponse({"ok": False, "error": f"Не хватает Sirius Coins. Нужно добавить: {new_cost - old_cost}."}, status_code=402)
    elif uid and old_cost > new_cost:
        storage.release_coins(uid, old_cost - new_cost)

    storage.update_watch_priority(user_id, event_id, snipe_priority, new_cost)

    if str(event_id).startswith("community_"):
        try:
            community_event = storage.get_community_event(int(str(event_id).split("_", 1)[1]))
            target = _community_datetime(community_event["registration_open_at"]) if community_event else None
            if community_event and target:
                poller.cancel_snipe(user_id, event_id)
                poller.schedule_community_snipe(
                    user_id, event_id, community_event["event_name"], target, uid, web_notify, new_cost
                )
        except (ValueError, IndexError):
            pass
        return JSONResponse({"ok": True, "coin_cost": new_cost})

    if token and _sirius_client:
        poller.cancel_snipe(user_id, event_id)
        ev = None
        try:
            cached = _get_cached(f"events:{user_id}") or _get_cached_any(f"events:{user_id}") or _get_persistent_events_cache(user_id) or []
            ev = next((e for e in cached if e.event_id == event_id), None)
            if ev is None:
                storage.increment_sirius_request(user_id)
                fresh = await _sirius_client.fetch_schedule(token=token)
                _set_events_cached(user_id, fresh)
                ev = next((e for e in fresh if e.event_id == event_id), None)
        except Exception as e:
            log.warning("Failed to reschedule snipe after priority change: %s", e)
        if ev:
            poller.on_watch_added(user_id, token, event_id, ev, _sirius_client, web_notify, snipe_priority, new_cost)

    return JSONResponse({"ok": True, "coin_cost": new_cost})


@app.post("/api/unwatch")
async def api_unwatch(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)

    data = await request.json()
    event_id = data.get("event_id")
    poller.cancel_snipe(user_id, event_id)
    watch = storage.get_watch(user_id, event_id)
    coin_cost = watch["coin_cost"] if watch and "coin_cost" in watch.keys() else 1
    storage.remove_watch(user_id, event_id)

    uid = _session_uid(user_id)
    if uid:
        storage.release_coins(uid, coin_cost)

    return JSONResponse({"ok": True})


@app.post("/api/sync")
async def api_sync(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    token = storage.get_token(user_id)
    if not token:
        return JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)
    if not _sirius_client:
        return JSONResponse({"ok": False, "error": "Sirius клиент не запущен"}, status_code=503)

    try:
        storage.increment_sirius_request(user_id)
        events = await _sirius_client.fetch_schedule(token=token)
    except Exception as e:
        return JSONResponse({"ok": False, "error": _friendly_error(e)}, status_code=500)

    _set_events_cached(user_id, events)

    registered_ids = {ev.event_id for ev in events if ev.is_recorded or ev.is_reserved}
    watches = {w["event_id"]: w for w in storage.get_all_watchlist(user_id)}

    removed = 0
    for wid, w in watches.items():
        if w["status"] == "registered" and wid not in registered_ids:
            storage.remove_watch(user_id, wid)
            removed += 1

    return JSONResponse({"ok": True, "total": len(events), "registered": len(registered_ids), "removed": removed})


@app.post("/api/ping")
async def api_ping(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    token = storage.get_token(user_id)
    if not token:
        return JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)
    if not _sirius_client:
        return JSONResponse({"ok": False, "error": "Sirius клиент не запущен"}, status_code=503)

    import time as time_module
    t0 = time_module.time()
    try:
        storage.increment_sirius_request(user_id)
        events = await _sirius_client.fetch_schedule(token=token)
    except Exception as e:
        return JSONResponse({"ok": False, "error": _friendly_error(e)}, status_code=500)
    _set_events_cached(user_id, events)
    elapsed_ms = int((time_module.time() - t0) * 1000)

    open_events = sum(1 for ev in events if ev.is_available)
    total = len(events)

    exp = token_expiry(token)
    remaining = None
    if exp:
        remaining = int((exp - _now()).total_seconds())

    skew_s = int(_sirius_client.clock_skew.total_seconds()) if _sirius_client else 0

    return JSONResponse({
        "ok": True, "response_time_ms": elapsed_ms, "total_events": total,
        "open_events": open_events, "token_remaining_seconds": remaining,
        "clock_skew_s": skew_s,
    })


@app.get("/api/events")
async def api_events(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    token = storage.get_token(user_id)
    if not token:
        return JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)

    if not _sirius_client:
        return JSONResponse({"ok": False, "error": "Sirius клиент не запущен"}, status_code=503)

    try:
        storage.increment_sirius_request(user_id)
        events = await _sirius_client.fetch_schedule(token=token)
        _set_events_cached(user_id, events)
        watches = {w["event_id"] for w in storage.get_watchlist(user_id)}

        # Compute overlaps for the detail modal
        user_ids = {ev.event_id for ev in events if ev.is_recorded or ev.is_reserved or ev.event_id in watches}
        user_ranges = []
        for ev in events:
            if ev.event_id in user_ids:
                s = parse_sirius_time(ev.event_start)
                e_raw = ev.raw.get("eventEnd")
                e = parse_sirius_time(e_raw) if e_raw else None
                if s and e:
                    user_ranges.append((ev.event_id, s, e))
        _user_event_names = {ev.event_id: ev.event_name for ev in events}
        conflict_ids = set()
        conflict_with_map: dict[str, list[str]] = {}
        for ev in events:
            if ev.event_id in user_ids:
                continue
            s = parse_sirius_time(ev.event_start)
            e_raw = ev.raw.get("eventEnd")
            e = parse_sirius_time(e_raw) if e_raw else None
            if not s or not e:
                continue
            for uid, us, ue in user_ranges:
                if s < ue and us < e:
                    conflict_ids.add(ev.event_id)
                    conflict_with_map.setdefault(ev.event_id, []).append(_user_event_names.get(uid, uid))

        result = []
        for ev in events:
            result.append({
                "event_id": ev.event_id,
                "event_name": ev.event_name,
                "day_iso": ev.day_iso,
                "event_start": ev.event_start,
                "event_end": ev.raw.get("eventEnd"),
                "record_start": ev.record_start,
                "is_available": ev.is_available,
                "reasons": ev.reasons,
                "will_open_at": ev.will_open_at,
                "is_recorded": ev.is_recorded,
                "is_reserved": ev.is_reserved,
                "people_current": ev.people_current,
                "people_max": ev.people_max,
                "description": ev.description,
                "conflict": ev.event_id in conflict_ids,
                "conflict_with": conflict_with_map.get(ev.event_id, []),
                "watched": ev.event_id in watches,
            })
        return JSONResponse({"ok": True, "events": result, "now": _now().isoformat()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/watchlist")
async def api_watchlist(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)

    watches = storage.get_all_watchlist(user_id)
    result = []
    for w in watches:
        result.append({
            "id": w["id"],
            "event_id": w["event_id"],
            "event_name": w["event_name"],
            "status": w["status"],
            "event_start": w["event_start"],
            "snipe_active": poller.is_sniping(user_id, w["event_id"]),
        })
    return JSONResponse({"ok": True, "watches": result})


@app.get("/api/snipe-log/{event_id}")
async def api_snipe_log(request: Request, event_id: str):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    rows = storage.get_snipe_attempts(user_id, event_id)
    return JSONResponse({
        "ok": True,
        "attempts": [
            {
                "phase": r["phase"],
                "status_code": r["status_code"],
                "success": bool(r["success"]),
                "reserved": bool(r["reserved"]),
                "message": r["message"],
                "latency_ms": r["latency_ms"],
                "created_at": fmt_ts(r["created_at"]),
            }
            for r in rows
        ],
    })


@app.get("/api/push/public-key")
async def api_push_public_key(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    try:
        return JSONResponse({"ok": True, "public_key": _get_vapid_public_key()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Web Push недоступен: {e}"}, status_code=500)


@app.post("/api/push/subscribe")
async def api_push_subscribe(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)

    data = await request.json()
    endpoint = data.get("endpoint", "")
    keys = data.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return JSONResponse({"ok": False, "error": "Некорректная push-подписка"}, status_code=400)

    storage.save_push_subscription(user_id, json.dumps(data, ensure_ascii=False), endpoint)
    return JSONResponse({"ok": True})


@app.post("/api/push/unsubscribe")
async def api_push_unsubscribe(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)

    data = await request.json()
    endpoint = data.get("endpoint", "")
    if endpoint:
        storage.delete_push_subscription(endpoint)
    return JSONResponse({"ok": True})


@app.post("/api/push/test")
async def api_push_test(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)

    text = "🔔 Тестовый push\nЕсли телефон заблокирован или вкладка закрыта, это должно прийти системным уведомлением."
    result = await _send_push_to_user(
        user_id,
        text,
        "reminder",
    )
    mobile_result = await _send_mobile_push_to_user(user_id, text, "reminder")
    return JSONResponse({"ok": True, **result, "mobile": mobile_result})


@app.post("/api/mobile/push-token")
async def api_mobile_push_token(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    if "SiriusPlusAndroid/" not in request.headers.get("user-agent", ""):
        return JSONResponse({"ok": False, "error": "Требуется приложение Sirius Plus"}, status_code=403)
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "Некорректный запрос"}, status_code=400)
    token = str(data.get("token", "")).strip()
    if len(token) < 20 or len(token) > 4096 or any(char.isspace() for char in token):
        return JSONResponse({"ok": False, "error": "Некорректный FCM-токен"}, status_code=400)
    storage.save_mobile_push_device(user_id, token)
    return JSONResponse({"ok": True, "configured": _fcm_is_configured()})


@app.post("/api/notifications")
async def api_notifications(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)

    msgs = _notification_queue.pop(user_id, [])
    return JSONResponse({"ok": True, "notifications": msgs})


@app.get("/api/notifications/history")
async def api_notifications_history(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    history = storage.get_notifications(user_id)
    return JSONResponse({"ok": True, "notifications": [{
        "id": n["id"],
        "text": n["text"],
        "type": n["type"],
        "created_at": n["created_at"],
    } for n in history]})


@app.post("/api/notifications/clear")
async def api_notifications_clear(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    storage.clear_notifications(user_id)
    return JSONResponse({"ok": True})


@app.post("/api/notifications/delete/{notif_id}")
async def api_notifications_delete(notif_id: int, request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    storage.delete_notification(user_id, notif_id)
    return JSONResponse({"ok": True})


@app.post("/api/logout")
async def api_logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        with storage.get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
    response = JSONResponse({"ok": True})
    response.set_cookie(key="session_id", value="", max_age=0)
    return response


@app.get("/api/token-status")
async def api_token_status(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False}, status_code=401)

    token = storage.get_token(user_id)
    if not token:
        return JSONResponse({"ok": True, "has_token": False})

    exp = token_expiry(token)
    if exp:
        remaining = (exp - _now()).total_seconds()
        return JSONResponse({"ok": True, "has_token": True,
                             "expires": exp.isoformat(),
                             "remaining_seconds": int(remaining)})
    return JSONResponse({"ok": True, "has_token": True})


@app.get("/api/schedule")
async def api_schedule(request: Request, date: str = ""):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    token = storage.get_token(user_id)
    if not token:
        return JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)

    if not date:
        date = _now().astimezone(_MSK).strftime("%Y-%m-%d")

    if not _sirius_client:
        return JSONResponse({"ok": False, "error": "Sirius клиент не запущен"}, status_code=503)

    try:
        storage.increment_sirius_request(user_id)
        events = await _sirius_client.fetch_schedule_day(date, token=token)
        custom_evs = storage.get_custom_events(user_id, date)

        def _to_iso_api(d: str, t: str) -> str:
            if not t:
                return ""
            t = t.strip()
            if "T" in t or "+" in t:
                return t
            parts = t.split(":")
            if len(parts) == 2:
                return f"{d}T{t}:00+03:00"
            return f"{d}T{t}+03:00"

        result = []
        for ev in events:
            ti = None
            if ev.transport_info:
                ti = {"kind": ev.transport_info.get("kind", ""), "number": ev.transport_info.get("number", "")}
            result.append({
                "event_id": ev.event_id,
                "event_name": ev.event_name,
                "event_type": ev.event_type,
                "start_time": ev.start_time,
                "end_time": ev.end_time,
                "status": ev.status,
                "location": ev.location,
                "tutors": ev.tutors,
                "description": ev.description,
                "people_current": ev.people_current,
                "people_max": ev.people_max,
                "unions": ev.unions,
                "departure_location": ev.departure_location,
                "arrival_location": ev.arrival_location,
                "transport_info": ti,
            })
        for ce in custom_evs:
            st = _to_iso_api(date, ce["start_time"])
            et = _to_iso_api(date, ce["end_time"])
            result.append({
                "event_id": f"custom_{ce['id']}",
                "event_name": ce["event_name"],
                "event_type": "customEvent",
                "start_time": st,
                "end_time": et,
                "status": "",
                "location": [ce["location"]] if ce["location"] else [],
                "tutors": [],
                "description": ce["description"],
                "people_current": 0,
                "people_max": 0,
                "unions": [],
                "departure_location": "",
                "arrival_location": "",
                "transport_info": None,
            })
        session_uid = _session_uid(user_id) or ""
        for community_event in storage.get_community_events_for_date(user_id, date):
            if community_event["is_registered"]:
                result.append(_community_event_payload(
                    community_event, user_id, storage.is_admin(session_uid)
                ))
        result.sort(key=lambda e: parse_sirius_time(e["start_time"]) or dt.datetime.max.replace(tzinfo=dt.timezone.utc))
        return JSONResponse({"ok": True, "events": result, "date": date})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/user-info")
async def api_user_info(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False}, status_code=401)

    token = storage.get_token(user_id)
    if not token:
        return JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)

    payload = _decode_jwt(token)
    if not payload:
        return JSONResponse({"ok": False, "error": "Не удалось декодировать токен"}, status_code=400)
    payload["id"] = storage.get_user_uid(user_id) or ""

    exp_ts = payload.get("exp")
    iat_ts = payload.get("iat")
    remaining = max(0, exp_ts - time.time()) if exp_ts else 0

    watches = storage.get_all_watchlist(user_id)
    watched_count = sum(1 for w in watches if w["status"] == "watching")
    registered_count = sum(1 for w in watches if w["status"] == "registered")
    uid = payload.get("id", "")
    trust_level = storage.get_trust_level(uid)

    return JSONResponse({
        "ok": True,
        "email": payload.get("email"),
        "firstName": payload.get("firstName"),
        "lastName": payload.get("lastName"),
        "middleName": payload.get("middleName"),
        "id": uid,
        "team": storage.get_known_team(uid),
        "trustLevel": trust_level,
        "expiresAt": dt.datetime.fromtimestamp(exp_ts, tz=dt.timezone.utc).isoformat() if exp_ts else None,
        "issuedAt": dt.datetime.fromtimestamp(iat_ts, tz=dt.timezone.utc).isoformat() if iat_ts else None,
        "remainingSeconds": int(remaining),
        "watchedCount": watched_count,
        "registeredCount": registered_count,
        "siriusRequestCount": storage.get_sirius_request_count(user_id),
        "loginType": storage.get_login_type(user_id),
    })


# ---------- Custom events API ----------

@app.post("/api/custom-events")
async def api_add_custom_event(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    data = await request.json()
    event_id = storage.add_custom_event(
        user_id=user_id,
        event_name=data.get("event_name", "").strip(),
        date_iso=data.get("date_iso", "").strip(),
        start_time=data.get("start_time", "").strip(),
        end_time=data.get("end_time", "").strip(),
        description=data.get("description", "").strip(),
        location=data.get("location", "").strip(),
        repeat_daily=bool(data.get("repeat_daily")),
    )
    return JSONResponse({"ok": True, "id": event_id})


@app.delete("/api/custom-events/{event_id}")
async def api_delete_custom_event(event_id: int, request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    for watch in storage.remove_watches_for_event(f"custom_{event_id}"):
        poller.cancel_snipe(watch["user_id"], watch["event_id"])
        watched_uid = storage.get_user_uid(watch["user_id"])
        if watched_uid and watch["coin_cost"]:
            storage.release_coins(watched_uid, watch["coin_cost"])
    storage.remove_custom_event(user_id, event_id)
    return JSONResponse({"ok": True})


COMMUNITY_EVENT_PUBLICATION_COST = 5


def _community_event_input(data: dict) -> tuple[dict | None, str | None]:
    event_name = str(data.get("event_name") or "").strip()
    date_iso = str(data.get("date_iso") or "").strip()
    start_time = str(data.get("start_time") or "").strip()
    end_time = str(data.get("end_time") or "").strip()
    registration_open_at = str(data.get("registration_open_at") or "").strip()
    registration_close_at = str(data.get("registration_close_at") or "").strip()
    description = str(data.get("description") or "").strip()
    location = str(data.get("location") or "").strip()
    contact = str(data.get("contact") or "").strip()
    try:
        people_max = int(data.get("people_max") or 0)
        dt.date.fromisoformat(date_iso)
    except (ValueError, TypeError):
        return None, "Укажи корректную дату и лимит участников"
    if not event_name or len(event_name) > 180:
        return None, "Название обязательно и должно быть не длиннее 180 символов"
    if not start_time:
        return None, "Укажи время начала события"
    if people_max < 1 or people_max > 1000:
        return None, "Лимит участников должен быть от 1 до 1000"
    if len(contact) > 200:
        return None, "Контакт организатора должен быть не длиннее 200 символов"
    open_at = _community_datetime(registration_open_at)
    close_at = _community_datetime(registration_close_at)
    starts_at = _community_datetime(f"{date_iso}T{start_time}")
    if registration_open_at and not open_at:
        return None, "Некорректное время открытия регистрации"
    if registration_close_at and not close_at:
        return None, "Некорректное время закрытия регистрации"
    if open_at and close_at and open_at >= close_at:
        return None, "Открытие регистрации должно быть раньше закрытия"
    if close_at and starts_at and close_at > starts_at:
        return None, "Регистрация должна закрыться до начала события"
    return {
        "event_name": event_name,
        "date_iso": date_iso,
        "start_time": start_time,
        "end_time": end_time,
        "registration_open_at": registration_open_at,
        "registration_close_at": registration_close_at,
        "people_max": people_max,
        "description": description,
        "location": location,
        "contact": contact,
    }, None


def _community_coorganizer_input(data: dict, owner_user_id: str) -> tuple[list[tuple[str, str, str]] | None, str | None]:
    raw_values = data.get("coorganizers") or []
    if isinstance(raw_values, str):
        raw_values = raw_values.replace("\n", ",").split(",")
    if not isinstance(raw_values, list):
        return None, "Соорганизаторов нужно указывать списком"
    resolved: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        value = str(raw_value or "").strip()
        if not value:
            continue
        matches = storage.resolve_known_recipient(value)
        if not matches:
            key = f"text:{value.casefold()}"
            if key not in seen:
                resolved.append((key, "", value))
                seen.add(key)
            continue
        if len(matches) > 1:
            return None, f"Для «{value}» найдено несколько людей. Укажи UID"
        user_id = matches[0]["user_id"]
        uid = matches[0]["uid"]
        if user_id != owner_user_id and user_id not in seen:
            resolved.append((user_id, uid, matches[0]["full_name"] or value))
            seen.add(user_id)
    return resolved, None


def _can_manage_community_event(event_id: int, user_id: str) -> bool:
    uid = _session_uid(user_id) or ""
    return storage.is_admin(uid) or storage.can_manage_community_event(event_id, user_id)


def _community_event_change_lines(before, after: dict) -> list[str]:
    lines: list[str] = []
    old_time = f"{before['date_iso']} {before['start_time']}"
    new_time = f"{after['date_iso']} {after['start_time']}"
    if old_time != new_time:
        lines.append(f"время: {old_time} → {new_time}")
    if before["location"] != after["location"]:
        lines.append(f"место: {before['location'] or 'не указано'} → {after['location'] or 'не указано'}")
    if before["people_max"] != after["people_max"]:
        lines.append(f"мест: {before['people_max']} → {after['people_max']}")
    if before["contact"] != after["contact"]:
        lines.append("обновлён контакт организатора")
    if before["description"] != after["description"]:
        lines.append("обновлено описание")
    return lines


async def _notify_community_participants(event_id: int, text: str, exclude_user_id: str = "") -> None:
    for participant in storage.get_community_event_participants(event_id):
        if participant["user_id"] != exclude_user_id:
            await web_notify(participant["user_id"], text)


@app.get("/api/community-events/{event_id}")
async def api_community_event(event_id: int, request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    event = storage.get_community_event(event_id)
    if not event:
        return JSONResponse({"ok": False, "error": "Событие не найдено"}, status_code=404)
    uid = _session_uid(user_id) or ""
    payload = _community_event_payload(event, user_id, storage.is_admin(uid))
    payload["coorganizers"] = [
        {"uid": row["uid"], "name": row["full_name"] or row["display_name"] or row["uid"]}
        for row in storage.get_community_coorganizers(event_id)
    ]
    return JSONResponse({"ok": True, "event": payload})


@app.post("/api/community-events")
async def api_add_community_event(request: Request):
    user_id = get_user_id(request)
    uid = _session_uid(user_id) if user_id else None
    if not user_id or not uid:
        return JSONResponse({"ok": False, "error": "Войди через Sirius, чтобы создать событие"}, status_code=401)
    data = await request.json()
    event_data, error = _community_event_input(data)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    coorganizers, error = _community_coorganizer_input(data, user_id)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    if storage.get_coins(uid) < COMMUNITY_EVENT_PUBLICATION_COST:
        return JSONResponse({"ok": False, "error": f"Нужно {COMMUNITY_EVENT_PUBLICATION_COST} Сириус Коинов для публикации"}, status_code=402)
    storage.add_coins(uid, -COMMUNITY_EVENT_PUBLICATION_COST)
    event_id = storage.add_community_event(user_id, uid, coorganizers=coorganizers or [], **event_data)
    return JSONResponse({"ok": True, "id": event_id, "new_balance": storage.get_coins(uid)})


@app.patch("/api/community-events/{event_id}")
async def api_update_community_event(event_id: int, request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    if not _can_manage_community_event(event_id, user_id):
        return JSONResponse({"ok": False, "error": "Нет прав на изменение события"}, status_code=403)
    data = await request.json()
    event_data, error = _community_event_input(data)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    event = storage.get_community_event(event_id)
    if not event:
        return JSONResponse({"ok": False, "error": "Событие не найдено"}, status_code=404)
    coorganizers, error = _community_coorganizer_input(data, event["owner_user_id"])
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    changes = _community_event_change_lines(event, event_data)
    storage.update_community_event(event_id, coorganizers=coorganizers or [], **event_data)
    if changes:
        await _notify_community_participants(
            event_id,
            f"✏️ Организатор изменил событие «{event['event_name']}»:\n" + "\n".join(changes),
            user_id,
        )
    return JSONResponse({"ok": True})


@app.delete("/api/community-events/{event_id}")
async def api_delete_community_event(event_id: int, request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    if not _can_manage_community_event(event_id, user_id):
        return JSONResponse({"ok": False, "error": "Нет прав на удаление события"}, status_code=403)
    event = storage.get_community_event(event_id)
    if not event:
        return JSONResponse({"ok": False, "error": "Событие не найдено"}, status_code=404)
    await _notify_community_participants(
        event_id,
        f"🗑 Событие сообщества «{event['event_name']}» отменено организатором.",
        user_id,
    )
    for watch in storage.remove_watches_for_event(f"community_{event_id}"):
        poller.cancel_snipe(watch["user_id"], watch["event_id"])
        watched_uid = storage.get_user_uid(watch["user_id"])
        if watched_uid and watch["coin_cost"]:
            storage.release_coins(watched_uid, watch["coin_cost"])
    storage.delete_community_event(event_id)
    return JSONResponse({"ok": True})


@app.get("/api/community-events/{event_id}/participants")
async def api_community_event_participants(event_id: int, request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    if not _can_manage_community_event(event_id, user_id):
        return JSONResponse({"ok": False, "error": "Нет прав на просмотр участников"}, status_code=403)
    event = storage.get_community_event(event_id)
    if not event:
        return JSONResponse({"ok": False, "error": "Событие не найдено"}, status_code=404)
    return JSONResponse({
        "ok": True,
        "event_name": event["event_name"],
        "participants": [
            {"name": row["full_name"] or row["uid"] or "Пользователь", "uid": row["uid"]}
            for row in storage.get_community_event_participants(event_id)
        ],
    })


@app.post("/api/community-events/{event_id}/register")
async def api_register_community_event(event_id: int, request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    event = storage.get_community_event(event_id)
    if not event:
        return JSONResponse({"ok": False, "error": "Событие не найдено"}, status_code=404)
    if not _community_registration_open(event):
        return JSONResponse({"ok": False, "error": "Регистрация сейчас закрыта"}, status_code=400)
    ok, reason = storage.add_community_registration(event_id, user_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "Места закончились" if reason == "full" else "Событие не найдено"}, status_code=400)
    watch = storage.take_active_watch(user_id, f"community_{event_id}")
    if watch:
        poller.cancel_snipe(user_id, watch["event_id"])
        uid = _session_uid(user_id) or ""
        if uid and watch["coin_cost"]:
            storage.release_coins(uid, int(watch["coin_cost"]))
    await web_notify(user_id, f"✅ Ты теперь записан на «{event['event_name']}».")
    return JSONResponse({"ok": True, "alreadyRegistered": reason == "already_registered", "auto_registration_cancelled": bool(watch)})


@app.delete("/api/community-events/{event_id}/register")
async def api_unregister_community_event(event_id: int, request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    event = storage.get_community_event(event_id)
    if not event:
        return JSONResponse({"ok": False, "error": "Событие не найдено"}, status_code=404)
    starts_at = _community_datetime(f"{event['date_iso']}T{event['start_time']}")
    if starts_at and _now() >= starts_at:
        return JSONResponse({"ok": False, "error": "Событие уже прошло или началось"}, status_code=400)
    storage.remove_community_registration(event_id, user_id)
    return JSONResponse({"ok": True})


# ---------- Reminders API ----------

@app.get("/api/reminders")
async def api_get_reminders(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    reminders = storage.get_reminders_for_user(user_id)
    return JSONResponse({
        "ok": True,
        "reminders": [{
            "id": r["id"],
            "event_id": r["event_id"],
            "event_name": r["event_name"],
            "event_start": r["event_start"],
            "minutes_before": r["minutes_before"],
        } for r in reminders],
    })


@app.post("/api/reminders")
async def api_add_reminder(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    data = await request.json()
    storage.add_reminder(
        user_id=user_id,
        event_id=data.get("event_id", ""),
        event_name=data.get("event_name", ""),
        event_start=data.get("event_start", ""),
        minutes_before=int(data.get("minutes_before", 15)),
    )
    return JSONResponse({"ok": True})


@app.delete("/api/reminders/{reminder_id}")
async def api_delete_reminder(reminder_id: int, request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    with storage.get_conn() as conn:
        conn.execute("DELETE FROM schedule_reminders WHERE id=? AND user_id=?", (reminder_id, user_id))
    return JSONResponse({"ok": True})


@app.post("/api/feedback")
async def api_feedback(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    data = await request.json()
    msg = data.get("message", "").strip()
    if not msg:
        return JSONResponse({"ok": False, "error": "Сообщение не может быть пустым"}, status_code=400)
    storage.add_feedback(user_id, msg)
    log.info("FEEDBACK from %s: %s", user_id or "anonymous", msg)
    return JSONResponse({"ok": True})


# ---------- Start ----------

if __name__ == "__main__":
    uvicorn.run("main:app", host=app_config.HOST, port=app_config.PORT, reload=False)
