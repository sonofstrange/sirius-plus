from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import math
import os
import time
import urllib.parse
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
import dronebet
import poller
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

CACHE_TTL = 600
_schedule_cache: dict[str, tuple[float, list]] = {}

PAGE_SIZE = 20

_MSK = dt.timezone(dt.timedelta(hours=3))

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
    if text.startswith("🔔"):
        try:
            asyncio.create_task(_send_push_to_user(user_id, text, ntype))
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
        "url": "/events?tab=notifications",
        "is_alarm": text.startswith("🔔"),
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
    dronebet_task = asyncio.create_task(dronebet.run_dronebet_monitor(web_notify))

    app.state.ready = True
    try:
        yield
    finally:
        # Nginx turns this 503 into the shared maintenance page immediately.
        app.state.ready = False
        for t in [poller_task, reminder_task, dronebet_task]:
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
        "notifications": notifications,
    "fmt_dt": fmt_dt,
    "fmt_time": fmt_time,
    "fmt_countdown": _fmt_countdown,
    "now_utc": _now,
        **kwargs,
    })


# ---------- Pages ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        error = request.query_params.get("error", "")
        return _render("login.html", request, error=error)
    return RedirectResponse(url="/events?tab=register")


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, tab: str = "register", status: str = "all", date: str = "", q: str = "", sub: str = "current", page: int = 1, sort: str = "date"):
    user_id = get_user_id(request)
    if not user_id:
        return RedirectResponse(url="/")

    token = storage.get_token(user_id)
    if not token:
        return _render("login.html", request, error="Войди, чтобы продолжить")

    if tab == "notifications":
        history = storage.get_notifications(user_id)
        _notification_queue.pop(user_id, [])
        return _render("events.html", request, cur_tab="notifications", notifications_history=history, events=[])

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

    now = _now()
    watches = {w["event_id"]: w for w in storage.get_watchlist(user_id)}

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
        ev_start = parse_sirius_time(ev.event_start)
        is_past = ev_start and ev_start < now
        if tab == "my":
            in_my = ev.event_id in watches or ev.is_recorded or ev.is_reserved
            if not in_my:
                continue
            if sub == "current" and is_past:
                continue
            if sub == "past" and not is_past:
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
        ev._watched = ev.event_id in watches
        ev._is_past = _is_past_event(ev)

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
async def schedule_page(request: Request, date: str = ""):
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
                cached = await _sirius_client.fetch_schedule_day(date, token=token)
                _set_cached(cache_key, cached)
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
        reminder_event_ids=reminder_event_ids,
    )


