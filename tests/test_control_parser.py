"""Parser integration is exercised only with a local stub, never production collection."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from control_jobs import log_text
from control_parser import Parser
import test_control_workflow as workflow

STUB = '''import argparse, csv, json, time, os, subprocess, sys
from pathlib import Path
p=argparse.ArgumentParser()
for k in ('industry','target','database','output','max-cards','max-pages'):p.add_argument('--'+k,required=True)
a=p.parse_args()
root=Path(a.output).parent
if (Path(__file__).parent/'sleep').exists():
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
    (root/'child.pid').write_text(str(child.pid))
    print('child ready',flush=True)
    time.sleep(60)
n=int(a.target)
with open(a.output,'w',encoding='utf-8-sig',newline='') as f:
    w=csv.writer(f,delimiter=';')
    w.writerow(['Организация','Официальный сайт','Телефон','Email','Исходная карточка'])
    for i in range(n):w.writerow(['=Clinic '+str(i),'https://site'+str(i)+'.test','+79991234567','','https://source.test/card'])
Path(a.database).write_text('stub database')
summary=dict(target_achieved=True,stop_reason='target_reached',cards_reviewed=n+2,accepted=n,exported_total=n)
if (Path(__file__).parent/'partial').exists():summary.update(target_achieved=False,stop_reason='safety_page_limit')
Path(a.output).with_suffix('.summary.json').write_text(json.dumps(summary))
print('completed',flush=True)
'''


class ParserTest(unittest.TestCase):
    setUp = workflow.WorkflowTest.setUp
    request = workflow.WorkflowTest.request
    post = workflow.WorkflowTest.post
    wait = workflow.WorkflowTest.wait

    def stub(self):
        source = self.root / 'cli'
        source.mkdir()
        (source / 'parser_run.py').write_text(STUB)
        (source / 'industries.py').write_text("INDUSTRIES={1:{'name':'Медицинские клиники','status':'ready'},3:{'name':'Строительство','status':'ready'}}")
        self.jobs.parser = Parser(source)
        mock = patch.object(self.jobs.parser, 'diagnostic', return_value=[])
        mock.start()
        self.addCleanup(mock.stop)
        return source

    def preview(self, target='2', name='Проверка'):
        head, body = self.post('/collect/preview', industry='1', target=target, name=name)
        self.assertIn('200', head)
        token = re.search(r'name="preview" value="([^"]+)"', body.decode()).group(1)
        return token, body.decode()

    def launch(self, target='2'):
        token, _ = self.preview(target)
        head, _ = self.post('/collect', preview=token)
        self.assertIn('303', head)
        return self.jobs.list()[0]['id']

    def test_contract_validation_and_unique_preview(self):
        self.stub()
        for target in ('0', '-1', '1001', '1.2', ' 1', '١', '', 'x; touch /tmp/x'):
            self.assertIn('400', self.post('/collect/preview', industry='1', target=target)[0])
        for industry in ('99', '../1', '1; echo bad', ''):
            self.assertIn('400', self.post('/collect/preview', industry=industry, target='1')[0])
        for name in ('x'*101, 'bad\nname'):
            self.assertIn('400', self.post('/collect/preview', industry='1', target='1', name=name)[0])
        token, page = self.preview(name='<test>')
        second, _ = self.preview()
        first = self.jobs.previews[token]
        self.assertNotEqual(first['id'], self.jobs.previews[second]['id'])
        self.assertFalse((self.jobs.state_dir / first['id']).exists())
        self.assertIn('&lt;test&gt;', page)
        command = self.jobs.parser.command(first['params'], self.jobs.state_dir / first['id'])
        self.assertIn('--database', command)
        self.assertIn('--output', command)
        self.assertNotIn('--region', command)
        self.assertNotIn('--city', command)
        self.assertNotIn('--resume', command)
        self.assertIn('MedAdvisor', page)
        self.assertIn('--target 2', page)
        self.assertEqual(self.jobs.list(), [])

    def test_confirmation_csrf_get_and_replay(self):
        self.stub()
        for path in ('/collect', '/collect/preview', '/jobs/preview'):
            self.request(path)
        self.assertEqual(self.jobs.list(), [])
        self.assertIn('400', self.post('/collect', preview='invented')[0])
        token, _ = self.preview()
        self.assertIn('403', self.request('/collect','POST',{'preview':token,'csrf':'wrong','confirm':'yes'})[0])
        self.assertIn('403', self.request('/collect','POST',{'preview':token,'csrf':'test-secret'})[0])
        self.assertIn('303', self.post('/collect', preview=token, target='999')[0])
        self.assertIn('400', self.post('/collect', preview=token)[0])
        row = self.wait(self.jobs.list()[0]['id'])
        self.assertEqual(json.loads(row['params'])['target'], 2)
        self.assertEqual(row['status'], 'succeeded', row)

    def test_stub_success_import_run_filter_export_and_html(self):
        self.stub()
        first = self.launch()
        row = self.wait(first)
        self.assertEqual(row['status'], 'succeeded', row)
        self.assertEqual(json.loads(row['result'])['usable'], 2)
        self.assertTrue((self.jobs.state_dir / first / 'parser.sqlite').is_file())
        second = self.launch('1')
        self.assertEqual(self.wait(second)['status'], 'succeeded')
        page = self.request('/organizations?run='+first)[1].decode()
        self.assertIn('Найдено: 2', page)
        self.assertIn('Скачать CSV запуска', page)
        self.assertIn('Найдено: 3', self.request('/organizations')[1].decode())
        self.assertIn('Найдено: 0', self.request('/mentions')[1].decode())
        leads, _ = self.jobs.collected()
        detail = self.request('/organizations/'+leads[0]['id'])[1].decode()
        self.assertIn('Проверка',detail)
        self.assertNotIn('<script>',detail)
        head, raw = self.request('/exports/'+first)
        self.assertIn('attachment',head)
        self.assertIn("'=Clinic",raw.decode('utf-8-sig'))
        self.assertIn("'+7999",raw.decode('utf-8-sig'))
        self.assertIn('404',self.request('/exports/../../etc/passwd')[0])
        (self.jobs.state_dir / first / 'parser.csv').unlink()
        (self.jobs.state_dir / first / 'parser.csv').symlink_to(self.root / 'cli/parser_run.py')
        self.assertIn('404',self.request('/exports/'+first)[0])

    def test_partial_and_invalid_summary_fail_even_exit_zero(self):
        source = self.stub()
        (source/'partial').touch()
        row = self.wait(self.launch())
        self.assertEqual(row['status'],'failed',row)
        self.assertIn('Цель не подтверждена',row['error'])
        self.assertEqual(json.loads(row['result'])['stop_reason'],'safety_page_limit')
        self.assertEqual(self.jobs.collected()[0],[])
        self.assertIn('404',self.request('/exports/'+row['id'])[0])
        (source/'parser_run.py').write_text(STUB.replace('json.dumps(summary)', 'json.dumps([])'))
        self.assertEqual(self.wait(self.launch())['status'],'failed')

    def test_dependency_block_and_expired_preview(self):
        self.stub()
        token,_ = self.preview()
        with patch.object(self.jobs.parser,'diagnostic',return_value=['Playwright отсутствует']):
            self.assertIn('400',self.post('/collect',preview=token)[0])
            page=self.post('/collect/preview',industry='1',target='1')[1].decode()
            self.assertIn('button disabled',page)
            self.assertNotIn('action="/collect"',page)
        self.jobs.previews[token]['time']-=901
        self.assertIn('400',self.post('/collect',preview=token)[0])
        self.assertEqual(self.jobs.list(),[])

    def test_cancel_and_timeout_kill_descendants_and_refresh(self):
        source=self.stub()
        (source/'sleep').touch()
        for timeout in (False, True):
            with patch('control_jobs.PARSER_TIMEOUT', 0.7 if timeout else 60):
                identifier=self.launch()
                pidfile=self.jobs.state_dir/identifier/'child.pid'
                deadline=time.monotonic()+4
                while not pidfile.exists() and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(pidfile.exists())
                head,_=self.request('/jobs/'+identifier)
                self.assertIn('Refresh: 5',head)
                pid=int(pidfile.read_text())
                if not timeout:self.jobs.cancel(identifier)
                row=self.wait(identifier)
                self.assertEqual(row['status'],'failed' if timeout else 'cancelled',row)
                # An exited orphan may briefly remain a zombie until init reaps it.
                stat=Path('/proc')/str(pid)/'stat'
                self.assertTrue(not stat.exists() or stat.read_text().split()[2]=='Z')

    def test_exited_cli_does_not_leave_child_holding_log_pipe(self):
        source=self.stub()
        (source/'sleep').touch()
        (source/'parser_run.py').write_text(STUB.replace('\n    time.sleep(60)\n', '\n    sys.exit(7)\n'))
        identifier=self.launch()
        row=self.wait(identifier)
        self.assertEqual(row['status'],'failed',row)
        self.assertIn('Код завершения: 7',row['error'])
        pid=int((self.jobs.state_dir/identifier/'child.pid').read_text())
        stat=Path('/proc')/str(pid)/'stat'
        self.assertTrue(not stat.exists() or stat.read_text().split()[2]=='Z')

    def test_abrupt_owner_exit_stops_parser_group(self):
        source = self.stub()
        (source / 'sleep').touch()
        state = self.root / 'crash-state'
        script = """import os, sys, time
