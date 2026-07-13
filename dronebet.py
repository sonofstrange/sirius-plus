"""Automatic DroneBet markets driven by the Sirius Radar state API."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from typing import Awaitable, Callable

import storage
from sirius_radar import fetch_radar_state

log = logging.getLogger("dronebet")

RADAR_POLL_INTERVAL = 5
DRONEBET_OPTIONS = ["<45 минут", "45–90 минут", "90–180 минут", "180+ минут"]
LEGACY_DRONEBET_OPTIONS = ["До 30 минут", "30–60 минут", "1–2 часа", "2–4 часа", "4+ часов"]
NotifyFn = Callable[[str, str, str], Awaitable[None]]


def duration_option(duration_seconds: int, options: list[str] | None = None) -> str:
    options = options or DRONEBET_OPTIONS
    if options == LEGACY_DRONEBET_OPTIONS:
        if duration_seconds < 30 * 60:
            return options[0]
        if duration_seconds < 60 * 60:
            return options[1]
        if duration_seconds < 2 * 60 * 60:
            return options[2]
        if duration_seconds < 4 * 60 * 60:
            return options[3]
        return options[4]
    if duration_seconds < 45 * 60:
        return options[0]
    if duration_seconds < 90 * 60:
        return options[1]
    if duration_seconds < 180 * 60:
        return options[2]
    return options[3]


def _state_timestamp(state: dict) -> int:
    event = state.get("event") or {}
    value = event.get("occurred_at") or event.get("received_at") or state.get("updated_at")
    if isinstance(value, str):
        try:
            return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return int(time.time())


async def sync_radar_state(state: dict, notify: NotifyFn) -> None:
    event = state.get("event") or {}
    active = bool(state.get("active"))
    timestamp = _state_timestamp(state)
    storage.set_drone_radar_state(active, timestamp, str(event.get("message") or ""))
    if active:
        storage.create_drone_alert(
            str(event.get("id") or ""),
            timestamp,
            str(event.get("message") or ""),
            str(event.get("source_url") or ""),
            DRONEBET_OPTIONS,
        )
        return

    alert = storage.get_active_drone_alert()
    if not alert:
        return
    market = storage.get_prediction_market(int(alert["market_id"]))
    try:
        market_options = json.loads(market["options_json"]) if market else DRONEBET_OPTIONS
    except (TypeError, json.JSONDecodeError):
        market_options = DRONEBET_OPTIONS
    result = duration_option(max(0, timestamp - int(alert["started_at"])), market_options)
    if market and market["status"] == "open":
        ok, error, payouts = storage.resolve_prediction_market(int(alert["market_id"]), correct_option=result)
        if not ok:
            log.warning("Could not resolve DroneBet market %s: %s", alert["market_id"], error)
            return
        else:
            for uid, payout in payouts:
                user_id = storage.get_user_by_uid(uid)
                if not user_id:
                    continue
                text = (
                    f"🚁 ДронБет рассчитан: «{result}». Ты получил {payout} Сириус Коин(ов)."
                    if payout else f"🚁 ДронБет рассчитан: «{result}». Эта ставка не принесла коинов."
                )
                await notify(user_id, text, "info")
    storage.finish_drone_alert(int(alert["id"]), timestamp, result)


async def run_dronebet_monitor(notify: NotifyFn) -> None:
    while True:
        try:
            state = await asyncio.to_thread(fetch_radar_state)
            await sync_radar_state(state, notify)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Sirius Radar is unavailable: %s", exc)
        await asyncio.sleep(RADAR_POLL_INTERVAL)
