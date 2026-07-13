"""Client for the public Sirius Radar state API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from urllib.request import Request, urlopen

import storage

RADAR_API_URL = os.environ.get("SIRIUS_RADAR_API_URL", "http://109.248.207.162:8000")
RADAR_POLL_INTERVAL = max(10, int(os.environ.get("SIRIUS_RADAR_POLL_INTERVAL", "20")))
log = logging.getLogger("sirius_radar")
NotifyFn = Callable[[str, str, str], Awaitable[None]]


def fetch_radar_state(api_url: str = RADAR_API_URL) -> dict:
    """Return the latest Radar alarm state without needing a Radar token."""
    request = Request(f"{api_url.rstrip('/')}/api/state", headers={"User-Agent": "SiriusPlus/1.0"})
    with urlopen(request, timeout=15) as response:
        state = json.loads(response.read().decode("utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("active"), bool):
        raise RuntimeError("Sirius Radar returned an invalid state response")
    return state


def _alert_message(active: bool) -> str:
    if active:
        return "🚨 В Sirius объявлена угроза атаки БПЛА. Отойди от окон и укройся в безопасном месте."
    return "✅ Отбой угрозы атаки БПЛА в Sirius."


async def process_radar_state(state: dict, notify: NotifyFn) -> bool:
    """Persist a Radar state and broadcast only when it changes."""
    active = bool(state["active"])
    event = state.get("event") if isinstance(state.get("event"), dict) else {}
    message = str(event.get("message") or "")
    previous = storage.get_radar_alert_state()
    was_active = bool(previous["active"]) if previous else False
    storage.set_radar_alert_state(active, message)
    if active == was_active:
        return False

    text = _alert_message(active)
    kind = "alarm" if active else "info"
    for user_id in storage.get_all_users_with_tokens():
        await notify(user_id, text, kind)
    return True


async def run_radar_alert_monitor(notify: NotifyFn) -> None:
    """Poll the Plishkin/Radar API and notify all accounts on state transitions."""
    last_failed = False
    while True:
        try:
            state = await asyncio.to_thread(fetch_radar_state)
            changed = await process_radar_state(state, notify)
            if changed:
                log.info("Radar alert state changed: active=%s", state["active"])
            last_failed = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not last_failed:
                log.warning("Radar state request failed: %s", exc)
            last_failed = True
        await asyncio.sleep(RADAR_POLL_INTERVAL)
