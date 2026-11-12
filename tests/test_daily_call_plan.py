from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from control_pages import Pages
from daily_call_plan import (DailyPlan, NAME, MOSCOW, build_plan, espo,
                             metadata_field, plan, plan_html, save_local)


class DailyCallPlanTest(unittest.TestCase):
    def test_saved_plan_is_not_recalculated_or_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            now = datetime(2026, 11, 9, 22, tzinfo=timezone.utc)
            build = Mock(return_value={'tasks': [{'task_id': str(i)} for i in range(10)]})
            first = save_local(directory, build, now)
            path = directory / 'daily-call-plan-2026-11-10.json'
            original = path.read_bytes()
            build.side_effect = AssertionError('must not recalculate')
            self.assertEqual(save_local(directory, build, now), first)
            build.assert_called_once_with()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(json.loads(original)['tasks']), 10)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(list(directory.glob('.daily-call-plan-*')))
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                save_local(directory, build, now)
            self.assertEqual(path.read_text(), '{broken')


NOW = datetime(2026, 11, 10, 12, tzinfo=MOSCOW)


class FakeAPI:
    def __init__(self, field=True):
        self.calls = []
        self.field = field
        self.tasks = {f't{i:02}': dict(id=f't{i:02}', name=NAME,
            description=f'LexRadar followup Email/e{i:02}\nТелефон: +74951234567',
            parentType='Account', parentId=f'a{i:02}', status='Not Started',
            dateEnd='2026-11-09 09:00:00', callResult='Не звонили') for i in range(85)}
        self.accounts = {f'a{i:02}': dict(id=f'a{i:02}', name=f'Организация {i}',
            phoneNumber='+74951234567') for i in range(85)}
        self.fail_put = False

    def request(self, method, route, payload=None):
        self.calls.append((method, route, deepcopy(payload)))
        if route == 'Metadata':
            assert method == 'GET'
            return {'entityDefs': {'Task': {'fields': {'callPlanDate': {'type': 'date'}} if self.field else {}}}}
        if route.startswith('Task?'):
            assert method == 'GET'
            offset = int(parse_qs(urlsplit(route).query)['offset'][0])
            return {'list': [{'id': tid} for tid in self.tasks] if offset == 0 else []}
        if method == 'PUT':
            assert payload.keys() == {'callPlanDate'}
            self.tasks[route.split('/')[1]].update(payload)
            if self.fail_put:
                raise espo.Hold('lost reply')
            return {}
        raise AssertionError((method, route))

    def get(self, kind, eid):
        self.calls.append(('GET', kind + '/' + eid, None))
        if kind == 'Task':
            return deepcopy(self.tasks[eid])
        if kind == 'Account':
            return deepcopy(self.accounts[eid])
        if kind == 'Email':
            return dict(id=eid, status='Sent', parentType='Account', parentId='a' + eid[1:],
                        dateSent='2026-11-01 09:00:00')
        raise AssertionError(kind)


class DailyPlanMVPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.api = FakeAPI()
        self.bindings = {f'e{i:02}': {'account_id': f'a{i:02}'} for i in range(85)}
        self.store = DailyPlan(self.directory, NOW)

    def build(self):
        with patch('daily_call_plan.campaign_bindings', return_value=(self.bindings, [])), \
                patch('daily_call_plan.evidence', return_value=({}, {})), \
                patch('daily_call_plan.local_phones', return_value=[]):
            return plan(self.api, [], self.directory, (), NOW)

    def proposed(self):
        return self.store.action('generate', self.store.day, build=self.build)

    def approved(self):
        report = self.proposed()
        return self.store.action('approve', self.store.day, self.store.token(report))

    def handler(self):
        handler = SimpleNamespace(server=SimpleNamespace(csrf_token='token'))
        handler.form = lambda *args, **kw: Pages.form(handler, *args, **kw)
        return handler

    def test_top_ten_get_only_and_skip_reasons(self):
        self.api.tasks['t00']['status'] = 'Completed'
        self.api.tasks['t01']['dateEnd'] = '2026-11-11 09:00:00'
        self.api.tasks['t02']['callResult'] = 'Отказ'
        self.api.accounts['a03']['phoneNumber'] = '+74959999999'
        self.api.accounts['a04']['phoneNumberData'] = [{'phoneNumber': '+74951234567', 'optOut': True}]
        self.api.accounts['a05'].pop('phoneNumber')
        self.api.tasks['t05']['description'] = 'LexRadar followup Email/e05\n'
        self.api.tasks['t06']['dateEnd'] = 'invalid'
        self.api.accounts['a07']['doNotCall'] = True
        self.api.tasks['t08']['parentId'] = 'a09'
        self.bindings['e08']['account_id'] = 'a09'
        original_get = self.api.get
        def get(kind, eid):
            row = original_get(kind, eid)
            if kind == 'Email' and eid == 'e08': row['parentId'] = 'a09'
            return row
        self.api.get = get
        result = self.build()
        self.assertEqual(result['selected'], 10)
        self.assertEqual(len({r['account_id'] for r in result['tasks']}), 10)
        self.assertEqual(result['tasks'][0]['name'], 'Организация 9')
        for reason in ('завершено', 'срок не наступил', 'результат звонка: Отказ',
                       'конфликт номеров', 'optOut / неверный номер', 'телефон отсутствует',
                       'ошибка данных: ValueError', 'optOut / Не беспокоить', 'дубликат Account'):
            self.assertIn(reason, result['skipped'])
        self.assertTrue(all(c[0] == 'GET' for c in self.api.calls))

    def test_approval_is_idempotent_and_day_bound(self):
        report = self.approved()
        original = self.store.path.read_bytes()
        for action in ('approve', 'generate', 'refresh'):
            self.store.action(action, self.store.day, self.store.token(report),
                              build=Mock(side_effect=AssertionError('reselection')))
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.path.with_suffix('.lock').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            self.store.action('approve', '2026-11-09', self.store.token(report))
        with self.assertRaises(ValueError):
            self.store.action('approve', self.store.day, 'stale')
        self.assertIsNone(DailyPlan(self.directory, datetime(2026, 11, 11, tzinfo=MOSCOW)).load())

    def test_view_reads_call_result_without_writes_or_reselection(self):
        report = self.approved()
        original = self.store.path.read_bytes()
        self.api.tasks[report['tasks'][0]['task_id']]['callResult'] = 'Не беспокоить'
        self.api.calls.clear()
        html = plan_html(self.handler(), self.directory, lambda: self.api, NOW)
        self.assertIn('Не звонить: результат звонка: Не беспокоить', html)
        self.assertIn('tel:+74951234567', html)
        self.assertIn('Передать план в CRM', html)
        self.assertNotIn('type="checkbox"', html)
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertTrue(all(c[0] == 'GET' for c in self.api.calls))
        self.assertFalse(any(c[1].startswith('Task?') for c in self.api.calls))

    def test_missing_field_local_plan_and_no_put(self):
        report = self.approved()
        self.api.field = False
        self.assertFalse(metadata_field(self.api))
        html = plan_html(self.handler(), self.directory, lambda: self.api, NOW)
        self.assertIn('план работает локально', html)
        self.assertNotIn('Передать план в CRM', html)
        with self.assertRaisesRegex(ValueError, 'callPlanDate'):
            self.store.action('transfer', self.store.day, self.store.token(report), api=self.api)
        self.assertTrue(all(c[0] == 'GET' for c in self.api.calls))

    def test_transfer_only_selected_dates_lost_reply_and_repeat(self):
        report = self.approved()
        original = deepcopy(self.api.tasks)
        self.api.fail_put = True
        self.api.calls.clear()
        self.store.action('transfer', self.store.day, self.store.token(report), api=self.api)
        puts = [c for c in self.api.calls if c[0] == 'PUT']
        self.assertEqual(len(puts), 10)
        for method, route, payload in puts:
            self.assertEqual(payload, {'callPlanDate': self.store.day})
            tid = route.split('/')[1]
            before = original[tid]
            self.assertEqual({k: v for k, v in self.api.tasks[tid].items() if k != 'callPlanDate'}, before)
        for tid in set(original) - {r['task_id'] for r in report['tasks']}:
            self.assertEqual(original[tid], self.api.tasks[tid])
        self.store.action('transfer', self.store.day, self.store.token(report), api=self.api)
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'PUT']), 10)
        self.assertTrue(all('Подтверждено' in r for r in self.store.load()['transfer'].values()))

    def test_transfer_unknown_never_retries_and_binding_error_is_per_task(self):
        report = self.approved()
        self.api.tasks['t01']['parentId'] = 'other'
        request = self.api.request
        def fail(method, route, payload=None):
            if method == 'PUT':
                self.api.calls.append((method, route, payload))
                raise espo.Hold('lost')
            return request(method, route, payload)
        self.api.request = fail
        self.store.action('transfer', self.store.day, self.store.token(report), api=self.api)
        first_count = len([c for c in self.api.calls if c[0] == 'PUT'])
        self.assertEqual(first_count, 9)
        self.store.action('transfer', self.store.day, self.store.token(report), api=self.api)
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'PUT']), first_count)
        self.assertIn('Ошибка чтения', self.store.load()['transfer']['t01'])
        self.assertIn('заблокирован', self.store.load()['transfer']['t00'])

    def test_build_missing_metadata_still_works(self):
        self.api.field = False
        with patch('daily_call_plan.phone_leads', return_value=([{'id': 'test'}], [])), \
                patch('daily_call_plan.plan', return_value={'tasks': []}):
            report = build_plan(self.api, self.directory, self.directory)
        self.assertFalse(report['crm_field'])
        self.assertEqual(self.api.calls[0][1], 'Metadata')

    def test_post_security_and_existing_page_integration(self):
        import io
        from email.message import Message
        from control_mvp import MVPHandler
        handler = object.__new__(MVPHandler)
        handler.server = SimpleNamespace(state_dir=self.directory, csrf_token='token', server_port=8080,
            followup=SimpleNamespace(journals=(), html=lambda h: '<p>Existing followup</p>'))
        handler.path = '/followup/plan'
        results = []
        handler.reject_post = lambda status, *args: results.append(status)
        handler.html = lambda body, status=200: results.append((status, body))
        handler.send_response = lambda status: results.append(status)
        handler.send_header = lambda *args: None
        handler.write_response = lambda *args: None
        def post(changes=None, host='127.0.0.1:8080', origin='http://127.0.0.1:8080'):
            fields = dict(csrf='token', day=self.store.day, action='generate')
            fields.update(changes or {})
            raw = urlencode(fields, doseq=True).encode()
            handler.headers = Message()
            for key, value in [('Host', host), ('Origin', origin), ('Content-Type', 'application/x-www-form-urlencoded'),
                               ('Content-Length', str(len(raw)))]:
                handler.headers[key] = value
            handler.rfile = io.BytesIO(raw)
            handler.do_POST()
        with patch('daily_call_plan.DailyPlan', return_value=self.store), \
                patch.object(self.store, 'action', return_value={}) as action, \
                patch('daily_call_plan.espo.API', side_effect=AssertionError('no API for generation until build')):
            for changes, host, origin in [({'csrf': 'wrong'}, '127.0.0.1:8080', 'http://127.0.0.1:8080'),
                ({'csrf': ['token', 'token']}, '127.0.0.1:8080', 'http://127.0.0.1:8080'),
                ({}, 'evil.example', 'http://127.0.0.1:8080'),
                ({}, '127.0.0.1:8080', 'https://evil.example')]:
                post(changes, host, origin)
                self.assertEqual(results[-1], 403)
            action.assert_not_called()
            post({'action': ['generate', 'transfer']})
            self.assertEqual(results[-1][0], 400)
            action.assert_not_called()
            post()
            self.assertEqual(results[-1], 303)
            action.assert_called_once()
        self.assertFalse(list(self.directory.iterdir()))
        handler.path = '/followup'
        handler.followup_page()
        self.assertIn('План звонков на сегодня', results[-1][1])
        self.assertIn('Existing followup', results[-1][1])
        self.assertIn('Сформировать план', results[-1][1])
        self.assertFalse(list(self.directory.iterdir()))

    def test_legacy_snapshot_read_and_transfer_without_changing_members(self):
        report = self.proposed()
        for row in report['tasks']:
            row.pop('account_id')
            row.pop('email_id')
        report.pop('state')
        self.store.path.write_text(json.dumps(report))
        original_tasks = deepcopy(report['tasks'])
        with patch('daily_call_plan.campaign_bindings', return_value=(self.bindings, [])):
            approved = self.store.action('approve', self.store.day, self.store.token(report))
            html = plan_html(self.handler(), self.directory, lambda: self.api, NOW)
            self.assertIn('Доступно по проверке', html)
            self.store.action('transfer', self.store.day, self.store.token(approved), api=self.api)
        self.assertEqual(self.store.load()['tasks'], original_tasks)
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'PUT']), 10)
