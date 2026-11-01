import unittest
from datetime import datetime
from unittest.mock import patch

import bot


class ReminderTests(unittest.TestCase):
    def test_study_events_and_existing_online_events(self):
        for title in ("Зачет по истории", "ЗАЧЁТ", "Зачёты", "Перед консультацией",
                      "Консультация", "Консультации", "Онлайн урок", "Вебинар"):
            with self.subTest(title=title), patch.object(bot, "REMIND_ALL", False):
                self.assertTrue(bot.needs_reminder({"content": title}))
        for title in ("Купить хлеб", "Зачетный фильм", ""):
            with self.subTest(title=title), patch.object(bot, "REMIND_ALL", False):
                self.assertFalse(bot.needs_reminder({"content": title}))

    def test_remind_requires_date_time_and_matching_slot(self):
        def task(title, when):
            return {"content": title, "due": {"date": when}}
        tasks = [task("Зачет", "2026-11-01T15:30:00"),
                 task("Консультация", "2026-11-01T15:30:00"),
                 task("Онлайн урок", "2026-11-01T15:30:00"),
                 task("Зачет без времени", "2026-11-01"),
                 task("Зачет завтра", "2026-11-02T15:30:00"),
                 task("Купить хлеб", "2026-11-01T15:30:00"),
                 task("Консультация позже", "2026-11-01T16:00:00")]
        with patch.object(bot, "now", return_value=datetime(2026, 11, 1, 15, 0, tzinfo=bot.TZ)), \
                patch.object(bot, "td_all", return_value=tasks), \
                patch.object(bot, "REMIND_ALL", False), patch.object(bot, "send") as send:
            bot.remind()
            self.assertEqual(send.call_count, 3)

    def test_pause_suppresses_study_reminders(self):
        tasks = [{"content": "Пауза", "description": "с 2026-11-01", "due": {"date": "2026-11-02"}},
                 {"content": "Зачёт", "due": {"date": "2026-11-01T15:30:00"}}]
        with patch.object(bot, "now", return_value=datetime(2026, 11, 1, 15, 0, tzinfo=bot.TZ)), \
                patch.object(bot, "td_all", return_value=tasks), patch.object(bot, "send") as send:
            bot.remind()
            send.assert_not_called()

    def test_release_version_and_html_escaping(self):
        env = {"GITHUB_SHA": "a" * 40, "TELEGRAM_TOKEN": "test", "TELEGRAM_CHAT_ID": "42",
               "BOT_CHANGELOG": "Зачёты и консультации <b>новые</b>"}
        with patch.dict(bot.os.environ, env), patch.object(bot, "send") as send:
            bot.notify_update()
            text = send.call_args.args[0]
            self.assertIn("gh.aaaaaaaa", text)
            self.assertIn("&lt;b&gt;новые&lt;/b&gt;", text)


if __name__ == "__main__":
    unittest.main()
