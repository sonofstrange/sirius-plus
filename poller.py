from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
import time
from dataclasses import asdict
from typing import Callable, Awaitable

import storage
from sirius_api import SiriusClient, EventInfo, classify_subscribe_result, parse_sirius_time, token_expiry

log = logging.getLogger("poller")

POLLER_TICK = 15
PREWARM_WINDOW = 120
WARMUP_WINDOW = 30
SNIPE_MAX_DURATION = 20 * 60
RESCHEDULE_DRIFT = 30
MAX_CONCURRENT_USERS = 20

NotifyFn = Callable[[str, str], Awaitable[None]]

_snipe_tasks: dict[tuple[str, str], dict] = {}

_last_invalid_token_notify: dict[str, float] = {}
INVALID_TOKEN_NOTIFY_COOLDOWN = 6 * 60 * 60
_MSK = dt.timezone(dt.timedelta(hours=3))
_risk_backoff_until = 0.0


def _uid_from_token(token: str) -> str:
    import base64
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return payload.get("id", "")
    except Exception:
        return ""


def _snipe_interval_for_uid(uid: str) -> float:
    try:
        level = storage.get_trust_level(uid)
    except Exception:
        level = 2
    if level in (0, 1):
        return 2.5
    if level == 3:
        return 15.0
    return 5.0


def _snipe_interval(uid: str, snipe_priority: str = "high") -> float:
    return _snipe_interval_for_uid(uid) * storage.snipe_priority_multiplier(snipe_priority)


def _poll_interval_for_uid(uid: str) -> float:
    try:
        level = storage.get_trust_level(uid)
    except Exception:
        level = 2
    if level in (0, 1):
        return 5 * 60
    if level == 3:
        return 30 * 60
    return 10 * 60


def _with_jitter(seconds: float) -> float:
    return seconds * random.uniform(0.88, 1.18)


def _looks_like_sirius_risk(msg: str) -> bool:
    low = msg.lower()
    return any(x in low for x in ("http 503", "http 403", "ngenix", "challenge", "too many requests", "429"))


def _note_sirius_risk(reason: str):
    global _risk_backoff_until
    backoff = random.uniform(5 * 60, 10 * 60)
    _risk_backoff_until = max(_risk_backoff_until, time.time() + backoff)
    log.warning("Sirius risk backoff for background polling: %.0fs (%s)", backoff, reason[:160])


def _log_snipe_attempt(
    user_id: str,
    event_id: str,
    event_name: str,
    phase: str,
    status_code: int | None = None,
    success: bool = False,
    reserved: bool = False,
    message: str = "",
    latency_ms: int = 0,
):
    try:
        storage.add_snipe_attempt(
            user_id,
            event_id,
            event_name,
            phase,
            status_code=status_code,
            success=success,
            reserved=reserved,
            message=message,
            latency_ms=latency_ms,
        )
    except Exception as e:
        log.warning("failed to write snipe attempt log: %s", e)


def _persist_events_cache(user_id: str, events: list[EventInfo]):
    try:
        storage.set_events_cache(user_id, json.dumps([asdict(e) for e in events], ensure_ascii=False))
    except Exception as e:
        log.warning("failed to persist events cache for %s: %s", user_id, e)


def event_target_time(ev: EventInfo) -> dt.datetime | None:
    t = parse_sirius_time(ev.will_open_at)
    if t:
        return t
    return parse_sirius_time(ev.record_start)


def _fmt_event_dt(iso_str: str | None) -> str:
    d = parse_sirius_time(iso_str)
    if not d:
        return "неизвестно"
    return d.astimezone(_MSK).strftime("%d.%m %H:%M МСК")


def is_sniping(user_id: str, event_id: str) -> bool:
    return (user_id, event_id) in _snipe_tasks


def cancel_snipe(user_id: str, event_id: str):
    entry = _snipe_tasks.pop((user_id, event_id), None)
    if entry:
        entry["task"].cancel()
    # Coin will be released by api_unwatch calling storage.release_coin