from pathlib import Path
from control_jobs import Jobs
from control_parser import Parser
source, state = map(Path, sys.argv[1:])
jobs = Jobs(state, state, state)
jobs.parser = Parser(source)
jobs.parser.diagnostic = lambda industry: []
token, preview, command = jobs.preview_parser('1','1')
identifier = jobs.submit_parser(token)
marker = state / identifier / 'child.pid'
deadline = time.monotonic() + 5
while not marker.exists() and time.monotonic() < deadline: time.sleep(.01)
os._exit(0 if marker.exists() else 2)
"""
        owner = subprocess.Popen([sys.executable, '-B', '-c', script, str(source), str(state)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: owner.kill() if owner.poll() is None else None)
        self.assertEqual(owner.wait(timeout=8), 0)
        marker = next(state.glob('*/child.pid'))
        pid = int(marker.read_text())
        deadline = time.monotonic() + 3
        stat = Path('/proc') / str(pid) / 'stat'
        while stat.exists() and stat.read_text().split()[2] != 'Z' and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(not stat.exists() or stat.read_text().split()[2] == 'Z')
        from control_jobs import Jobs
        recovered = Jobs(state, state, state)
        try:
            self.assertEqual(recovered.list()[0]['status'], 'failed')
            self.assertEqual(recovered.collected()[0], [])
        finally:
            recovered.close()

    def test_html_is_balanced_and_get_never_submits(self):
        from html.parser import HTMLParser
        class Check(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack=[]
            def handle_starttag(self,tag,attrs):
                if tag not in {'meta','input','br','hr','link','img'}:
                    self.stack.append(tag)
            def handle_endtag(self,tag):
                if not self.stack or self.stack.pop()!=tag:
                    raise AssertionError('Unbalanced HTML: '+tag)
        self.stub()
        identifier=self.launch()
        self.assertEqual(self.wait(identifier)['status'],'succeeded')
        with patch.object(self.jobs,'submit_parser',side_effect=AssertionError('GET cannot collect')):
            for path in ('/','/jobs','/settings','/organizations','/reports','/jobs/'+identifier):
                head,body=self.request(path)
                self.assertIn('200',head)
                checker=Check()
                checker.feed(body.decode())
                self.assertEqual(checker.stack,[])

    def test_log_redaction_and_unavailable_source_diagnostic(self):
        value=log_text(b'API_KEY=hidden-value\nCookie: session=private\nhttps://user:password@site.test\n')
        self.assertNotIn('hidden-value',value)
        self.assertNotIn('session=private',value)
        self.assertNotIn('user:password',value)
        self.jobs.parser=Parser(self.root/'missing')
        self.assertTrue(self.jobs.parser.diagnostic())
        self.assertIn('200',self.request('/settings')[0])
        self.assertIn('200',self.request('/jobs')[0])


if __name__=='__main__':unittest.main()
