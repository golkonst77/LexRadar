"""Dashboard counters use the existing local queue; no CRM or working data."""
import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from unittest.mock import patch

from control_followup import Followup, UTC


class FollowupSummaryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.followup = Followup(temporary.name, journals=())
        self.now = datetime(2026, 11, 9, 21, 30, tzinfo=UTC)  # 10 November in Moscow.
        self.bindings = {}
        self.mock = patch('control_followup.campaign_bindings', return_value=(self.bindings, []))
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def add(self, eid, due, status='pending', confirmed=True):
        if confirmed:
            self.bindings[eid] = {'account_id': eid}
        row = dict(email_id=eid, account_id=eid, date_sent='2026-11-01T10:00:00Z')
        with closing(self.followup.connect()) as db, db:
            db.execute('INSERT INTO snapshot VALUES (?,?)', (eid, json.dumps(row)))
            db.execute('INSERT INTO actions VALUES (?,?,?,?,?,?)', (eid, eid, status, due, '', 1))

    def test_moscow_calendar_days_and_confirmed_active_only(self):
        self.add('today', '2026-11-09T22:00:00Z')
        self.add('later_today', '2026-11-10T20:00:00Z', 'deferred')
        self.add('overdue', '2026-11-09T20:59:00Z', 'no_answer')
        self.add('future', '2026-11-11T10:00:00Z')
        self.add('done', '2026-11-09T22:00:00Z', 'completed')
        self.add('refused', '2026-11-09T22:00:00Z', 'do_not_contact')
        self.add('draft', '2026-11-09T22:00:00Z', confirmed=False)
        self.add('blocked', '2026-11-09T22:00:00Z')
        with closing(self.followup.connect()) as db, db:
            db.execute('INSERT INTO blocked VALUES (?,?)', ('blocked', self.now.isoformat()))
        self.add('ready', '2026-11-09T20:00:00Z')
        self.followup.path.with_name('mailing.json').write_text(json.dumps({
            'today': {'id': 'today', 'account_id': 'today', 'task_id': 'task1'},
            'future': {'id': 'future', 'account_id': 'future', 'call_selected_at': self.now.isoformat()}}))
        html = self.followup.summary_html(self.now)
        self.assertIn('href="/followup"', html)
        for label, value in [('Всего КП ожидают контроля', 5), ('Рекомендовано к звонку', 1),
                             ('Выбрано для звонка', 1), ('Задачи CRM подтверждены', 1)]:
            self.assertIn(label + '</span><br><strong class="metric">' + str(value), html)

    def test_unavailable_invalid_and_incomplete_show_dashes(self):
        self.assertEqual(self.followup.summary_html(self.now).count('>—</strong>'), 4)
        self.assertFalse(self.followup.path.exists())
        self.add('invalid', 'bad-date')
        self.assertEqual(self.followup.summary_html(self.now).count('>—</strong>'), 4)
        with patch.object(self.followup, 'queue', return_value=([], ['Incomplete'])):
            self.assertEqual(self.followup.summary_html(self.now).count('>—</strong>'), 4)
        self.followup.path.write_bytes(b'broken sqlite')
        self.assertEqual(self.followup.summary_html(self.now).count('>—</strong>'), 4)

    def test_available_empty_queue_shows_real_zeros(self):
        with closing(self.followup.connect()):
            pass
        self.assertEqual(self.followup.summary_html(self.now).count('>0</strong>'), 4)


if __name__ == '__main__':
    unittest.main()