def _is_subscribe_success(result) -> bool:
    """Sirius sometimes returns 204/empty body on success; only explicit errors block success."""
    if result.status_code not in (200, 201, 204):
        return False

    body = (result.body or "").strip()
    if not body:
        return True

    try:
        data = json.loads(body)
    except Exception:
        return True

    errors = data.get("error") if isinstance(data, dict) else None
    return not errors


def schedule_snipe(
    user_id: str,
    token: str,
    event_id: str,
    event_name: str,
    client: SiriusClient,
    notify: NotifyFn,
    target_time: dt.datetime | None,
    uid: str = "",
    reserve_only: bool = False,
    snipe_priority: str = "high",
    coin_cost: int = 1,
):
    key = (user_id, event_id)
    existing = _snipe_tasks.get(key)
    if existing:
        prev_target = existing["target"]
        task = existing["task"]
        if task.done():
            _snipe_tasks.pop(key, None)
            existing = None
        else:
            now = client.now()
            if (
                target_time is None
                or prev_target is None
                or now >= prev_target - dt.timedelta(seconds=WARMUP_WINDOW)
            ):
                return
    if existing:
        prev_target = existing["target"]
        task = existing["task"]
        same = (prev_target is None and target_time is None) or (
            prev_target is not None and target_time is not None
            and abs((prev_target - target_time).total_seconds()) <= RESCHEDULE_DRIFT
        )
        if same:
            return
        existing["task"].cancel()

    task = asyncio.create_task(
        _snipe_loop(user_id, token, event_id, event_name, client, notify, target_time, uid, reserve_only, snipe_priority, coin_cost)
    )
    _snipe_tasks[key] = {"task": task, "target": target_time}


def on_watch_added(
    user_id: str,
    token: str,
    event_id: str,
    ev: EventInfo,
    client: SiriusClient,
    notify: NotifyFn,
    snipe_priority: str = "high",
    coin_cost: int | None = None,
):
    uid = _uid_from_token(token)
    target = event_target_time(ev)
    if coin_cost is None:
        coin_cost = storage.snipe_priority_cost(snipe_priority)
    schedule_snipe(user_id, token, event_id, ev.event_name, client, notify, target, uid, "noSpace" in ev.reasons, snipe_priority, coin_cost)


async def _detect_changes(user_id: str, events: list, notify: NotifyFn):
    now_map = {e.event_id: {"name": e.event_name, "start": e.event_start,
                            "available": e.is_available, "raw": {}} for e in events}
    for e in events:
        now_map[e.event_id]["raw"] = {
            "location": e.raw.get("eventLocation") or e.raw.get("location", ""),
            "end": e.raw.get("eventEnd", ""),
        }

    prev_raw = storage.get_event_snapshot(user_id)
    prev_map: dict = json.loads(prev_raw) if prev_raw else {}

    if not prev_map:
        storage.set_event_snapshot(user_id, json.dumps(now_map, ensure_ascii=False, default=str))
        return

    for eid in now_map:
        cur = now_map[eid]
        prev = prev_map.get(eid)
        if prev is None:
            await notify(user_id, f"🆕 Новое событие: «{cur['name']}»\nКогда: {_fmt_event_dt(cur.get('start'))}")
            continue
        changes = []
        if cur["name"] != prev["name"]:
            changes.append(f"название: «{prev['name']}» → «{cur['name']}»")
        if cur["start"] != prev["start"]:
            changes.append(
                "время изменилось\n"
                f"было: {_fmt_event_dt(prev.get('start'))}\n"
                f"стало: {_fmt_event_dt(cur.get('start'))}"
            )
        cur_loc = cur.get("raw", {}).get("location", "")
        prev_loc = prev.get("raw", {}).get("location", "")
        if cur_loc and prev_loc and cur_loc != prev_loc:
            changes.append(f"место: {prev_loc} → {cur_loc}")
        if changes:
            await notify(user_id, f"✏️ «{cur['name']}»: " + "; ".join(changes))

    for eid in prev_map:
        if eid not in now_map:
            prev = prev_map[eid]
            await notify(user_id, f"🗑 Событие «{prev['name']}» пропало из расписания.\nБыло: {_fmt_event_dt(prev.get('start'))}")

    storage.set_event_snapshot(user_id, json.dumps(now_map, ensure_ascii=False, default=str))


