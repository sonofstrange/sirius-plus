import asyncio
import datetime as dt
import json
import unittest
from unittest.mock import patch

import poller
from sirius_api import SubscribeResult


class FakeClient:
    def __init__(self, responses):
        self.current = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        self.responses = list(responses)
        self.calls = 0

    def now(self):
        return self.current

    async def subscribe(self, event_id, token=None):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def advance(self, seconds):
        self.current += dt.timedelta(seconds=seconds)


async def _notify_sink(user_id, text):
    _notify_sink.messages.append((user_id, text))


class EventStub:
    def __init__(self, event_id, name, start):
        self.event_id = event_id
        self.event_name = name
        self.event_start = start
        self.is_available = False
        self.raw = {}


class SnipeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _notify_sink.messages = []

    async def _run_snipe(self, client, uid="uid-1", target_time=None, max_duration=1200):
        statuses = []
        spent = []
        released = []
        real_sleep = asyncio.sleep

        async def fake_sleep(seconds):
            client.advance(seconds)
            await real_sleep(0)

        with (
            patch.object(poller.asyncio, "sleep", side_effect=fake_sleep),
            patch.object(poller, "SNIPE_MAX_DURATION", max_duration),
            patch.object(poller.storage, "set_watch_status", side_effect=lambda *args: statuses.append(args)),
            patch.object(poller.storage, "spend_reserved_coin", side_effect=lambda uid_arg: spent.append(uid_arg) or True),
            patch.object(poller.storage, "release_coin", side_effect=lambda uid_arg: released.append(uid_arg)),
        ):
            await poller._snipe_loop(
                user_id="user-1",
                token="token-1",
                event_id="event-1",
                event_name="Test event",
                client=client,
                notify=_notify_sink,
                target_time=target_time,
                uid=uid,
            )
        return statuses, spent, released

    async def test_204_empty_body_is_success(self):
        client = FakeClient([SubscribeResult(True, False, 204, "")])

        statuses, spent, released = await self._run_snipe(client)

        self.assertEqual(client.calls, 1)
        self.assertIn(("user-1", "event-1", "registered"), statuses)
        self.assertEqual(spent, ["uid-1"])
        self.assertEqual(released, [])

    async def test_success_notification_is_neutral_for_reserve(self):
        client = FakeClient([SubscribeResult(True, True, 201, "{}")])

        await self._run_snipe(client)

        self.assertEqual(
            _notify_sink.messages[-1][1],
            "✅ Ты теперь записан на «Test event».",
        )

    async def test_error_body_retries_until_success(self):
        client = FakeClient([
            SubscribeResult(True, False, 200, '{"error":[{"key":"record_disabled"}]}'),
            SubscribeResult(True, False, 201, '{"success":true}'),
        ])

        statuses, spent, released = await self._run_snipe(client)

        self.assertEqual(client.calls, 2)
        self.assertIn(("user-1", "event-1", "registered"), statuses)
        self.assertEqual(spent, ["uid-1"])
        self.assertEqual(released, [])

    async def test_network_errors_retry_until_success(self):
        client = FakeClient([
            RuntimeError("temporary network error"),
            RuntimeError("temporary network error"),
            RuntimeError("temporary network error"),
            SubscribeResult(True, False, 200, "{}"),
        ])

        statuses, spent, released = await self._run_snipe(client)

        self.assertEqual(client.calls, 4)
        self.assertIn(("user-1", "event-1", "registered"), statuses)
        self.assertEqual(spent, ["uid-1"])
        self.assertEqual(released, [])

    async def test_retries_stop_at_deadline_and_release_coin(self):
        client = FakeClient([
            SubscribeResult(False, False, 503, "busy"),
            SubscribeResult(False, False, 503, "busy"),
            SubscribeResult(False, False, 503, "busy"),
            SubscribeResult(False, False, 503, "busy"),
        ])

        statuses, spent, released = await self._run_snipe(client, max_duration=10)

        self.assertIn(("user-1", "event-1", "failed"), statuses)
        self.assertEqual(spent, [])
        self.assertEqual(released, ["uid-1"])


class ScheduledSnipeStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        poller._snipe_tasks.clear()

    async def asyncTearDown(self):
        tasks = [entry["task"] for entry in poller._snipe_tasks.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        poller._snipe_tasks.clear()

    async def test_future_snipe_is_not_active_before_warmup(self):
        client = FakeClient([])
        target = client.now() + dt.timedelta(hours=50)

        poller.schedule_snipe(
            "user-1", "token-1", "event-1", "Test event", client,
            _notify_sink, target,
        )
        await asyncio.sleep(0)

        self.assertIn(("user-1", "event-1"), poller._snipe_tasks)
        self.assertFalse(poller.is_sniping("user-1", "event-1"))


class ChangeDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _notify_sink.messages = []

    async def test_change_notifications_use_short_dates(self):
        prev_snapshot = json.dumps({
            "old": {"name": "Силовой зал", "start": "2026-07-08T17:00:00Z", "available": False, "raw": {}},
            "move": {"name": "Интеллектуальная игра", "start": "2026-07-08T17:00:00Z", "available": False, "raw": {}},
        })
        events = [
            EventStub("move", "Интеллектуальная игра", "2026-07-08T17:20:00Z"),
            EventStub("new", "Мастер-класс", "2026-07-11T16:30:00Z"),
        ]

        with (
            patch.object(poller.storage, "get_event_snapshot", return_value=prev_snapshot),
            patch.object(poller.storage, "set_event_snapshot"),
        ):
            await poller._detect_changes("user-1", events, _notify_sink)

        messages = [text for _, text in _notify_sink.messages]
        joined = "\n".join(messages)
        self.assertNotIn("2026-", joined)
        self.assertNotIn("Z", joined)
        self.assertTrue(any("Когда: 11.07 19:30 МСК" in text for text in messages))
        self.assertTrue(any("было: 08.07 20:00 МСК" in text and "стало: 08.07 20:20 МСК" in text for text in messages))
        self.assertTrue(any("Силовой зал" in text and "Было: 08.07 20:00 МСК" in text for text in messages))


if __name__ == "__main__":
    unittest.main()
