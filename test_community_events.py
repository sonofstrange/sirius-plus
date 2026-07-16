import tempfile
import asyncio
import datetime as dt
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import main
import storage


class CommunityEventStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()
        storage.save_known_uid("100", "owner", "Owner Name")
        storage.save_known_uid("200", "coorganizer", "Coorganizer Name")
        storage.save_known_uid("300", "member", "Member Name")

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_community_event_enforces_capacity_and_tracks_coorganizer(self):
        event_id = storage.add_community_event(
            "owner", "100", "Community event", "2026-08-01", "12:00", "13:00",
            "", "", 1, "Description", "Hall", [("coorganizer", "200")],
        )

        self.assertTrue(storage.can_manage_community_event(event_id, "coorganizer"))
        self.assertTrue(storage.add_community_registration(event_id, "member")[0])
        self.assertEqual(storage.add_community_registration(event_id, "coorganizer"), (False, "full"))
        row = storage.get_community_events_for_date("member", "2026-08-01")[0]
        self.assertEqual(row["people_current"], 1)
        self.assertTrue(row["is_registered"])

    def test_recipient_resolution_accepts_full_name_without_guessing(self):
        resolved = storage.resolve_known_recipient("member name")

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["uid"], "300")

    def test_text_coorganizer_and_event_watch_cleanup_are_stored(self):
        event_id = storage.add_community_event(
            "owner", "100", "Community event", "2026-08-01", "12:00", "13:00",
            "", "", 5, "Description", "Hall", [("text:guest", "", "Guest organiser")],
        )

        coorganizers = storage.get_community_coorganizers(event_id)
        self.assertEqual(coorganizers[0]["display_name"], "Guest organiser")

        storage.add_watch("member", f"community_{event_id}", "Community event")
        removed = storage.remove_watches_for_event(f"community_{event_id}")
        self.assertEqual(len(removed), 1)
        self.assertIsNone(storage.get_watch("member", f"community_{event_id}"))

    def test_taking_pending_auto_registration_is_idempotent(self):
        storage.add_watch("member", "event-1", "Event", snipe_priority="medium")

        removed = storage.take_active_watch("member", "event-1")

        self.assertIsNotNone(removed)
        self.assertEqual(removed["coin_cost"], 1)
        self.assertIsNone(storage.take_active_watch("member", "event-1"))

    def test_community_contact_is_saved_and_visible_in_event(self):
        event_id = storage.add_community_event(
            "owner", "100", "Community event", "2026-08-01", "12:00", "13:00",
            "", "", 5, "Description", "Hall", [], contact="@organizer",
        )

        self.assertEqual(storage.get_community_event(event_id)["contact"], "@organizer")

    def test_registration_stays_open_while_community_event_is_running(self):
        event_id = storage.add_community_event(
            "owner", "100", "Community event", "2026-08-01", "12:00", "13:00",
            "", "", 5, "Description", "Hall", [],
        )
        event = storage.get_community_event(event_id)
        during_event = dt.datetime(2026, 8, 1, 9, 30, tzinfo=dt.timezone.utc)
        after_event = dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.timezone.utc)

        with patch.object(main, "_now", return_value=during_event):
            self.assertTrue(main._community_registration_open(event))
            self.assertEqual(main._community_event_payload(event, "owner")["status"], "ongoing")
        with patch.object(main, "_now", return_value=after_event):
            self.assertFalse(main._community_registration_open(event))
            self.assertEqual(main._community_event_payload(event, "owner")["status"], "finished")

    def test_community_change_summary_mentions_time_and_place(self):
        before = {"date_iso": "2026-08-01", "start_time": "12:00", "location": "Hall", "people_max": 5, "contact": "@old", "description": "Old"}
        after = {"date_iso": "2026-08-02", "start_time": "13:00", "location": "Park", "people_max": 10, "contact": "@new", "description": "New"}

        changes = main._community_event_change_lines(before, after)

        self.assertTrue(any(line.startswith("время:") for line in changes))
        self.assertTrue(any(line.startswith("место:") for line in changes))
        self.assertIn("обновлён контакт организатора", changes)


class CommunityAutoRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()
        storage.save_known_uid("100", "owner", "Owner")
        storage.save_known_uid("300", "member", "Member")
        storage.ensure_coins("300")
        self.messages = []

    async def asyncTearDown(self):
        import poller
        for entry in list(poller._snipe_tasks.values()):
            entry["task"].cancel()
        poller._snipe_tasks.clear()
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    async def test_community_auto_registration_registers_at_opening(self):
        import poller
        now = dt.datetime.now(dt.timezone.utc)
        local_now = now.astimezone(dt.timezone(dt.timedelta(hours=3)))
        event_id = storage.add_community_event(
            "owner", "100", "Community event", local_now.strftime("%Y-%m-%d"),
            (local_now + dt.timedelta(hours=1)).strftime("%H:%M"), "", now.isoformat(), "", 2,
            "Description", "Hall", [],
        )

        async def notify(user_id, text):
            self.messages.append((user_id, text))

        await poller._community_snipe_loop(
            "member", f"community_{event_id}", "Community event", now - dt.timedelta(seconds=1),
            "300", notify, 0,
        )

        event = storage.get_community_events_for_user("member")[0]
        self.assertTrue(event["is_registered"])
        self.assertTrue(any("Ты теперь записан" in text for _, text in self.messages))


if __name__ == "__main__":
    unittest.main()
