#!/usr/bin/env python3
"""List local token owners and expiry without printing any credentials or tokens."""

from __future__ import annotations

import base64
import datetime as dt
import json

import storage


MSK = dt.timezone(dt.timedelta(hours=3))


def _payload(token: str) -> dict:
    try:
        value = token.split(".")[1]
        value += "=" * (-len(value) % 4)
        return json.loads(base64.urlsafe_b64decode(value))
    except Exception:
        return {}


def _fmt_expiry(exp: object) -> tuple[str, str]:
    try:
        expiry = dt.datetime.fromtimestamp(float(exp), tz=dt.timezone.utc).astimezone(MSK)
    except (TypeError, ValueError, OSError):
        return "unknown", ""
    remaining = expiry - dt.datetime.now(dt.timezone.utc).astimezone(MSK)
    state = "expired" if remaining.total_seconds() <= 0 else f"{int(remaining.total_seconds() // 3600)}h left"
    return expiry.strftime("%Y-%m-%d %H:%M MSK"), state


def main():
    with storage.get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, uid, login_type, token_saved_at FROM users WHERE sirius_token != '' ORDER BY token_saved_at"
        ).fetchall()

    if not rows:
        print("No saved tokens.")
        return

    for row in rows:
        token = storage.get_token(row["user_id"])
        payload = _payload(token or "")
        jwt_uid = str(payload.get("id") or "unknown")
        name = " ".join(filter(None, [payload.get("lastName"), payload.get("firstName"), payload.get("middleName")])) or "unknown"
        expiry, state = _fmt_expiry(payload.get("exp"))
        legacy = " legacy-local-id" if row["user_id"] != jwt_uid else ""
        print(
            f"user_id={row['user_id']} | jwt_uid={jwt_uid} | db_uid={row['uid'] or '-'} | "
            f"name={name} | login={row['login_type'] or '-'} | expires={expiry} ({state}){legacy}"
        )


if __name__ == "__main__":
    main()