async def _snipe_loop(
    user_id: str,
    token: str,
    event_id: str,
    event_name: str,
    client: SiriusClient,
    notify: NotifyFn,
    target_time: dt.datetime | None,
    uid: str = "",
    reserve_only: bool = False,
    snipe_priority: str = "high",
    coin_cost: int = 1,
):
    snipe_interval = _snipe_interval(uid, snipe_priority)
    try:
        if target_time is not None:
            prewarm_at = target_time - dt.timedelta(seconds=PREWARM_WINDOW)
            prewarm_delay = (prewarm_at - client.now()).total_seconds()
            if prewarm_delay > 0:
                await asyncio.sleep(prewarm_delay)
            if hasattr(client, "refresh_session"):
                t0 = time.perf_counter()
                try:
                    await client.refresh_session()
                    _log_snipe_attempt(user_id, event_id, event_name, "prewarm", success=True, message="session refreshed", latency_ms=int((time.perf_counter() - t0) * 1000))
                except Exception as e:
                    _log_snipe_attempt(user_id, event_id, event_name, "prewarm_error", success=False, message=str(e), latency_ms=int((time.perf_counter() - t0) * 1000))

            wake_at = target_time - dt.timedelta(seconds=WARMUP_WINDOW)
            delay = (wake_at - client.now()).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)

        deadline_base = max(target_time, client.now()) if target_time is not None else client.now()
        deadline = deadline_base + dt.timedelta(seconds=SNIPE_MAX_DURATION)

        remaining_str = ""
        if target_time:
            now = client.now()
            remaining = (target_time - now).total_seconds()
            if remaining > 0:
                h = int(remaining // 3600)
                m = int((remaining % 3600) // 60)
                remaining_str = f", через {h}ч {m}м" if h else f", через {m}м"
            t_msk = target_time.astimezone(dt.timezone(dt.timedelta(hours=3)))
            await notify(user_id,
                f"🎯 Начинаю ловить открытие «{event_name}» (открытие в {t_msk.strftime('%d.%m %H:%M')} МСК{remaining_str})")
        else:
            await notify(user_id,
                f"🎯 Начинаю ловить открытие «{event_name}» — запись уже открыта")
        _log_snipe_attempt(user_id, event_id, event_name, "start", success=True, message=f"interval={snipe_interval:.1f}s priority={snipe_priority}")

        while True:
            if client.now() > deadline:
                storage.set_watch_status(user_id, event_id, "failed")
                if uid:
                    if coin_cost <= 1:
                        storage.release_coin(uid)
                    else:
                        storage.release_coins(uid, coin_cost)
                _log_snipe_attempt(user_id, event_id, event_name, "timeout", success=False, message="deadline exceeded")
                await notify(user_id, f"😢 «{event_name}»: так и не поймал открытие записи за отведённое время — похоже, сайт не открыл её вовремя.")
                return

            t0 = time.perf_counter()
            try:
                result = await client.subscribe(event_id, token=token)
            except Exception as e:
                _log_snipe_attempt(user_id, event_id, event_name, "error", success=False, message=str(e), latency_ms=int((time.perf_counter() - t0) * 1000))
                log.warning("subscribe error for %s, will retry until deadline: %s", event_id, e)
                await asyncio.sleep(snipe_interval)
                continue

            # Only stop on explicit success; retry on anything else
            success = _is_subscribe_success(result)
            _log_snipe_attempt(
                user_id,
                event_id,
                event_name,
                "try",
                status_code=result.status_code,
                success=success,
                reserved=bool(result.reserved),
                message=(result.body or "")[:300],
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
            if success:
                final_reserved = result.reserved
                if hasattr(client, "fetch_schedule"):
                    try:
                        fresh = await client.fetch_schedule(token=token)
                        _persist_events_cache(user_id, fresh)
                        actual = next((e for e in fresh if e.event_id == event_id), None)
                        if actual:
                            final_reserved = bool(actual.is_reserved)
                            if actual.is_recorded and not actual.is_reserved:
                                final_reserved = False
                    except Exception as e:
                        log.warning("final status fetch failed for %s: %s", event_id, e)
                if uid:
                    if coin_cost <= 1:
                        storage.spend_reserved_coin(uid)
                    else:
                        if not storage.spend_reserved_coins(uid, coin_cost):
                            storage.spend_reserved_coin(uid)
                storage.set_watch_status(user_id, event_id, "registered")
                _log_snipe_attempt(user_id, event_id, event_name, "success", status_code=result.status_code, success=True, reserved=bool(final_reserved), message="registered")
                if final_reserved:
                    await notify(user_id, f"😅 «{event_name}» — основные места разобрали, но я успел записать тебя в резерв.")
                else:
                    await notify(user_id, f"✅ Записал тебя на «{event_name}»!")
                return

            log.info("snipe retry %s: HTTP %s %s", event_id, result.status_code, result.body[:120])
            await asyncio.sleep(snipe_interval)
    except asyncio.CancelledError:
        raise
    finally:
        _snipe_tasks.pop((user_id, event_id), None)


async def poll_user_once(
    user_id: str,
    token: str,
    client: SiriusClient,
    notify: NotifyFn,
):
    watches = {w["event_id"]: w for w in storage.get_watchlist(user_id)}

    # Auto-refresh token if expired and we have login credentials
    exp = token_expiry(token)
    if exp and exp <= client.now():
        creds = storage.get_login_credentials(user_id)
        if creds:
            email, password = creds
            log.info("auto-refresh token for %s", user_id)
            try:
                new_token = await client.login(email, password)
                if new_token:
                    storage.save_token(user_id, new_token)
                    token = new_token
                    await notify(user_id, "🔄 Токен Sirius автоматически обновлён.")
                else:
                    await notify(user_id, "⚠️ Не удалось автоматически обновить токен Sirius. Зайди на страницу входа.")
            except Exception as e:
                log.warning("auto-refresh failed for %s: %s", user_id, e)

    try:
        events = await client.fetch_schedule(token=token)
        _persist_events_cache(user_id, events)
    except Exception as e:
        msg = str(e)
        code = 0
        if "HTTP 401" in msg:
            code = 401
        elif "HTTP 503" in msg or "HTTP 403" in msg:
            code = 503
            _note_sirius_risk(msg)
        elif _looks_like_sirius_risk(msg):
            _note_sirius_risk(msg)
        if code in (401, 503):
            now_ts = time.time()
            last = _last_invalid_token_notify.get(user_id, 0)
            if now_ts - last > INVALID_TOKEN_NOTIFY_COOLDOWN:
                _last_invalid_token_notify[user_id] = now_ts
                text = (
                    "🔑 Твой токен Sirius протух. Обнови его на странице входа."
                    if code == 401 else
                    "⚠️ Сайт Sirius временно недоступен (503). Если ошибка повторяется — попробуй обновить токен на странице входа."
                )
                await notify(user_id, text)
        else:
            log.warning("schedule fetch failed for %s: %s", user_id, e)
        return

    try:
        await _detect_changes(user_id, events, notify)
    except Exception as e:
        log.warning("change detection failed for %s: %s", user_id, e)

    if not watches:
        return

    by_id = {e.event_id: e for e in events}

    uid = _uid_from_token(token)

    for event_id, watch in watches.items():
        ev = by_id.get(event_id)
        if ev is None:
            continue
        if is_sniping(user_id, event_id) and _snipe_tasks[(user_id, event_id)]["target"] is None:
            continue
        target = event_target_time(ev)
        snipe_priority = watch["snipe_priority"] if "snipe_priority" in watch.keys() else "high"
        coin_cost = watch["coin_cost"] if "coin_cost" in watch.keys() else storage.snipe_priority_cost(snipe_priority)
        schedule_snipe(user_id, token, event_id, ev.event_name, client, notify, target, uid, "noSpace" in ev.reasons, snipe_priority, coin_cost)


async def run_poller(notify: NotifyFn, client: SiriusClient):
    storage.init_db()
    storage.cleanup_snipe_attempts()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_USERS)
    next_poll_at: dict[str, float] = {}

    # Restore active snipes from DB on startup
    all_watches = storage.get_all_active_watches()
    by_user: dict[str, list[tuple[str, str]]] = {}
    for w in all_watches:
        by_user.setdefault(w["user_id"], []).append((
            w["event_id"],
            w["event_name"],
            w["snipe_priority"] if "snipe_priority" in w.keys() else "high",
            w["coin_cost"] if "coin_cost" in w.keys() else 1,
        ))
    for user_id, events in by_user.items():
        token = storage.get_token(user_id)
        if token:
            try:
                all_events = await client.fetch_schedule(token=token)
                _persist_events_cache(user_id, all_events)
                by_id = {e.event_id: e for e in all_events}
                for event_id, event_name, snipe_priority, coin_cost in events:
                    ev = by_id.get(event_id)
                    if ev:
                        uid = _uid_from_token(token)
                        target = event_target_time(ev)
                        schedule_snipe(user_id, token, event_id, event_name, client, notify, target, uid, "noSpace" in ev.reasons, snipe_priority, coin_cost)
            except Exception as e:
                log.warning("Failed to restore snipes for %s: %s", user_id, e)

    async def poll_one(user_id: str):
        token = storage.get_token(user_id)
        if not token:
            return
        async with semaphore:
            try:
                await poll_user_once(user_id, token, client, notify)
                uid = _uid_from_token(token)
                next_poll_at[user_id] = time.time() + _with_jitter(_poll_interval_for_uid(uid))
            except Exception as e:
                log.exception("poll_user_once crashed for %s: %s", user_id, e)
                uid = _uid_from_token(token)
                next_poll_at[user_id] = time.time() + _with_jitter(min(_poll_interval_for_uid(uid), 5 * 60))

    while True:
        now_ts = time.time()
        if now_ts < _risk_backoff_until:
            log.debug("coarse scan skipped by Sirius risk backoff, left=%.0fs", _risk_backoff_until - now_ts)
            await asyncio.sleep(POLLER_TICK)
            continue
        active_user_ids = storage.get_all_users_with_tokens()
        due = []
        for user_id in active_user_ids:
            if user_id not in next_poll_at:
                token = storage.get_token(user_id) or ""
                uid = _uid_from_token(token)
                next_poll_at[user_id] = now_ts + random.uniform(0, min(60, _poll_interval_for_uid(uid) / 4))
            if now_ts >= next_poll_at[user_id]:
                due.append(user_id)
        await asyncio.gather(*(poll_one(uid) for uid in due))
        log.debug("coarse scan done, due=%s, users=%s, active snipes=%s", len(due), len(active_user_ids), len(_snipe_tasks))
        await asyncio.sleep(POLLER_TICK)


async def run_reminder_checker(notify: NotifyFn, now_fn: Callable[[], dt.datetime] | None = None):
    REMINDER_CHECK_INTERVAL = 60
    if now_fn is None:
        now_fn = lambda: dt.datetime.now(dt.timezone.utc)
    while True:
        try:
            reminders = storage.get_all_reminders()
            for r in reminders:
                event_start = parse_sirius_time(r["event_start"])
                if not event_start:
                    continue
                notify_at = event_start - dt.timedelta(minutes=r["minutes_before"])
                if now_fn() >= notify_at:
                    user_id = r["user_id"]
                    event_name = r["event_name"]
                    start_msk = event_start.astimezone(dt.timezone(dt.timedelta(hours=3)))
                    text = f"🔔 Напоминание: «{event_name}» начинается в {start_msk.strftime('%H:%M')} МСК"
                    await notify(user_id, text)
                    if storage.is_daily_custom_event(r["event_id"]):
                        tomorrow = event_start + dt.timedelta(days=1)
                        storage.add_reminder(user_id, r["event_id"], event_name, tomorrow.isoformat(), r["minutes_before"])
                    storage.remove_reminder(user_id, r["event_id"])
        except Exception as e:
            log.warning("reminder checker error: %s", e)
        await asyncio.sleep(REMINDER_CHECK_INTERVAL)
