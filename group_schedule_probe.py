"""Read-only probe for checking whether Sirius supports another group's schedule.

The Sirius API normally scopes schedule data to the bearer token. This script
tries the likely documented-style group query names and reports whether any of
them changes the returned events. It never subscribes, unsubscribes or writes
to the local database.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
from urllib.parse import urlencode

import storage
from sirius_api import SCHEDULE_DAY_URL, SiriusClient, _parse_schedule_events


GROUP_QUERY_KEYS = ("union", "unions", "group", "team", "groupName")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Проверка расписания другой группы Sirius")
    parser.add_argument("--date", required=True, help="Дата в формате YYYY-MM-DD")
    parser.add_argument("--group", required=True, help="Название группы, например БВ3")
    parser.add_argument(
        "--user-id",
        help="ID аккаунта из локальной БД. Не указывай --token одновременно.",
    )
    parser.add_argument(
        "--token",
        help="Токен Sirius. Лучше используй переменную окружения SIRIUS_TOKEN, чтобы токен не попал в историю команд.",
    )
    return parser.parse_args()


def load_token(args: argparse.Namespace) -> str:
    if args.token and args.user_id:
        raise SystemExit("Укажи только --token или --user-id")
    token = args.token or os.environ.get("SIRIUS_TOKEN", "")
    if token:
        return token.strip()
    if args.user_id:
        token = storage.get_token(args.user_id)
        if token:
            return token
        raise SystemExit("Для этого user-id в локальной БД нет токена")
    raise SystemExit("Укажи --user-id или задай SIRIUS_TOKEN")


def schedule_fingerprint(events) -> str:
    value = "\n".join(sorted(f"{event.event_id}:{event.start_time}" for event in events))
    return hashlib.sha256(value.encode()).hexdigest()[:12]


async def probe(args: argparse.Namespace) -> int:
    token = load_token(args)
    client = SiriusClient(headless=True)
    await client.start()
    try:
        base_query = {"date": args.date}
        base_url = f"{SCHEDULE_DAY_URL}?{urlencode(base_query)}"
        base = await client._js_fetch(base_url, token=token)
        if not base.get("ok"):
            print(f"Базовый запрос не удался: HTTP {base.get('status')}")
            return 1
        base_events = _parse_schedule_events(base.get("json") or {})
        base_fingerprint = schedule_fingerprint(base_events)
        print(f"Базовое расписание: {len(base_events)} событий, fingerprint {base_fingerprint}")
        print("Группы в ответе:", ", ".join(sorted({str(group) for event in base_events for group in event.unions})) or "нет")

        changed = []
        for key in GROUP_QUERY_KEYS:
            query = {**base_query, key: args.group}
            url = f"{SCHEDULE_DAY_URL}?{urlencode(query)}"
            response = await client._js_fetch(url, token=token)
            if not response.get("ok"):
                print(f"{key}: HTTP {response.get('status')}")
                continue
            events = _parse_schedule_events(response.get("json") or {})
            fingerprint = schedule_fingerprint(events)
            matches_group = sum(args.group.casefold() in " ".join(map(str, event.unions)).casefold() for event in events)
            same = fingerprint == base_fingerprint
            print(f"{key}: {len(events)} событий, группы {matches_group}, {'без изменений' if same else 'ОТВЕТ ИЗМЕНИЛСЯ'}")
            if not same:
                changed.append((key, events))

        if not changed:
            print("Итог: параметр группы не поддержан или игнорируется. Токен видит только своё расписание.")
            return 0

        print("Итог: найдены параметры, меняющие ответ. Проверь события вручную перед использованием в сайте:")
        for key, events in changed:
            print(f"- {key}: {[event.event_name for event in events[:5]]}")
        return 0
    finally:
        await client.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(probe(parse_args())))
