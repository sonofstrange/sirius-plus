"""Autonomous read-only probe for an alternative Sirius group schedule.

Run this file directly from PyCharm. It only sends GET requests and never
changes registrations, subscriptions or the local database.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
from urllib.parse import urlencode

from sirius_api import (
    SCHEDULE_DAY_URL,
    SCHEDULE_URL,
    SiriusClient,
    _parse_events,
    _parse_schedule_events,
)


# ---- Local test settings -------------------------------------------------
# Paste a current AuthToken from Sirius here. Do not commit it or send this
# file to anyone while the token is present.
TEST_TOKEN = ""

# Change this to the group whose schedule you want to try to obtain.
TARGET_GROUP = "БВ3"

# Empty means today's date. Set YYYY-MM-DD if the events you expect are on a
# different day, for example "2026-07-24".
TEST_DATE = ""


# These are the conventional query parameter names used by APIs for a group.
# The probe runs every name against both schedule endpoints once.
GROUP_QUERY_KEYS = (
    "union",
    "unions",
    "unionName",
    "unionId",
    "group",
    "groupName",
    "groupId",
    "team",
)


def fingerprint(events) -> str:
    source = "\n".join(sorted(f"{event.event_id}:{event.event_name}" for event in events))
    return hashlib.sha256(source.encode()).hexdigest()[:12]


def event_groups(events) -> list[str]:
    return sorted({str(group) for event in events for group in event.unions if str(group).strip()})


def report(label: str, events, baseline_fingerprint: str | None = None) -> bool:
    current_fingerprint = fingerprint(events)
    changed = baseline_fingerprint is not None and current_fingerprint != baseline_fingerprint
    target_count = sum(
        TARGET_GROUP.casefold() in " ".join(map(str, event.unions)).casefold()
        for event in events
    )
    state = "ОТВЕТ ИЗМЕНИЛСЯ" if changed else "без изменений"
    print(f"{label}: {len(events)} событий; {state}; событий группы {TARGET_GROUP}: {target_count}")
    return changed


async def fetch(client: SiriusClient, url: str):
    result = await client._js_fetch(url, token=TEST_TOKEN)
    if not result.get("ok"):
        print(f"{url}: HTTP {result.get('status')}")
        return None
    return result.get("json") or {}


async def probe() -> None:
    if not TEST_TOKEN.strip():
        print("Вставь AuthToken Sirius в TEST_TOKEN вверху файла и запусти снова.")
        return

    date = TEST_DATE.strip() or dt.date.today().isoformat()
    print(f"Проверяю группу: {TARGET_GROUP}; дата: {date}")
    print(f"Запросов будет: {2 + len(GROUP_QUERY_KEYS) * 2}. Только GET, без записи на события.")

    client = SiriusClient(headless=True)
    await client.start()
    try:
        record_json = await fetch(client, SCHEDULE_URL)
        day_url = f"{SCHEDULE_DAY_URL}?{urlencode({'date': date})}"
        day_json = await fetch(client, day_url)
        if record_json is None or day_json is None:
            print("Итог: базовый endpoint не ответил. Проверь, что токен действующий.")
            return

        record_events = _parse_events(record_json)
        day_events = _parse_schedule_events(day_json)
        record_fingerprint = fingerprint(record_events)
        day_fingerprint = fingerprint(day_events)
        print("\nБазовые ответы")
        report("record", record_events)
        print("Группы record:", ", ".join(event_groups(record_events)) or "нет")
        report("day", day_events)
        print("Группы day:", ", ".join(event_groups(day_events)) or "нет")

        changes = []
        print("\nПараметры группы")
        for key in GROUP_QUERY_KEYS:
            record_url = f"{SCHEDULE_URL}?{urlencode({key: TARGET_GROUP})}"
            record_variant = await fetch(client, record_url)
            if record_variant is not None:
                events = _parse_events(record_variant)
                if report(f"record?{key}", events, record_fingerprint):
                    changes.append((f"record?{key}", events))

            day_variant_url = f"{SCHEDULE_DAY_URL}?{urlencode({'date': date, key: TARGET_GROUP})}"
            day_variant = await fetch(client, day_variant_url)
            if day_variant is not None:
                events = _parse_schedule_events(day_variant)
                if report(f"day?{key}", events, day_fingerprint):
                    changes.append((f"day?{key}", events))

        print("\nИтог")
        if not changes:
            print("Все известные параметры проигнорированы: API отдаёт расписание только владельца токена.")
            return
        print("Следующие варианты меняют ответ. Это ещё не доказательство доступа к чужой группе, проверь названия событий:")
        for label, events in changes:
            print(f"- {label}: {[event.event_name for event in events[:6]]}")
    finally:
        await client.stop()


if __name__ == "__main__":
    asyncio.run(probe())
