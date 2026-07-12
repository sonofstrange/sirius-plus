from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Awaitable, Callable

import storage
from sirius_api import BASE_HEADERS, SCHEDULE_URL, SUBSCRIBE_URL, SiriusClient, _parse_events


@dataclass
class ProbeResult:
    ok: bool
    ms: float
    events: int = 0
    detail: str = ""


async def page_fetch(client: SiriusClient, token: str) -> int:
    result = await client._page.evaluate(
        """
        async ([url, token]) => {
            const resp = await fetch(url, {
                headers: {
                    authorization: 'Bearer ' + token,
                    accept: 'application/json;charset=utf-8',
                },
                credentials: 'include',
            });
            const text = await resp.text();
            let json = null;
            try { json = JSON.parse(text); } catch {}
            return {status: resp.status, ok: resp.ok, body: text, json};
        }
        """,
        [SCHEDULE_URL, token],
    )
    if not result["ok"]:
        raise RuntimeError(f"HTTP {result['status']}: {result['body'][:140]}")
    return len(_parse_events(result["json"] or {}))


async def install_page_fast_fetch(client: SiriusClient):
    await client._page.evaluate(
        """
        () => {
            window.__siriusFastFetch = async (url, token) => {
                const resp = await fetch(url, {
                    headers: {
                        authorization: 'Bearer ' + token,
                        accept: 'application/json;charset=utf-8',
                    },
                    credentials: 'include',
                });
                const text = await resp.text();
                let json = null;
                try { json = JSON.parse(text); } catch {}
                return {status: resp.status, ok: resp.ok, body: text, json};
            };
        }
        """
    )


async def page_function_fetch(client: SiriusClient, token: str) -> int:
    result = await client._page.evaluate(
        "async ([url, token]) => await window.__siriusFastFetch(url, token)",
        [SCHEDULE_URL, token],
    )
    if not result["ok"]:
        raise RuntimeError(f"HTTP {result['status']}: {result['body'][:140]}")
    return len(_parse_events(result["json"] or {}))


async def cookie_header(client: SiriusClient) -> str:
    cookies = await client._context.cookies("https://my.sirius.online")
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


async def request_context_cookie_fetch(client: SiriusClient, token: str) -> int:
    headers = dict(BASE_HEADERS)
    headers["authorization"] = "Bearer " + token
    headers["cookie"] = await cookie_header(client)
    resp = await client._context.request.fetch(
        SCHEDULE_URL,
        method="GET",
        headers=headers,
        timeout=10_000,
    )
    text = await resp.text()
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status}: {text[:140]}")
    return len(_parse_events(data or {}))


