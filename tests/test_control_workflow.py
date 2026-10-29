"""End-to-end local UI and worker checks using temporary fixtures only."""
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from control_catalog import Catalog, discover, domain
from control_center import ControlHandler
from control_jobs import Jobs, LOG_LIMIT
from control_results import document_id
import test_control_center as fixtures


class WorkflowTest(unittest.TestCase):
    write = fixtures.OrganizationTest.write
    audit = fixtures.OrganizationTest.audit

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.results = self.root / 'results'
        self.parser = self.root / 'parser'
        self.results.mkdir()
        self.parser.mkdir()
        self.jobs = Jobs(self.root / 'state', self.parser, self.results)
        self.addCleanup(self.jobs.close)
        self.server = SimpleNamespace(results_dir=self.results, parser_dir=self.parser,
                                      state_dir=self.root / 'state', server_port=8765,
                                      csrf_token='test-secret', jobs=self.jobs,
                                      catalog=Catalog(self.results, self.parser))

    def request(self, path, method='GET', values=None, headers=None):
        body = urlencode(values or {}).encode()
        extra = {'Host': '127.0.0.1:8765', **(headers or {})}
        if method == 'POST':
            extra.setdefault('Content-Type', 'application/x-www-form-urlencoded')
            extra.setdefault('Content-Length', str(len(body)))
        raw = (f'{method} {path} HTTP/1.0\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in extra.items()) + '\r\n').encode() + body
        connection = fixtures.MemoryConnection('/')
        connection.request = io.BytesIO(raw)
        with patch.object(ControlHandler, 'log_message'):
            ControlHandler(connection, ('127.0.0.1', 0), self.server)
        head, response = bytes(connection.response).split(b'\r\n\r\n', 1)
        self.assertIn(f'Content-Length: {len(response)}'.encode(), head)
        return head.decode(), response

    def post(self, path, **values):
        return self.request(path, 'POST', dict(csrf='test-secret', confirm='yes', **values))

    def candidate(self, **changes):
        return {'company_name': 'Клиника', 'website': 'https://www.clinic.test/path',
                'phone': '123', 'email': '', 'source_card': 'https://source.test/card', **changes}

    def seed(self, candidates=None):
        (self.results / 'run').mkdir(exist_ok=True)
        (self.results / 'run/audit.json').write_text(json.dumps(self.audit()))
        (self.results / 'run/proposal.pdf').write_bytes(b'%PDF-fixture')
        (self.parser / 'crm-candidates.json').write_text(json.dumps(candidates or [self.candidate()]))
        return self.server.catalog.snapshot(force=True)

    def wait(self, identifier, status=None):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            row = self.jobs.get(identifier)
            if (row['status'] == status if status else row['status'] not in {'queued', 'running'}):
                return row
            time.sleep(0.01)
        self.fail(f'Timeout: {row}')

    def test_pages_and_real_counters_no_placeholder(self):
        data = self.seed()
        for path in ('/', '/organizations', '/mentions', '/reports', '/reports?kind=pdf',
                     '/settings', '/jobs', '/crm', '/reports/' + data['reports'][0]['id']):
            head, raw = self.request(path)
            self.assertIn('200 OK', head)
            self.assertIn('nosniff', head)
            page = raw.decode()
            self.assertNotIn('Скоро', page)
            self.assertIn('href="/jobs"', page)
        page = self.request('/')[1].decode()
        self.assertIn('href="/reports?kind=audit"', page)
        self.assertIn('<strong>1</strong>', page)
        self.assertFalse(list(self.results.glob('**/jobs.sqlite3')))

    def test_link_by_domain_only_when_unique_and_never_from_mentions(self):
        data = self.seed()
        lead = data['leads'][0]
        page = self.request('/organizations/' + lead['id'])[1].decode()
        self.assertIn('run/audit.json', page)
        self.assertIn('run/proposal.pdf', page)
        self.assertEqual(len(self.server.catalog.linked(lead)), 2)
        self.assertEqual(data['mentions'][0]['websites'], [])
        self.seed([self.candidate(), self.candidate(company_name='Другой филиал')])
        self.assertEqual(self.server.catalog.linked(lead), [])
        self.seed([self.candidate(website='https://sub.clinic.test')])
        self.assertEqual(self.server.catalog.linked(self.server.catalog.snapshot()['leads'][0]), [])
        for value in ('javascript:bad', 'https://user:pass@clinic.test', '', 'https://bad host'):
            self.assertEqual(domain(value), '')
        self.assertEqual(domain('https://WWW.CLINIC.test./a'), 'clinic.test')

    def test_pdf_json_download_and_containment_after_indexing(self):
        data = self.seed()
        pdf = next(r for r in data['reports'] if r['kind'] == 'pdf')
        audit = next(r for r in data['reports'] if r['kind'] == 'audit')
        for query, disposition in (('', 'inline'), ('?download=1', 'attachment')):
            head, raw = self.request('/files/' + pdf['id'] + query)
            self.assertIn('200 OK', head)
            self.assertIn('Content-Disposition: ' + disposition, head)
            self.assertEqual(raw, b'%PDF-fixture')
        self.assertEqual(json.loads(self.request('/files/' + audit['id'])[1])['schema_version'], 'primary-audit-0.1')
        content = self.request('/reports/' + audit['id'])[1].decode()
        self.assertIn('/files/' + pdf['id'], content)
        secret = self.root / 'secret.pdf'
        secret.write_bytes(b'%PDF-secret')
        (self.results / 'run/proposal.pdf').unlink()
        (self.results / 'run/proposal.pdf').symlink_to(secret)
        self.assertIn('404', self.request('/files/' + pdf['id'])[0])
        for path in ('/files/../../secret.pdf', '/files/%2e%2e%2fsecret.pdf', '/reports/nope', '/jobs/nope'):
            self.assertIn('404', self.request(path)[0])
        (self.results / 'run/audit.json').write_text('{}')
        self.assertIn('404', self.request('/files/' + audit['id'])[0])

    def test_cache_refresh_changes_add_remove_and_malformed(self):
        self.seed()
        with patch('control_catalog.load_leads', side_effect=AssertionError('Cache must be reused')):
            self.request('/organizations')
            self.request('/')
        (self.parser / 'crm-candidates.json').write_text('[]')
        self.assertEqual(len(self.server.catalog.snapshot()['leads']), 1)
        self.assertIn('303', self.post('/refresh')[0])
        self.assertEqual(self.server.catalog.snapshot()['leads'], [])
        (self.results / 'run/audit.json').write_text('{')
        (self.results / 'run/proposal.pdf').unlink()
        data = self.server.catalog.snapshot(force=True)
        self.assertEqual(data['reports'], [])
        self.assertGreater(data['warnings'], 0)
        (self.results / 'run/proposal.pdf').write_bytes(b'bad')
        self.assertEqual(self.server.catalog.snapshot(force=True)['reports'], [])

    def test_report_filters_pagination_dates_and_escaping(self):
        for i in range(23):
            path = self.results / f'run{i:02}'
            path.mkdir()
            (path / 'proposal.pdf').write_bytes(b'%PDF-fixture')
        self.server.catalog.snapshot(force=True)
        self.assertIn('Найдено: 23'.encode(), self.request('/reports?kind=pdf')[1])
        self.assertIn('Далее'.encode(), self.request('/reports?kind=pdf')[1])
        self.assertIn('Страница 2'.encode(), self.request('/reports?kind=pdf&page=2')[1])
        self.assertIn('Найдено: 1'.encode(), self.request('/reports?q=run21')[1])
        self.assertIn('Найдено: 0'.encode(), self.request('/reports?since=9999-01-01')[1])
        for path in ('/reports?page=bad&since=bad&until=bad', '/reports?q=%3Cscript%3E', '/organizations?q=%3Cscript%3E'):
            head, raw = self.request(path)
            self.assertIn('200 OK', head)
            self.assertNotIn(b'<script>', raw)

    def test_post_csrf_origin_host_limits_and_no_get_mutations(self):
        for values in ({}, {'csrf': 'wrong', 'confirm': 'yes'}, {'csrf': 'неверный', 'confirm': 'yes'}, {'csrf': 'test-secret'}):
            self.assertTrue(any(code in self.request('/jobs', 'POST', values)[0] for code in ('400', '403')))
        for headers in ({'Origin': 'https://evil.test'}, {'Host': 'evil.test:8765'},
                        {'Host': '127.0.0.1:80'}, {'Host': '127.0.0.1:8765@evil.test'},
                        {'Content-Length': '99999'}, {'Transfer-Encoding': 'chunked'},
                        {'Content-Type': 'application/json'}):
            head, _ = self.request('/jobs', 'POST', dict(csrf='test-secret', confirm='yes', kind='inspect-import'), headers)
            self.assertTrue('403' in head or '400' in head, head)
        self.assertEqual(self.jobs.list(), [])
        for path in ('/refresh', '/jobs/preview', '/jobs/x/cancel'):
            self.request(path)
        self.assertEqual(self.jobs.list(), [])
        self.assertIn('403', self.request('/', headers={'Host': 'evil.test:8765'})[0])
        self.assertIn('400', self.post('/jobs', kind='arbitrary; rm -rf /')[0])
        self.assertIn('404', self.post('/unknown')[0])

    def test_preview_submit_actual_worker_and_restart(self):
        self.seed()
        page = self.post('/jobs/preview', kind='inspect-import')[1].decode()
        self.assertIn('Предпросмотр локальной команды', page)
        self.assertIn('control_worker.py', page)
        self.assertEqual(self.jobs.list(), [])
        head, _ = self.post('/jobs', kind='inspect-import')
        self.assertIn('303', head)
        identifier = self.jobs.list()[0]['id']
        row = self.wait(identifier)
        self.assertEqual(row['status'], 'succeeded', row)
        self.assertIn('"usable_contacts": 1', row['output'])
        page = self.request('/jobs/' + identifier)[1].decode()
        self.assertIn('succeeded', page)
        self.assertIn('stdout', page)
        self.assertIsNotNone(row['started'])
        self.assertIsNotNone(row['ended'])
        self.assertEqual(list((self.root / 'state' / identifier).iterdir()), [])
        with patch.dict(os.environ, {'ESPO_API_KEY': 'never-forward-this'}):
            with patch.object(self.jobs, 'command', return_value=[sys.executable, '-c', 'import os; print(os.getenv("ESPO_API_KEY", "absent"))']):
                second = self.jobs.submit('inspect-import')
                self.assertEqual(self.wait(second)['output'].strip(), '[stdout] absent')

    def test_single_worker_cancel_queued_and_running(self):
        command = [sys.executable, '-c', 'import time; print("running", flush=True); time.sleep(30)']
        with patch.object(self.jobs, 'command', return_value=command):
            first = self.jobs.submit('inspect-import')
            self.wait(first, 'running')
            second = self.jobs.submit('offline-results')
            self.assertEqual(self.jobs.get(second)['status'], 'queued')
            self.assertIn('303', self.post('/jobs/' + second + '/cancel')[0])
            self.assertEqual(self.jobs.get(second)['status'], 'cancelled')
            self.assertIn('303', self.post('/jobs/' + first + '/cancel')[0])
            self.assertEqual(self.wait(first)['status'], 'cancelled')
            self.assertEqual(len([r for r in self.jobs.list() if r['status'] == 'running']), 0)
        self.assertIn('400', self.post('/jobs/missing/cancel')[0])

    def test_worker_failure_timeout_and_log_limit(self):
        commands = [([sys.executable, '-c', 'import sys; print("failure", file=sys.stderr); sys.exit(7)'], 'Код завершения: 7'),
                    ([sys.executable, '-c', 'import time; time.sleep(30)'], 'Превышен лимит времени'),
                    ([str(self.root / 'absent')], 'FileNotFoundError')]
        for command, reason in commands:
            with patch.object(self.jobs, 'command', return_value=command), patch('control_jobs.TIMEOUT', 0.2):
                identifier = self.jobs.submit('inspect-import')
                row = self.wait(identifier)
                self.assertEqual(row['status'], 'failed', row)
                self.assertIn(reason, row['error'])
        command = [sys.executable, '-c', 'import sys; sys.stdout.write("x"*200000); sys.stderr.write("y"*200000)']
        with patch.object(self.jobs, 'command', return_value=command):
            row = self.wait(self.jobs.submit('inspect-import'))
            self.assertEqual(row['status'], 'succeeded')
            self.assertLessEqual(len(row['output'].encode()), LOG_LIMIT)
            self.assertIn('64 КиБ', row['error'])
        self.assertNotIn('output', self.jobs.list()[0])

    def test_crm_preview_actual_schema_no_secrets_and_no_network(self):
        row = dict(id='saved-id', status='PENDING_REVIEW', account_id='account-fixture',
                   identity={'company_name': '<script>clinic</script>'},
                   materials=[{'document_id': 'doc-fixture'}], api_key='secret-must-not-render')
        (self.results / 'ten-crm-result.json').write_text(json.dumps([row]))
        self.server.catalog.snapshot(force=True)
        with patch('socket.create_connection', side_effect=AssertionError('Network forbidden')):
            page = self.request('/crm')[1].decode()
        for value in ('saved-id', 'PENDING_REVIEW', 'doc-fixture', 'account-fixture', '&lt;script&gt;'):
            self.assertIn(value, page)
        self.assertNotIn('secret-must-not-render', page)
        self.assertNotIn('<script>', page)
        self.assertNotIn('method="post"', page)

    def test_thousands_of_leads_sort_search_and_cached_pages(self):
        candidates = [self.candidate(company_name=f'Clinic {i:04}', website=f'https://site{i}.test')
                      for i in range(3000)]
        (self.parser / 'crm-candidates.json').write_text(json.dumps(candidates))
        data = self.server.catalog.snapshot(force=True)
        self.assertEqual(len(data['leads']), 3000)
        with patch('control_catalog.load_leads', side_effect=AssertionError('Repeated import')):
            page = self.request('/organizations?page=150')[1].decode()
            self.assertIn('Clinic 2999', page)
            self.assertIn('Найдено: 3000', page)
            page = self.request('/organizations?sort=name-desc')[1].decode()
            self.assertLess(page.index('Clinic 2999'), page.index('Clinic 2998'))
            self.assertIn('Найдено: 1', self.request('/organizations?q=site2121.test')[1].decode())

    def test_automatic_metadata_refresh_and_unchanged_content_reuse(self):
        self.seed()
        self.server.catalog.checked = 0
        with patch('control_catalog.load_leads', side_effect=AssertionError('Unchanged metadata')):
            self.server.catalog.snapshot()
        (self.parser / 'crm-candidates.json').write_text('[]')
        self.server.catalog.checked = 0
        self.assertEqual(self.server.catalog.snapshot()['leads'], [])
        (self.parser / 'crm-candidates.json').unlink()
        self.server.catalog.checked = 0
        self.assertEqual(self.server.catalog.snapshot()['leads'], [])

    def test_malformed_target_types_and_non_pdf_stay_safe(self):
        (self.results / 'run').mkdir()
        for value in (None, [], {}, 42):
            data = self.audit()
            data['target_url'] = value
            (self.results / 'run/audit.json').write_text(json.dumps(data))
            self.server.catalog.snapshot(force=True)
            self.assertIn('200 OK', self.request('/reports')[0])
            self.assertEqual(self.server.catalog.snapshot()['reports'][0]['target'], '')
        (self.results / 'run/proposal.pdf').write_bytes(b'not pdf')
        self.server.catalog.snapshot(force=True)
        self.assertIn('404', self.request('/files/' + document_id('run/proposal.pdf'))[0])

    def test_report_organization_filter_and_audit_dates(self):
        data = self.seed()
        report = next(r for r in data['reports'] if r['kind'] == 'audit')
        audit = self.audit()
        audit.update(source_started_at='2026-10-01T10:00:00Z', assessed_at='2026-10-02T11:00:00Z',
                     stages=[{'stage_id': 'one', 'title': 'Stage fixture', 'status': 'not_checked'}])
        (self.results / 'run/audit.json').write_text(json.dumps(audit))
        self.server.catalog.snapshot(force=True)
        page = self.request('/reports?q=' + urlencode({'q': 'Клиника'}).split('=', 1)[1])[1].decode()
        self.assertIn('Найдено: 2', page)
        page = self.request('/reports/' + report['id'])[1].decode()
        for value in ('2026-10-01T10:00:00Z', '2026-10-02T11:00:00Z', 'Stage fixture', 'not_checked'):
            self.assertIn(value, page)

    def test_invalid_utf8_worker_output_is_bounded(self):
        command = [sys.executable, '-c', 'import sys; sys.stdout.buffer.write(bytes([255])*200000)']
        with patch.object(self.jobs, 'command', return_value=command):
            row = self.wait(self.jobs.submit('inspect-import'))
        self.assertEqual(row['status'], 'succeeded')
        self.assertLessEqual(len(row['output'].encode()), LOG_LIMIT)
        self.assertIn('64 КиБ', row['error'])

    def test_bounded_discovery_and_symlink_root(self):
        for i in range(520):
            (self.results / f'run{i:03}').mkdir()
        _, warnings = discover(self.results)
        self.assertGreater(warnings, 0)
        linked = self.root / 'linked'
        linked.symlink_to(self.results, target_is_directory=True)
        self.assertEqual(discover(linked), ([], 1))


class JobRecoveryTest(unittest.TestCase):
    def test_restart_does_not_resume_and_prevents_multiple_owners(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = Jobs(root / 'state', root / 'parser', root / 'results')
            with self.assertRaises(ValueError):
                Jobs(root / 'state', root, root)
            jobs.close()
            db = sqlite3.connect(root / 'state/jobs.sqlite3')
            for identifier, status in (('a', 'running'), ('b', 'queued')):
                db.execute('INSERT INTO jobs(id,kind,status,created) VALUES(?,?,?,?)',
                           (identifier, 'inspect-import', status, '2026-10-29'))
            db.commit()
            db.close()
            jobs = Jobs(root / 'state', root, root)
            try:
                self.assertEqual(jobs.get('a')['status'], 'failed')
                self.assertIn('перезапуском', jobs.get('a')['error'])
                self.assertEqual(jobs.get('b')['status'], 'cancelled')
                self.assertIsNone(jobs.get('a')['started'])
            finally:
                jobs.close()

    def test_state_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'linked').symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                Jobs(root / 'linked/state', root, root)
            (root / 'state').mkdir()
            (root / 'state/jobs.sqlite3').symlink_to(root / 'outside')
            with self.assertRaises(ValueError):
                Jobs(root / 'state', root, root)


if __name__ == '__main__':
    unittest.main()
