import tempfile
import unittest
from pathlib import Path

import config
import storage


class FeedbackThreadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tmp.name) / "test.sqlite3")
        storage.init_db()

    def tearDown(self):
        config.DB_PATH = self.old_db_path
        self.tmp.cleanup()

    def test_reply_thread_preserves_sender_name_and_order(self):
        storage.add_feedback("user", "Первое сообщение")
        feedback = storage.get_user_feedback_messages("user")[0]
        storage.add_feedback_reply(feedback["id"], "admin", "Николай", "admin", "Ответ")
        storage.add_feedback_reply(feedback["id"], "user", "Пользователь", "user", "Уточнение")

        replies = storage.get_feedback_replies(feedback["id"])

        self.assertEqual([(reply["sender_type"], reply["sender_name"], reply["message"]) for reply in replies], [
            ("admin", "Николай", "Ответ"),
            ("user", "Пользователь", "Уточнение"),
        ])

    def test_deleting_feedback_removes_its_replies(self):
        storage.add_feedback("user", "Первое сообщение")
        feedback = storage.get_user_feedback_messages("user")[0]
        storage.add_feedback_reply(feedback["id"], "admin", "Николай", "admin", "Ответ")

        storage.delete_feedback(feedback["id"])

        self.assertEqual(storage.get_feedback_replies(feedback["id"]), [])


if __name__ == "__main__":
    unittest.main()
