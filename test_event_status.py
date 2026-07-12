import unittest

from sirius_api import _parse_events


class EventStatusTests(unittest.TestCase):
    def test_record_closed_reason_is_preserved(self):
        events = _parse_events({
            "success": [{
                "dayISO": "2026-07-12",
                "events": [{
                    "eventId": "event-1",
                    "eventName": "Тест",
                    "eventStart": "2026-07-12T10:00:00Z",
                    "availability": {
                        "isAvailable": False,
                        "reason": [{"type": "recordClosed"}],
                    },
                }],
            }],
        })

        self.assertEqual(events[0].reasons, ["recordClosed"])
        self.assertFalse(events[0].is_available)


if __name__ == "__main__":
    unittest.main()
