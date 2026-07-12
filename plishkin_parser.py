"""Small, isolated parser for the public Plishkin VK feed.

This module does not poll VK or create markets. It only recognizes the two
official BPLA messages, so DroneBet can later reuse a tested signal source.
"""

from __future__ import annotations

import re

PLISHKIN_URL = "https://vk.com/ds.plishkin"

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
