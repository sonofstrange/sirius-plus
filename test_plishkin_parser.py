import unittest

from plishkin_parser import detect_bpla_status, detect_latest_bpla_status


class PlishkinParserTests(unittest.TestCase):
    def test_detects_threat_message(self):
        text = (
            "Внимание! Существует угроза атаки БПЛА. Все силы и средства готовы "
            "к ее отражению."
        )
        self.assertEqual(detect_bpla_status(text), "threat")

    def test_detects_clear_message(self):
        self.assertEqual(detect_bpla_status("Отбой угрозы атаки БПЛА."), "clear")

    def test_prefers_newer_threat_over_an_older_clear_message(self):
        text = (
            "Внимание! Существует угроза атаки БПЛА. "
            "Отбой угрозы атаки БПЛА."
        )
        self.assertEqual(detect_bpla_status(text), "threat")

    def test_uses_the_newest_matching_post(self):
        posts = [
            "Внимание! Существует угроза атаки БПЛА.",
            "Отбой угрозы атаки БПЛА.",
        ]
        self.assertEqual(detect_latest_bpla_status(posts), "threat")

    def test_ignores_unrelated_post(self):
        self.assertIsNone(detect_bpla_status("Сегодня проходит обычное занятие."))


if __name__ == "__main__":
    unittest.main()
