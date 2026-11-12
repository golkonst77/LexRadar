"""Schedule settings checks use temporary files only; no CRM or worker."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from control_mailing import Mailing, MOSCOW, UTC, load_settings, next_slot


class MailingSettingsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / 'mailing-settings.json'
        self.queue = Mailing(self.directory)
        self.settings = load_settings(self.path)

    def slot(self, local, used=()):
        value = next_slot(datetime.fromisoformat(local).replace(tzinfo=MOSCOW), used, self.path)
        return datetime.fromisoformat(value).replace(tzinfo=UTC).astimezone(MOSCOW)

    def test_default_boundaries_and_weekend(self):
        for now, expected in [
                ('2026-11-09T08:00', '2026-11-09T09:00'),
                ('2026-11-09T08:50', '2026-11-09T09:15'),
                ('2026-11-09T17:34', '2026-11-09T17:45'),
                ('2026-11-09T17:35', '2026-11-10T09:00'),
                ('2026-11-14T18:00', '2026-11-16T09:00')]:
            with self.subTest(now=now):
                self.assertEqual(self.slot(now).isoformat(timespec='minutes')[:16], expected)
        self.assertFalse(self.path.exists())

    def test_intervals_and_occupied_tail(self):
        for interval in (5, 17, 30, 120):
            with self.subTest(interval=interval):
                self.queue.save_settings(dict(self.settings, interval_minutes=interval))
                occupied = datetime(2026, 11, 9, 9, 1, tzinfo=MOSCOW)
                slot = self.slot('2026-11-09T08:00', [occupied])
                self.assertGreaterEqual(slot - occupied, timedelta(minutes=interval))
                self.assertEqual((slot.hour * 60 + slot.minute - 540) % interval, 0)

    def test_weekdays_and_custom_window(self):
        self.queue.save_settings(dict(self.settings, weekdays=[7], start_time='10:07',
                                      end_time='10:38', interval_minutes=30))
        self.assertEqual(self.slot('2026-11-09T08:00').isoformat(), '2026-11-15T10:07:00+03:00')
        self.assertEqual(self.slot('2026-11-15T10:08').isoformat(), '2026-11-15T10:37:00+03:00')
        self.assertEqual(self.slot('2026-11-15T10:28').isoformat(), '2026-11-22T10:07:00+03:00')

    def test_save_reload_preserves_journals_and_rejects_invalid(self):
        journals = {name: '{"protected":true}' for name in
                    ('mailing.json', 'mailing-reschedule.json')}
        for name, value in journals.items():
            (self.directory / name).write_text(value)
        settings = dict(self.settings, interval_minutes=20, weekdays=[2, 4])
        self.queue.save_settings(settings)
        self.assertEqual(load_settings(self.path), settings)
        for name, value in journals.items():
            self.assertEqual((self.directory / name).read_text(), value)
        saved = self.path.read_bytes()
        for invalid in ({}, [], dict(settings, interval_minutes=True),
                        dict(settings, interval_minutes=4), dict(settings, interval_minutes=121),
                        dict(settings, weekdays=[]), dict(settings, weekdays=[0]),
                        dict(settings, weekdays=[True]), dict(settings, weekdays=[1, 1]),
                        dict(settings, start_time='9:00'), dict(settings, end_time='24:00'),
                        dict(settings, end_time='09:00'), dict(settings, timezone='UTC')):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.queue.save_settings(invalid)
            self.assertEqual(self.path.read_bytes(), saved)
            self.path.write_text(json.dumps(invalid))
            with self.assertRaises(ValueError):
                self.slot('2026-11-09T08:00')
            self.path.write_bytes(saved)
        self.path.write_text('{broken')
        with self.assertRaises(ValueError):
            self.slot('2026-11-09T08:00')


if __name__ == '__main__':
    unittest.main()
