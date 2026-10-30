"""Analysis integration uses a subprocess stub only; no DNS, crawling or paid calls."""
import json
import re
import sqlite3
import sys
import time
import unittest
from unittest.mock import patch

from control_analysis import target
from control_jobs import Jobs
import test_control_workflow as workflow

STUB = '''import sys,time,json
from pathlib import Path
url=sys.argv[1]
if 'slow' in url:
    print('waiting',flush=True)
    time.sleep(60)
if 'fail' in url:sys.exit(2)
p=Path('analysis');p.mkdir()
(p/'manifest.json').write_text(json.dumps({'summary':{},'rows':[{'status':'SUCCESS','stages':['evidence_packet:SUCCESS']}]}))
(p/'report.md').write_text('Real stub report')
(p/'results.csv').write_text('status\\nSUCCESS\\n')
print('done',flush=True)
'''


class AnalysisTest(unittest.TestCase):
    setUp = workflow.WorkflowTest.setUp
    request = workflow.WorkflowTest.request
    post = workflow.WorkflowTest.post
    wait = workflow.WorkflowTest.wait
    seed = workflow.WorkflowTest.seed
    candidate = workflow.WorkflowTest.candidate
    audit = workflow.WorkflowTest.audit

    def stub(self):
        script = self.root / 'analyzer.py'
        script.write_text(STUB)
        mock = patch.object(self.jobs, 'analysis_command', side_effect=lambda p, d: [sys.executable, str(script), p['url']])
        command = mock.start()
        self.addCleanup(mock.stop)
        return command

    def records(self, count=3):
        return [dict(id=str(i), name=f'Clinic {i:03}', websites=[f'https://clinic{i}.ru/path']) for i in range(count)]

    def preview(self, **values):
        head, body = self.request('/analysis/preview', 'POST', dict(csrf='test-secret', **values))
        self.assertIn('200 OK', head)
        match = re.search(rb'name="preview" value="([^"]+)"', body)
        return match[1].decode() if match else None, body.decode()

    def launch(self, records, identifiers):
        token, _ = self.jobs.preview_analysis(records, identifiers)
        return self.jobs.submit_analysis(token)

    def test_safe_urls_normalization_no_dns(self):
        self.assertEqual(target('https://WWW.Example.org/some?q=1#x'), ('https://www.example.org/', 'example.org'))
        for url in ('file:///etc/passwd', 'http://localhost', 'http://127.0.0.1', 'http://169.254.169.254/',
                    'http://10.0.0.1', 'http://[::1]/', 'https://user:pass@example.org', 'https://example.org:9999',
                    'https://a.internal', 'https://a.local', 'https://2130706433', 'https://example.org/\n'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                target(url)

    def test_preview_no_side_effects_and_single_real_worker(self):
        command = self.stub()
        data = self.seed([self.candidate(website='https://clinic.ru')])
        lead = data['leads'][0]['id']
        token, body = self.preview(lead=lead)
        self.assertIn('clinic.ru', body)
        self.assertEqual(self.jobs.list(), [])
        command.assert_not_called()
        self.assertEqual(len(list(self.jobs.state_dir.iterdir())), 2)
        head, _ = self.post('/analysis', preview=token)
        self.assertIn('303', head)
        parent = next(r for r in self.jobs.list() if r['kind'] == 'analysis-batch')['id']
        self.assertEqual(self.wait(parent)['status'], 'succeeded')
        child = self.jobs.analysis_children(parent)[0]
        self.assertEqual(child['status'], 'succeeded')
        self.assertIn('manifest.json', self.request('/jobs/' + child['id'])[1].decode())
        self.assertIn('200', self.request('/analysis-files/' + child['id'] + '/manifest.json')[0])
        self.assertIn('404', self.request('/analysis-files/' + child['id'] + '/../../etc/passwd')[0])
        self.assertIn('Успешно', self.request('/organizations')[1].decode())
        with self.assertRaises(ValueError):
            self.jobs.submit_analysis(token)

    def test_selected_dedup_invalid_and_stable_next(self):
        records = self.records(60)
        records.append(dict(id='duplicate', name='Duplicate', websites=['http://www.clinic0.ru/']))
        records.append(dict(id='missing', name='Missing', websites=[]))
        _, preview = self.jobs.preview_analysis(list(reversed(records)), ['0', 'duplicate', 'missing'])
        self.assertEqual(len(preview['params']['sites']), 1)
        self.assertEqual(len(preview['params']['skipped']), 2)
        _, preview = self.jobs.preview_analysis(list(reversed(records)), count=50, completed={'clinic0.ru'})
        self.assertEqual(len(preview['params']['sites']), 50)
        self.assertEqual(preview['params']['sites'][0]['domain'], 'clinic1.ru')
        self.assertEqual(preview['params']['sites'][-1]['domain'], 'clinic50.ru')
        for count in (0, 1, 201):
            with self.assertRaises(ValueError):
                self.jobs.preview_analysis(records, count=count)

    def test_individual_failure_does_not_stop_batch(self):
        self.stub()
        records = self.records()
        records[1]['websites'] = ['https://fail.ru']
        parent = self.launch(records, ['0', '1', '2'])
        self.assertEqual(self.wait(parent)['status'], 'failed')
        self.assertEqual([r['status'] for r in self.jobs.analysis_children(parent)], ['succeeded', 'failed', 'succeeded'])
        self.assertIn('3/3', self.request('/jobs/' + parent)[1].decode())

    def test_duplicate_concurrent_previews_and_repeat(self):
        self.stub()
        records = self.records(1)
        first, _ = self.jobs.preview_analysis(records, ['0'])
        second, _ = self.jobs.preview_analysis(records, ['0'])
        # Freeze worker selection to inspect queued domain reservations.
        with self.jobs.lock:
            parent = self.jobs.submit_analysis(first)
            with self.assertRaises(ValueError):
                self.jobs.submit_analysis(second)
        self.wait(parent)
        _, preview = self.jobs.preview_analysis(records, count=10)
        self.assertEqual(preview['params']['sites'], [])
        token, preview = self.jobs.preview_analysis(records, ['0'])
        self.assertTrue(preview['params']['sites'][0]['repeat'])
        self.assertEqual(self.wait(self.jobs.submit_analysis(token))['status'], 'succeeded')

    def test_cancel_running_and_remaining(self):
        self.stub()
        records = self.records()
        records[0]['websites'] = ['https://slow.ru']
        parent = self.launch(records, ['0', '1', '2'])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            children = self.jobs.analysis_children(parent)
            if children[0]['status'] == 'running':
                break
            time.sleep(.01)
        self.jobs.cancel(parent)
        self.assertEqual(self.wait(parent)['status'], 'cancelled')
        self.assertEqual([r['status'] for r in self.jobs.analysis_children(parent)], ['cancelled'] * 3)

    def test_cancel_individual_pending(self):
        self.stub()
        records = self.records(2)
        records[0]['websites'] = ['https://slow.ru']
        parent = self.launch(records, ['0', '1'])
        children = self.jobs.analysis_children(parent)
        self.jobs.cancel(children[1]['id'])
        self.jobs.cancel(children[0]['id'])
        self.wait(parent)
        self.assertEqual([r['status'] for r in self.jobs.analysis_children(parent)], ['cancelled', 'cancelled'])

    def test_timeout_continues(self):
        self.stub()
        records = self.records(2)
        records[0]['websites'] = ['https://slow.ru']
        with patch('control_jobs.ANALYSIS_TIMEOUT', .1):
            parent = self.launch(records, ['0', '1'])
            self.wait(parent)
        children = self.jobs.analysis_children(parent)
        self.assertEqual([r['status'] for r in children], ['failed', 'succeeded'])
        self.assertIn('лимит времени', children[0]['error'])

    def test_restart_marks_all_interrupted_without_replay(self):
        # A separate stopped worker models a crash snapshot, never runs a command.
        state = self.root / 'restart'
        jobs = Jobs(state, self.parser, self.results)
        jobs.close()
        db = sqlite3.connect(state / 'jobs.sqlite3')
        for i, status in enumerate(('running', 'queued', 'pending')):
            db.execute('INSERT INTO jobs(id,kind,status) VALUES(?,?,?)', (str(i), 'analysis-site', status))
        db.commit()
        db.close()
        with patch('control_jobs.subprocess.Popen') as process:
            jobs = Jobs(state, self.parser, self.results)
            try:
                self.assertEqual({r['status'] for r in jobs.list()}, {'failed', 'cancelled'})
                process.assert_not_called()
            finally:
                jobs.close()

    def test_csrf_origin_confirm_and_unknown_ids(self):
        self.seed([self.candidate(website='https://clinic.ru')])
        for values, headers in ((dict(lead='0'), {}), (dict(csrf='wrong', lead='0'), {}),
                                (dict(csrf='test-secret', lead='0'), {'Origin': 'https://evil.ru'})):
            self.assertIn('403', self.request('/analysis/preview', 'POST', values, headers)[0])
        self.assertIn('400', self.request('/analysis/preview', 'POST', dict(csrf='test-secret', lead='unknown'))[0])
        self.assertIn('403', self.request('/analysis', 'POST', dict(csrf='test-secret', preview='x'))[0])
        self.assertEqual(self.jobs.list(), [])

    def test_page_selection_filters_and_next_ignores_page(self):
        data = self.seed([self.candidate(company_name=f'Clinic {i:03}', website=f'https://clinic{i}.ru') for i in range(35)])
        page = self.request('/organizations?q=Clinic&page=2&sort=name-desc')[1].decode()
        self.assertIn('analysis-selection', page)
        selected = re.search(r'name="all" value="([^"]+)"', page)[1].split(',')
        self.assertEqual(len(selected), 15)
        token, body = self.preview(all=','.join(selected))
        self.assertEqual(len(self.jobs.previews[token]['params']['sites']), 15)
        token, body = self.preview(count='20')
        self.assertEqual(len(self.jobs.previews[token]['params']['sites']), 20)
        self.assertIn('Clinic 000', body)
        single = data['leads'][0]['id']
        token, body = self.preview(single=single, lead=data['leads'][1]['id'])
        self.assertEqual(len(self.jobs.previews[token]['params']['sites']), 1)
        self.assertEqual(self.jobs.list(), [])

    def test_command_fixed_engine_and_no_synthetic_mode(self):
        directory = self.root / 'command'
        directory.mkdir()
        params = dict(url='https://clinic.ru', domain='clinic.ru')
        command = self.jobs.analysis_command(params, directory)
        self.assertTrue(command[2].endswith('control_analysis_runner.py'))
        self.assertEqual(command[-1], str(directory / 'analysis'))
        self.assertIn('https://clinic.ru/', (directory / 'input.csv').read_text())

    def test_runner_calls_existing_prepare_with_safe_flags(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import control_analysis_runner as runner
        run = Mock(return_value={'rows': [dict(status='SKIPPED', stages=['evidence_packet:SUCCESS'])]})
        original = sys.path[:]
        try:
            with patch.dict(sys.modules, {'lexradar.mvp_batch': SimpleNamespace(run=run)}), patch.object(sys, 'argv', ['runner', '/tmp/input.csv', '/tmp/output']):
                self.assertEqual(runner.main(), 0)
            args, kwargs = run.call_args
            self.assertEqual(str(args[0]), '/tmp/input.csv')
            self.assertEqual(kwargs, dict(mode='prepare', max_sites=1, max_ai_calls=0,
                                         allow_site_collection=True, ignore_robots=False))
            for row in (dict(status='SKIPPED', stages=[]), dict(status='ERROR', stages=['evidence_packet:SUCCESS'])):
                run.return_value = {'rows': [row]}
                with patch.dict(sys.modules, {'lexradar.mvp_batch': SimpleNamespace(run=run)}), patch.object(sys, 'argv', ['runner', '/tmp/input.csv', '/tmp/output']):
                    self.assertEqual(runner.main(), 1)
        finally:
            sys.path[:] = original

    def test_expired_preview_and_wrong_token_kind(self):
        token, _ = self.jobs.preview_analysis(self.records(1), ['0'])
        with self.assertRaises(ValueError):
            self.jobs.submit_parser(token)
        self.jobs.previews[token]['time'] -= 901
        with self.assertRaises(ValueError):
            self.jobs.submit_analysis(token)
        self.assertEqual(self.jobs.list(), [])

    def test_success_before_failed_repeat_stays_processed(self):
        self.stub()
        records = self.records(1)
        parent = self.launch(records, ['0'])
        self.wait(parent)
        records[0]['websites'] = ['http://www.clinic0.ru']
        token, _ = self.jobs.preview_analysis(records, ['0'])
        with patch.object(self.jobs, 'analysis_command', return_value=[sys.executable, '-c', 'raise SystemExit(1)']):
            self.wait(self.jobs.submit_analysis(token))
        _, preview = self.jobs.preview_analysis(records, count=10)
        self.assertEqual(preview['params']['sites'], [])

    def test_installed_engine_blocks_private_dns_and_rebinding_without_sockets(self):
        import subprocess
        from control_analysis import ENGINE, PYTHON
        if not PYTHON.is_file() or not (ENGINE / 'src').is_dir():
            self.skipTest('Installed LexRadar unavailable')
        code = '''import sys, socket
from unittest.mock import patch, MagicMock
sys.path.insert(0, sys.argv[1])
from lexradar.collector.network import AddressPolicy, CollectionError, Fetcher, Limits, origin
for ip in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1'):
    with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', (ip, 80))]), patch('socket.create_connection') as connect:
        try: AddressPolicy().resolve('clinic.ru', 80)
        except CollectionError: pass
        else: raise AssertionError(ip)
        connect.assert_not_called()
answers=iter(['8.8.8.8','127.0.0.1'])
connection=MagicMock()
connection.getresponse.return_value.status=302
connection.getresponse.return_value.getheader.return_value='/next'
with patch('socket.getaddrinfo', side_effect=lambda *a,**k: [(2,1,6,'',(next(answers),80))]), patch('lexradar.collector.network.PinnedHTTP', return_value=connection) as factory:
    try: Fetcher(Limits()).get('http://clinic.ru/', origin('http://clinic.ru/'))
    except CollectionError: pass
    else: raise AssertionError('Rebinding accepted')
    assert factory.call_count == 1
'''
        checked = subprocess.run([str(PYTHON), '-B', '-c', code, str(ENGINE / 'src')],
                                 capture_output=True, timeout=15, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        self.assertEqual(checked.returncode, 0, checked.stderr.decode())
