#!/usr/bin/env python3
"""Print the current Sirius Radar alarm state."""

from __future__ import annotations

from sirius_radar import fetch_radar_state


def main() -> int:
    state = fetch_radar_state()
    status = "threat" if state["active"] else "clear"
    event = state.get("event") or {}
    print(f"Sirius Radar state: {status}")
    if event.get("message"):
        print(f"Message: {event['message']}")
    if state.get("updated_at"):
        print(f"Updated: {state['updated_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
