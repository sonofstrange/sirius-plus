"""Small, isolated parser for the public Plishkin VK feed.

This module does not poll VK or create markets. It only recognizes the two
official BPLA messages, so DroneBet can later reuse a tested signal source.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PLISHKIN_URL = "https://vk.com/ds.plishkin"
VK_API_VERSION = "5.199"

_THREAT_RE = re.compile(r"внимание!\s*существует\s+угроза\s+атаки\s+бпла", re.I)
_CLEAR_RE = re.compile(r"отбой\s+угрозы\s+атаки\s+бпла", re.I)


def detect_bpla_status(text: str) -> str | None:
    """Return the newest visible official status from a newest-first VK feed."""
    normalized = " ".join(text.replace("\xa0", " ").split())
    matches = [
        (match.start(), "threat") for match in _THREAT_RE.finditer(normalized)
    ] + [
        (match.start(), "clear") for match in _CLEAR_RE.finditer(normalized)
    ]
    return min(matches, default=(None, None))[1]


def detect_latest_bpla_status(posts: list[str]) -> str | None:
    """Inspect newest-first VK posts and return the first official status."""
    for post in posts:
        status = detect_bpla_status(post)
        if status:
            return status
    return None


def fetch_recent_vk_posts(access_token: str, count: int = 10) -> list[str]:
    """Read the newest posts through VK's API instead of scraping its web UI."""
    params = urlencode({
        "domain": "ds.plishkin",
        "count": max(1, min(count, 100)),
        "access_token": access_token,
        "v": VK_API_VERSION,
    })
    request = Request(
        f"https://api.vk.com/method/wall.get?{params}",
        headers={"User-Agent": "SiriusPlus/1.0"},
    )
    with urlopen(request, timeout=25) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if "error" in payload:
        raise RuntimeError(f"VK API: {payload['error'].get('error_msg', 'unknown error')}")
    return [item.get("text", "") for item in payload.get("response", {}).get("items", [])]
