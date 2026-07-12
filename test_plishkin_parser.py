import unittest

from plishkin_parser import detect_bpla_status


class PlishkinParserTests(unittest.TestCase):
    def test_detects_threat_message(self):
        text = (
            "Внимание! Существует угроза атаки БПЛА. Все силы и средства готовы "
            "к ее отражению."
        )
        self.assertEqual(detect_bpla_status(text), "threat")

    def test_detects_clear_message(self):
        self.assertEqual(detect_bpla_status("Отбой угрозы атаки БПЛА."), "clear")

    def test_ignores_unrelated_post(self):
        self.assertIsNone(detect_bpla_status("Сегодня проходит обычное занятие."))


if __name__ == "__main__":
    unittest.main()
