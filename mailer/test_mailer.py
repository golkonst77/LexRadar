import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mailer_v2 as m

CONFIG = m.read_json(Path(__file__).parent / 'config.example.json')


def draft(eid='one', email='one@example.org'):
    return dict(id=eid, status='Draft', to=email, cc='', bcc='',
        sendAt=None, accountId='account', **{'from': CONFIG['sender']},
        attachmentsIds=['pdf'], attachmentsNames={'pdf': 'report.pdf'})


class FakeAPI:
    def __init__(self, items):
        self.items = {v['id']: copy.deepcopy(v) for v in items}
        self.puts = []
        self.fail_after_write = False
        self.fail_get = False

    def get(self, kind, eid):
        if self.fail_get:
            raise RuntimeError('API unavailable')
        return {'id': eid} if kind == 'Account' else copy.deepcopy(self.items[eid])

    def scheduled(self):
        return [copy.deepcopy(v) for v in self.items.values() if v.get('sendAt')]

    def put(self, eid, value):
        self.puts.append((eid, value))
        self.items[eid]['sendAt'] = value
        if self.fail_after_write:
            raise RuntimeError('timeout after server commit')


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = dict(CONFIG, enabled=True, dry_run=False)
        self.save('config.json', self.config)
        self.save('approved.json', ['one', 'two'])
        self.rows = [{'id': 'one'}, {'id': 'two'}]
        self.api = FakeAPI([draft(), draft('two', 'two@example.org')])

    def save(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    def test_friday_cutoff_weekend_utc(self):
        after = datetime(2026, 10, 30, 17, 37, tzinfo=m.MOSCOW)
        slot = m.next_slot(after, [])
        self.assertEqual(slot.isoformat(), '2026-11-02T09:00:00+03:00')
        self.assertEqual(m.stamp(slot), '2026-11-02 06:00:00')

    def test_safe_last_slot(self):
        slot = m.next_slot(datetime(2026, 10, 30, 17, 35, tzinfo=m.MOSCOW), [])
        self.assertEqual(slot.minute, 36)

    def test_capacity_and_irregular_existing_slots(self):
        used = [datetime(2026, 10, 29, 9, n, tzinfo=m.MOSCOW) for n in (0, 10, 20, 30, 40)]
        slot = m.next_slot(datetime(2026, 10, 29, 8, tzinfo=m.MOSCOW), used)
        self.assertEqual(slot.hour, 10)
        self.assertTrue(all(abs((slot - v).total_seconds()) >= 720 for v in used[:-1]))

    def test_long_queue_never_exceeds_window_or_hour(self):
        used = []
        start = datetime(2026, 10, 29, 8, tzinfo=m.MOSCOW)
        for _ in range(150):
            m.next_slot(start, used)
        self.assertTrue(all(v.weekday() < 5 and 9 <= v.hour < 18 and (v.hour != 17 or v.minute <= 40) for v in used))
        self.assertLessEqual(max(m.Counter((v.date(), v.hour) for v in used).values()), 5)
        self.assertTrue(all((b-a).total_seconds() >= 720 for a, b in zip(used, used[1:])))

    def test_preview_four_ready_one_hold_no_put(self):
        rows = [{'id': str(i)} for i in range(5)]
        api = FakeAPI([draft(str(i), f'a{i}@example.org' if i < 4 else '') for i in range(5)])
        plan, _ = m.snapshot(api, rows, CONFIG, self.root, datetime.now(m.UTC))
        self.assertEqual(m.Counter(v['state'] for v in plan), {'READY': 4, 'HOLD': 1})
        self.assertEqual(api.puts, [])

    def test_invalid_addresses(self):
        for address in ('a@b', 'a@example.org,b@example.org', 'Name <a@example.org>', 'a..b@example.org', 'a@example.org\nb@example.org'):
            self.assertEqual(m.validate(draft(email=address), {}, CONFIG)[0], 'HOLD')

    def test_account_pdf_sender_cc_validation(self):
        for key, value in [('accountId', None), ('attachmentsIds', []), ('from', 'other@example.org'), ('cc', 'other@example.org')]:
            item = draft()
            item[key] = value
            self.assertEqual(m.validate(item, {}, CONFIG)[0], 'HOLD')
        self.assertEqual(m.validate(draft(), {'expected_pdf': 'wrong.pdf'}, CONFIG)[0], 'HOLD')

    def test_duplicate_marks_both_review(self):
        self.api.items['two']['to'] = ' ONE@EXAMPLE.ORG '
        plan, _ = m.snapshot(self.api, self.rows, CONFIG, self.root, datetime.now(m.UTC))
        self.assertEqual([p['state'] for p in plan], ['REVIEW', 'REVIEW'])

    def test_duplicate_of_scheduled_in_batch(self):
        self.api.items['two'].update(to='one@example.org', sendAt='2026-11-02 06:00:00')
        plan, _ = m.snapshot(self.api, self.rows, CONFIG, self.root, datetime.now(m.UTC))
        self.assertEqual([p['state'] for p in plan], ['REVIEW', 'SKIP'])

    def test_gate_config(self):
        for enabled, dry in [(False, False), (True, True), (False, True)]:
            self.save('config.json', dict(CONFIG, enabled=enabled, dry_run=dry))
            with self.assertRaises(ValueError):
                m.schedule(self.api, self.rows, self.root, datetime.now(m.UTC))
        self.assertEqual(self.api.puts, [])

    def test_only_approved_and_repeat_idempotent(self):
        self.save('approved.json', ['one'])
        m.schedule(self.api, self.rows, self.root, datetime.now(m.UTC))
        m.schedule(self.api, self.rows, self.root, datetime.now(m.UTC))
        self.assertEqual(len(self.api.puts), 1)
        self.assertEqual(self.api.puts[0][0], 'one')

    def test_two_approved_keep_separate_slots(self):
        m.schedule(self.api, self.rows, self.root, datetime.now(m.UTC))
        self.assertEqual(len(self.api.puts), 2)
        self.assertGreaterEqual((m.utc(self.api.puts[1][1])-m.utc(self.api.puts[0][1])).total_seconds(), 720)

    def test_partial_failure_does_not_retry_or_continue(self):
        self.api.fail_after_write = True
        with self.assertRaises(RuntimeError):
            m.schedule(self.api, self.rows, self.root, datetime.now(m.UTC))
        self.assertEqual(len(self.api.puts), 1)
        self.api.fail_after_write = False
        m.schedule(self.api, self.rows, self.root, datetime.now(m.UTC))
        self.assertEqual([eid for eid, _ in self.api.puts], ['one', 'two'])

    def test_intent_without_server_commit_blocks_retry(self):
        m.log(self.root, 'one', 'schedule', '2026-11-02 06:00:00', 'INTENT')
        self.save('approved.json', ['one'])
        plan = m.schedule(self.api, self.rows, self.root, datetime.now(m.UTC))
        self.assertEqual(plan[0]['state'], 'REVIEW')
        self.assertEqual(self.api.puts, [])

    def test_api_failure_no_writes(self):
        self.api.fail_get = True
        with self.assertRaises(RuntimeError):
            m.schedule(self.api, self.rows, self.root, datetime.now(m.UTC))
        self.assertEqual(self.api.puts, [])

    def test_sent_not_rescheduled(self):
        self.api.items['one']['status'] = 'Sent'
        self.save('approved.json', ['one'])
        m.schedule(self.api, self.rows, self.root, datetime.now(m.UTC))
        self.assertEqual(self.api.puts, [])

    def test_cancel_only_owned_future_draft(self):
        with self.assertRaises(ValueError):
            m.cancel(self.api, self.root, ['one'])
        slot = m.stamp(datetime.now(m.UTC) + timedelta(days=2))
        self.api.items['one']['sendAt'] = slot
        m.log(self.root, 'one', 'schedule', slot, 'SCHEDULED')
        self.api.items['one']['status'] = 'Sent'
        with self.assertRaises(ValueError):
            m.cancel(self.api, self.root, ['one'])
        self.api.items['one']['status'] = 'Draft'
        m.cancel(self.api, self.root, ['one'])
        self.assertEqual(self.api.puts, [('one', None)])
        self.assertEqual(m.owned(self.root), {})

    def test_cancel_close_slot_refused(self):
        slot = m.stamp(datetime.now(m.UTC) + timedelta(minutes=5))
        self.api.items['one']['sendAt'] = slot
        m.log(self.root, 'one', 'schedule', slot, 'SCHEDULED')
        with self.assertRaises(ValueError):
            m.cancel(self.api, self.root, ['one'])
        self.assertEqual(self.api.puts, [])

    def test_cli_missing_flags_never_constructs_api(self):
        for command in ('schedule', 'cancel'):
            with patch.object(m, 'ROOT', self.root), patch.object(m.sys, 'argv', ['mailer_v2.py', command]), patch.object(m, 'API') as api:
                self.assertEqual(m.main(), 1)
                api.assert_not_called()

    def test_pause_during_validation_prevents_put(self):
        original = self.api.get
        calls = 0
        def get(kind, eid):
            nonlocal calls
            calls += 1
            if calls == 9:
                self.save('config.json', dict(self.config, enabled=False))
            return original(kind, eid)
        self.api.get = get
        with self.assertRaises(ValueError):
            m.schedule(self.api, self.rows, self.root, datetime.now(m.UTC))
        self.assertEqual(self.api.puts, [])

    def test_scheduled_pagination_and_invalid_results(self):
        with patch.dict(m.os.environ, {'ESPO_API_KEY': 'fake-test-key'}):
            api = m.API(CONFIG)
        page = [{'id': str(i), 'sendAt': '2026-11-02 06:00:00'} for i in range(200)]
        with patch.object(api, 'request', side_effect=[{'list': page, 'total': 201}, {'list': [{'id': 'last', 'sendAt': '2026-11-02 06:12:00'}], 'total': 201}]) as request:
            self.assertEqual(len(api.scheduled()), 201)
            self.assertIn('offset=200', request.call_args.args[1])
        for response in ({'list': [], 'total': 10}, {'list': [{'id': 'bad'}]}):
            with patch.object(api, 'request', return_value=response):
                with self.assertRaises(ValueError):
                    api.scheduled()

    def test_pause_no_api(self):
        with patch.object(m, 'ROOT', self.root), patch.object(m.sys, 'argv', ['mailer_v2.py', 'pause']), patch.object(m, 'API') as api:
            m.main()
            api.assert_not_called()
        self.assertFalse(m.config_at(self.root)['enabled'])


if __name__ == '__main__':
    unittest.main()
