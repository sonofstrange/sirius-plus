import asyncio
import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

import main
from sirius_api import EventInfo


def _request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": []})


class NavigationTests(unittest.TestCase):
    def test_legacy_watchlist_opens_auto_registration(self):
        response = asyncio.run(main.watchlist_page(_request("/watchlist")))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/events?tab=my&sub=watch")

    def test_home_opens_schedule_for_signed_in_user(self):
        with patch.object(main, "get_user_id", return_value="user-1"):
            response = asyncio.run(main.index(_request("/")))
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/schedule")

    def test_legacy_polymarket_opens_coins_section(self):
        response = asyncio.run(main.polymarket_page(_request("/polymarket")))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/coins-info?tab=polymarket")

    def test_auto_registration_state_is_scheduled_before_warmup(self):
        target = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.timezone.utc)
        event = SimpleNamespace(event_id="event-1", event_start=target.isoformat())
        watch = {"snipe_priority": "medium", "coin_cost": 1, "event_start": target.isoformat()}

        with (
            patch.object(main.poller, "event_target_time", return_value=target),
            patch.object(main.poller, "is_sniping", return_value=False),
        ):
            main._decorate_auto_registration(
                event,
                watch,
                "user-1",
                target - dt.timedelta(hours=2),
            )

        self.assertTrue(event._watched)
        self.assertEqual(event._watch["priority_code"], "medium")
        self.assertEqual(event._watch["priority"], "Средний приоритет")
        self.assertEqual(event._watch["status"], "Запланирована до открытия 20.07 15:00")

    def test_auto_registration_state_shows_active_snipe(self):
        target = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.timezone.utc)
        event = SimpleNamespace(event_id="event-2", event_start=target.isoformat())
        watch = {"snipe_priority": "high", "coin_cost": 2, "event_start": target.isoformat()}

        with (
            patch.object(main.poller, "event_target_time", return_value=target),
            patch.object(main.poller, "is_sniping", return_value=True),
        ):
            main._decorate_auto_registration(event, watch, "user-1", target - dt.timedelta(seconds=20))

        self.assertEqual(event._watch["status"], "Ловит открытие сейчас")


    def test_events_page_handles_regular_sirius_event_without_event_type(self):
        event = EventInfo(
            event_id="event-1", event_name="Regular Sirius event", day_iso="2026-07-20",
            event_start="2026-07-20T12:00:00Z", record_start=None, record_end=None,
            is_available=True, reasons=[], will_open_at=None, is_recorded=False,
            is_reserved=False, people_current=0, people_max=10, description="", raw={},
        )
        request = _request("/events")
        with (
            patch.object(main, "get_user_id", return_value="user-1"),
            patch.object(main.storage, "get_token", return_value="token"),
            patch.object(main.storage, "get_watchlist", return_value=[]),
            patch.object(main.storage, "get_community_events_for_user", return_value=[]),
            patch.object(main.storage, "is_admin", return_value=False),
            patch.object(main, "_session_uid", return_value="uid-1"),
            patch.object(main, "_get_cached", return_value=[event]),
            patch.object(main, "_render", return_value="rendered") as render,
        ):
            result = asyncio.run(main.events_page(request))

        self.assertEqual(result, "rendered")
        self.assertEqual(render.call_args.args[0], "events.html")


if __name__ == "__main__":
    unittest.main()
