"""Client for the public Sirius Radar state API."""

from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

RADAR_API_URL = os.environ.get("SIRIUS_RADAR_API_URL", "http://109.248.207.162:8000")


def fetch_radar_state(api_url: str = RADAR_API_URL) -> dict:
    """Return the latest Radar alarm state without needing a Radar token."""
    request = Request(f"{api_url.rstrip('/')}/api/state", headers={"User-Agent": "SiriusPlus/1.0"})
    with urlopen(request, timeout=15) as response:
        state = json.loads(response.read().decode("utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("active"), bool):
        raise RuntimeError("Sirius Radar returned an invalid state response")
    return state
