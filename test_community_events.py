import tempfile
import unittest
from pathlib import Path

import config
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


if __name__ == "__main__":
    unittest.main()