def urllib_cookie_fetch_sync(cookie: str, token: str) -> int:
    headers = dict(BASE_HEADERS)
    headers["authorization"] = "Bearer " + token
    headers["cookie"] = cookie
    req = urllib.request.Request(SCHEDULE_URL, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            data = json.loads(text)
            return len(_parse_events(data or {}))
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {text[:140]}") from e


async def urllib_cookie_fetch(client: SiriusClient, token: str) -> int:
    cookie = await cookie_header(client)
    return await asyncio.to_thread(urllib_cookie_fetch_sync, cookie, token)


async def page_parallel_fetch(client: SiriusClient, token: str, count: int) -> int:
    result = await client._page.evaluate(
        """
        async ([url, token, count]) => {
            const one = async () => {
                const resp = await fetch(url, {
                    headers: {
                        authorization: 'Bearer ' + token,
                        accept: 'application/json;charset=utf-8',
                    },
                    credentials: 'include',
                });
                const text = await resp.text();
                let json = null;
                try { json = JSON.parse(text); } catch {}
                return {status: resp.status, ok: resp.ok, body: text, json};
            };
            return await Promise.all(Array.from({length: count}, one));
        }
        """,
        [SCHEDULE_URL, token, count],
    )
    failures = [r for r in result if not r["ok"]]
    if failures:
        r = failures[0]
        raise RuntimeError(f"{len(failures)}/{count} failed, first HTTP {r['status']}: {r['body'][:140]}")
    return sum(len(_parse_events(r["json"] or {})) for r in result)


async def subscribe_invalid_parallel(client: SiriusClient, token: str, count: int) -> int:
    result = await client._page.evaluate(
        """
        async ([url, token, count]) => {
            const one = async (idx) => {
                const resp = await fetch(url, {
                    method: 'POST',
                    headers: {
                        authorization: 'Bearer ' + token,
                        accept: 'application/json;charset=utf-8',
                        'content-type': 'application/json',
                    },
                    credentials: 'include',
                    body: JSON.stringify({eventId: '__latency_probe_' + Date.now() + '_' + idx}),
                });
                const text = await resp.text();
                let json = null;
                try { json = JSON.parse(text); } catch {}
                return {status: resp.status, ok: resp.ok, body: text, json};
            };
            return await Promise.all(Array.from({length: count}, (_, idx) => one(idx)));
        }
        """,
        [SUBSCRIBE_URL, token, count],
    )
    # Invalid IDs are expected to fail at business/API level; transport latency is what matters here.
    return len(result)


async def measure(
    label: str,
    fn: Callable[[], Awaitable[int]],
    count: int,
    pause: float,
) -> list[ProbeResult]:
    print(f"\n--- {label} ---")
    results: list[ProbeResult] = []
    for i in range(count):
        t0 = time.perf_counter()
        try:
            events = await fn()
            ms = (time.perf_counter() - t0) * 1000
            results.append(ProbeResult(True, ms, events))
            print(f"{label} {i + 1}: OK {ms:.1f} ms, events={events}")
        except Exception as e:
            ms = (time.perf_counter() - t0) * 1000
            detail = str(e).replace("\n", " ")[:180]
            results.append(ProbeResult(False, ms, detail=detail))
            print(f"{label} {i + 1}: ERROR {ms:.1f} ms, {detail}")
        await asyncio.sleep(pause)
    ok = [r.ms for r in results if r.ok]
    if ok:
        print(
            f"{label}_SUMMARY: success={len(ok)}/{count} "
            f"min={min(ok):.1f} avg={statistics.mean(ok):.1f} "
            f"median={statistics.median(ok):.1f} max={max(ok):.1f}"
        )
    else:
        print(f"{label}_SUMMARY: success=0/{count}")
    return results


def get_token() -> str:
    storage.init_db()
    users = storage.get_all_users_with_tokens()
    if not users:
        raise RuntimeError("В локальной БД нет сохранённых токенов")
    token = storage.get_token(users[0])
    if not token:
        raise RuntimeError("Токен пустой")
    return token


async def main():
    parser = argparse.ArgumentParser(description="Compare Sirius fetch latency methods.")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--pause", type=float, default=0.35)
    parser.add_argument("--parallel-count", type=int, default=5)
    parser.add_argument(
        "--method",
        choices=[
            "all",
            "page",
            "page-fn",
            "request-cookie",
            "urllib-cookie",
            "page-parallel",
            "subscribe-invalid-parallel",
        ],
        default="all",
    )
    parser.add_argument(
        "--include-post-probe",
        action="store_true",
        help="Also measure parallel POST /subscribe with invalid event IDs. It should not subscribe to anything.",
    )
    args = parser.parse_args()

    token = get_token()
    client = SiriusClient(headless=True)
    await client.start()
    try:
        await install_page_fast_fetch(client)
        methods: dict[str, Callable[[], Awaitable[int]]] = {
            "page": lambda: page_fetch(client, token),
            "page-fn": lambda: page_function_fetch(client, token),
            "request-cookie": lambda: request_context_cookie_fetch(client, token),
            "urllib-cookie": lambda: urllib_cookie_fetch(client, token),
            "page-parallel": lambda: page_parallel_fetch(client, token, args.parallel_count),
            "subscribe-invalid-parallel": lambda: subscribe_invalid_parallel(client, token, args.parallel_count),
        }
        selected = list(methods.keys()) if args.method == "all" else [args.method]
        if not args.include_post_probe and "subscribe-invalid-parallel" in selected:
            selected.remove("subscribe-invalid-parallel")
        for name in selected:
            count = 1 if name == "page-parallel" else args.count
            await measure(name, methods[name], count, args.pause)
    finally:
        await client.stop()


if __name__ == "__main__":
    asyncio.run(main())
