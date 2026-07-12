import json
import unittest

import main
from sirius_api import clean_description


class DescriptionTests(unittest.TestCase):
    def test_rich_sirius_description_becomes_readable_plain_text(self):
        description = (
            '<p><span style="font-weight:bolder">Прыгать можно не только на кровати!</span>'
            '&nbsp;<br>Представляешь, целое занятие, где можно прыгать сколько угодно!'
            '<br><span style="font-weight:bolder">Прыгаем вместе!</span></p>'
        )

        self.assertEqual(
            clean_description(description),
            "Прыгать можно не только на кровати!\n"
            "Представляешь, целое занятие, где можно прыгать сколько угодно!\n"
            "Прыгаем вместе!",
        )

    def test_cached_descriptions_are_cleaned_on_read(self):
        cached = json.dumps([{
            "event_id": "event-1",
            "event_name": "Тест",
            "day_iso": "2026-07-12",
            "event_start": "2026-07-12T10:00:00Z",
            "record_start": None,
            "record_end": None,
            "is_available": True,
            "reasons": [],
            "will_open_at": None,
            "is_recorded": False,
            "is_reserved": False,
            "people_current": 0,
            "people_max": 0,
            "description": "<p>Первая строка<br>Вторая строка</p>",
            "raw": {},
        }])

        events = main._deserialize_events(cached)

        self.assertEqual(events[0].description, "Первая строка\nВторая строка")


if __name__ == "__main__":
    unittest.main()