@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist_page(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return RedirectResponse(url="/")

    watches = storage.get_all_watchlist(user_id)
    # Показываем только активную слежку и ошибки, зарегистрированные прячем
    watches = [w for w in watches if w["status"] != "registered"]
    token = storage.get_token(user_id)

    # Загружаем актуальное расписание, чтобы показать реальное время открытия записи
    by_id: dict[str, object] = {}
    cached: list | None = None
    if token and _sirius_client:
        try:
            cached = _get_cached(f"events:{user_id}")
            if cached is None:
                cached = _get_cached_any(f"events:{user_id}") or _get_persistent_events_cache(user_id) or []
            by_id = {ev.event_id: ev for ev in cached}
        except Exception as e:
            log.warning("Failed to fetch schedule for watchlist: %s", e)

    now = _now()
    watches_out = []
    for w in watches:
        w = dict(w)
        ev = by_id.get(w["event_id"])
        target = poller.event_target_time(ev) if ev else parse_sirius_time(w["event_start"])
        w["_snipe_active"] = poller.is_sniping(user_id, w["event_id"])
        w["_target_dt"] = target
        w["_snipe_opening_iso"] = target.astimezone(_MSK).isoformat() if target else ""
        w["_snipe_label"] = ""
        if target:
            remaining = (target - now).total_seconds()
            if remaining > poller.WARMUP_WINDOW:
                h = int(remaining // 3600)
                m = int((remaining % 3600) // 60)
                start_msk = target.astimezone(_MSK).strftime("%H:%M")
                w["_snipe_label"] = (
                    f"запись откроется в {start_msk} (через {h}ч {m}м)"
                    if h else f"запись откроется в {start_msk} (через {m}м)"
                )
            elif remaining > 0:
                w["_snipe_label"] = f"начну ловить через {int(remaining)}с"
            elif w["_snipe_active"]:
                w["_snipe_label"] = "ловлю открытие прямо сейчас"
            else:
                start_msk = target.astimezone(_MSK).strftime("%d.%m %H:%M")
                w["_snipe_label"] = f"запись открылась {start_msk}"
        watches_out.append(w)

    return _render("watchlist.html", request, watches=watches_out)


@app.get("/custom-events", response_class=HTMLResponse)
async def custom_events_page(request: Request, date: str = ""):
    user_id = get_user_id(request)
    if not user_id:
        return RedirectResponse(url="/")
    events = storage.get_custom_events(user_id, date) if date else storage.get_custom_events(user_id)
    today = _now().astimezone(_MSK).strftime("%Y-%m-%d")
    return _render("custom_events.html", request, custom_events=events, cur_date=today)


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


@app.get("/coins-info", response_class=HTMLResponse)
async def coins_info_page(request: Request):
    return _render("coins_info.html", request)


@app.get("/polymarket", response_class=HTMLResponse)
async def polymarket_page(request: Request):
    if not get_user_id(request):
        return RedirectResponse(url="/")
    return _render("polymarket.html", request)


@app.get("/dronebet", response_class=HTMLResponse)
async def dronebet_page(request: Request):
    if not get_user_id(request):
        return RedirectResponse(url="/")
    return _render("dronebet.html", request)


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
    if is_new_session:
        user_id = storage.get_user_by_uid(uid) or uid
    else:
        user_id = _resolve_uid_session(request, uid, user_id, session_id)
    storage.save_token(user_id, token)
    storage.set_user_uid(user_id, uid)
    storage.mark_token_verified(user_id, token)
    storage.set_login_type(user_id, "token")

    storage.ensure_coins(uid)
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

    if not _sirius_client:
        msg = quote("Сервис недоступен")
        return RedirectResponse(url=f"/?error={msg}", status_code=303)

    # Accept both JSON (old clients) and form data (new form)
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        email = data.get("email", "").strip()
        password = data.get("password", "").strip()
    else:
        form_data = await request.form()
        email = form_data.get("email", "").strip()
        password = form_data.get("password", "").strip()

    if not email or not password:
        return RedirectResponse(url="/?error=Email+и+пароль+обязательны", status_code=303)

    try:
        token = await _sirius_client.login(email, password)
    except Exception as e:
        return RedirectResponse(url=f"/?error={quote(f'Ошибка входа: {_friendly_error(e)}')}", status_code=303)

    if not token:
        return RedirectResponse(url="/?error=Неверный+email+или+пароль", status_code=303)

    exp = token_expiry(token)
    if not exp:
        return RedirectResponse(url="/?error=Некорректный+токен", status_code=303)

    payload = _decode_jwt(token)
    if not payload or not payload.get("id"):
        return RedirectResponse(url="/?error=Не удалось определить аккаунт Sirius", status_code=303)

    uid = payload["id"]
    if is_new_session:
        user_id = storage.get_user_by_uid(uid) or uid
    else:
        user_id = _resolve_uid_session(request, uid, user_id, session_id)

    storage.save_token(user_id, token)
    storage.set_user_uid(user_id, uid)
    storage.mark_token_verified(user_id, token)
    storage.save_login_credentials(user_id, email, password)

    storage.ensure_coins(uid)
    full_name = " ".join(filter(None, [payload.get("lastName"), payload.get("firstName"), payload.get("middleName")]))
    storage.save_known_uid(uid, user_id, full_name)

    response = RedirectResponse(url="/events?tab=register", status_code=303)
    if is_new_session:
        session_id = storage.create_session_for_user(user_id)
        response.set_cookie(key="session_id", value=session_id, max_age=86400 * 365, httponly=True, samesite="lax")
    return response


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
        wants_json = True
    else:
        form_data = await request.form()
        code = form_data.get("code", "").strip()
        wants_json = False

    user_id = storage.consume_login_code(code)
    if not user_id:
        if wants_json:
            return JSONResponse({"ok": False, "error": "Код неверный или уже истёк"}, status_code=400)
        return RedirectResponse(url=f"/?error={quote('Код неверный или уже истёк')}", status_code=303)

    session_id = storage.create_session_for_user(user_id)
    if wants_json:
        response = JSONResponse({"ok": True})
    else:
        response = RedirectResponse(url="/events?tab=register", status_code=303)
    response.set_cookie(key="session_id", value=session_id, max_age=86400 * 365, httponly=True, samesite="lax")
    return response


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


def _drone_alert_view(alert, viewer_uid: str) -> dict:
    market = storage.get_prediction_market(int(alert["market_id"]))
    source_url = str(alert["source_url"] or "")
    if not source_url.startswith(("https://", "http://")):
        source_url = ""
    return {
        "id": alert["id"],
        "started_at": alert["started_at"],
        "ended_at": alert["ended_at"],
        "duration_seconds": alert["duration_seconds"],
        "result_option": alert["result_option"],
        "source_message": alert["source_message"],
        "source_url": source_url,
        "market": _prediction_market_view(market, viewer_uid) if market else None,
    }


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


@app.get("/api/dronebet")
async def api_dronebet(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    uid = _session_uid(user_id)
    if not uid:
        return JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)
    active = storage.get_active_drone_alert()
    radar_state = storage.get_drone_radar_state()
    history = storage.get_drone_alerts()
    return JSONResponse({
        "ok": True,
        "coins": storage.get_coins(uid),
        "status": {
            "active": bool(radar_state["active"]) if radar_state else bool(active),
            "since": radar_state["changed_at"] if radar_state else (active["started_at"] if active else 0),
            "message": radar_state["message"] if radar_state else (active["source_message"] if active else ""),
        },
        "current": _drone_alert_view(active, uid) if active else None,
        "history": [_drone_alert_view(alert, uid) for alert in history],
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
    if market["created_by"] == "dronebet":
        return JSONResponse({"ok": False, "error": "ДронБет рассчитывается только по данным Sirius Radar"}, status_code=400)
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
    if market and market["created_by"] == "dronebet":
        return JSONResponse({"ok": False, "error": "Автоматический рынок ДронБета нельзя удалить вручную"}, status_code=400)
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
    if market["created_by"] == "dronebet":
        return JSONResponse({"ok": False, "error": "Автоматический рынок ДронБета нельзя отменить вручную"}, status_code=400)
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


@app.post("/api/coins/transfer")
async def api_coins_transfer(request: Request):
    user_id = get_user_id(request)
    if not user_id:
        return JSONResponse({"ok": False, "error": "Не авторизован"}, status_code=401)
    from_uid = _session_uid(user_id)
    if not from_uid:
        return JSONResponse({"ok": False, "error": "Токен не задан"}, status_code=400)

    data = await request.json()
    to_uid = data.get("to_uid", "").strip()
    try:
        amount = int(data.get("amount", 0))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "Некорректное количество"}, status_code=400)

    if not to_uid or to_uid == from_uid:
        return JSONResponse({"ok": False, "error": "Некорректный получатель"}, status_code=400)
    if amount < 1:
        return JSONResponse({"ok": False, "error": "Количество должно быть > 0"}, status_code=400)

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
                "coins": u["coins"],
                "trust_level": 0 if u["is_admin"] else u["trust_level"],
                "is_admin": bool(u["is_admin"]),
            }
            for u in users
        ]
    })


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
        result = await _sirius_client.subscribe(event_id, token=token)
        outcome, text = classify_subscribe_result(result)
        if outcome == "ok":
            final_reserved = result.reserved
            try:
                fresh_events = await _sirius_client.fetch_schedule(token=token)
                _set_events_cached(user_id, fresh_events)
                actual = next((e for e in fresh_events if e.event_id == event_id), None)
                if actual:
                    final_reserved = bool(actual.is_reserved)
                    if actual.is_recorded and not actual.is_reserved:
                        final_reserved = False
            except Exception as e:
                log.warning("Failed to verify subscribe status for %s: %s", event_id, e)
            storage.set_watch_status(user_id, event_id, "registered")
            _invalidate_events_cache(user_id)
            return JSONResponse({"ok": True, "reserved": final_reserved, "text": text})
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
        return JSONResponse({"ok": False, "error": "Слежка не найдена"}, status_code=404)

    old_cost = watch["coin_cost"] if "coin_cost" in watch.keys() else storage.snipe_priority_cost(watch["snipe_priority"])
    new_cost = storage.snipe_priority_cost(snipe_priority)
    uid = _session_uid(user_id) or ""

    if uid and new_cost > old_cost:
        if not storage.reserve_coins(uid, new_cost - old_cost):
            return JSONResponse({"ok": False, "error": f"Не хватает Sirius Coins. Нужно добавить: {new_cost - old_cost}."}, status_code=402)
    elif uid and old_cost > new_cost:
        storage.release_coins(uid, old_cost - new_cost)

    storage.update_watch_priority(user_id, event_id, snipe_priority, new_cost)

    if token and _sirius_client:
        poller.cancel_snipe(user_id, event_id)
        ev = None
        try:
            cached = _get_cached(f"events:{user_id}") or _get_cached_any(f"events:{user_id}") or _get_persistent_events_cache(user_id) or []
            ev = next((e for e in cached if e.event_id == event_id), None)
            if ev is None:
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

    result = await _send_push_to_user(
        user_id,
        "🔔 Тестовый push\nЕсли телефон заблокирован или вкладка закрыта, это должно прийти системным уведомлением.",
        "reminder",
    )
    return JSONResponse({"ok": True, **result})


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
        "trustLevel": trust_level,
        "expiresAt": dt.datetime.fromtimestamp(exp_ts, tz=dt.timezone.utc).isoformat() if exp_ts else None,
        "issuedAt": dt.datetime.fromtimestamp(iat_ts, tz=dt.timezone.utc).isoformat() if iat_ts else None,
        "remainingSeconds": int(remaining),
        "watchedCount": watched_count,
        "registeredCount": registered_count,
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
    storage.remove_custom_event(user_id, event_id)
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
