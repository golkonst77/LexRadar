"""Fixed, unpaid real LexRadar screening; no CRM or synthetic analyzer."""
import csv
import ipaddress
import json
import re
import secrets
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ENGINE = Path('/home/golkonst/src/LexRadar-compact')
PYTHON = Path('/home/golkonst/src/LexRadar/.venv/bin/python')
ANALYSIS_TIMEOUT = 180
ARTIFACTS = {'manifest.json': 'manifest.json', 'report.md': 'report.md', 'results.csv': 'results.csv',
             'review.json': 'sites/row-000002/review.json',
             'proposal.pdf': 'sites/row-000002/proposal.pdf'}


def target(value):
    if not isinstance(value, str):
        raise ValueError('Нет допустимого публичного официального сайта')
    try:
        u = urlsplit(value)
        host = (u.hostname or '').rstrip('.').encode('idna').decode().lower()
        if (u.scheme not in {'http', 'https'} or u.username is not None or u.password is not None
                or u.port not in (None, 80 if u.scheme == 'http' else 443)
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
                or not re.fullmatch(r'[a-z0-9.-]+', host) or '..' in host):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if '.' not in host or not host.rsplit('.', 1)[1].isalpha() or host.endswith(('.localhost', '.local', '.internal', '.test', '.example')):
                raise ValueError()
        else:
            if not address.is_global or address.is_multicast or address.is_reserved:
                raise ValueError()
        return u.scheme + '://' + host + '/', host.removeprefix('www.')
    except (ValueError, UnicodeError):
        raise ValueError('Нет допустимого публичного официального сайта') from None


class AnalysisJobs:
    def analysis_history(self):
        with self.lock:
            rows = self.db.execute("SELECT * FROM jobs WHERE kind='analysis-site' ORDER BY rowid DESC").fetchall()
            result = {}
            for row in rows:
                params = json.loads(row['params'])
                try:
                    host = target(params.get('url', ''))[1]
                    if host == params.get('domain'):
                        result[(params.get('lead_id'), host, row['id'])] = dict(row)
                except ValueError:
                    continue
            return result

    def preview_analysis(self, records, identifiers=None, count=None, completed=()):
        if count is not None and count not in (10, 20, 50, 100, 200):
            raise ValueError('Недопустимый размер пакета')
        if identifiers is not None and (not identifiers or len(identifiers) > 200):
            raise ValueError('Выберите от 1 до 200 организаций')
        known = {r['id'] for r in records}
        if identifiers is not None and not set(identifiers) <= known:
            raise ValueError('Каталог изменился; выберите организации заново')
        with self.lock:
            active = {json.loads(r['params'])['domain'] for r in self.db.execute(
                "SELECT params FROM jobs WHERE kind='analysis-site' AND status IN ('pending','queued','running')")}
            done = set(completed) | {json.loads(r[0])['domain'] for r in self.db.execute(
                "SELECT params FROM jobs WHERE kind='analysis-site' AND status='succeeded'")}
            sites, skipped, seen = [], [], set()
            for record in sorted(records, key=lambda r: (r['name'].casefold(), r['id'])):
                if identifiers is not None and record['id'] not in identifiers:
                    continue
                try:
                    urls = {target(v) for v in record['websites']}
                    if len({d for _, d in urls}) != 1:
                        raise ValueError('Сайт отсутствует или несколько разных доменов')
                    url, host = sorted(urls)[0]
                    if host in seen:
                        raise ValueError('Повтор домена в выбранных организациях')
                    seen.add(host)
                    if host in active:
                        raise ValueError('Домен уже в очереди или выполняется')
                    if count is not None and host in done:
                        raise ValueError('Анализ уже завершён')
                    sites.append(dict(name=record['name'], lead_id=record['id'], url=url, domain=host,
                                      repeat=host in done))
                except ValueError as exc:
                    skipped.append(record['name'] + ': ' + str(exc))
                if count is not None and len(sites) >= count:
                    break
            self.previews = {k: v for k, v in self.previews.items() if time.monotonic() - v['time'] < 900}
            if len(self.previews) >= 100:
                raise ValueError('Слишком много предпросмотров')
            token = secrets.token_urlsafe(32)
            preview = dict(id=uuid.uuid4().hex, time=time.monotonic(), analysis=True,
                           params=dict(sites=sites, skipped=skipped))
            self.previews[token] = preview
            return token, preview

    def submit_analysis(self, token):
        from control_jobs import now
        with self.lock:
            preview = self.previews.get(token)
            if not preview or not preview.get('analysis') or time.monotonic() - preview['time'] >= 900:
                raise ValueError('Предпросмотр истёк или уже использован')
            sites = preview['params']['sites']
            if not sites or len(sites) > 200:
                raise ValueError('Нет подходящих сайтов')
            if not PYTHON.is_file() or not (ENGINE / 'src/lexradar/mvp_batch.py').is_file():
                raise ValueError('LexRadar или его Python недоступен')
            active = {json.loads(r[0])['domain'] for r in self.db.execute(
                "SELECT params FROM jobs WHERE kind='analysis-site' AND status IN ('pending','queued','running')")}
            if any(s['domain'] in active for s in sites):
                raise ValueError('Домен уже поставлен в очередь; обновите предпросмотр')
            if self.stopping.is_set() or self.db.execute('SELECT count(*) FROM jobs').fetchone()[0] + len(sites) + 1 > 1000:
                raise ValueError('Сервер остановлен или журнал заполнен (1000)')
            if self.db.execute("SELECT count(*) FROM jobs WHERE status IN ('pending','queued','running')").fetchone()[0] + len(sites) > 200:
                raise ValueError('Очередь заполнена (200 сайтов)')
            parent = preview['id']
            try:
                self.db.execute('INSERT INTO jobs(id,kind,status,created,params) VALUES(?,?,?,?,?)',
                                (parent, 'analysis-batch', 'queued', now(), json.dumps(preview['params'], ensure_ascii=False)))
                for site in sites:
                    self.db.execute('INSERT INTO jobs(id,kind,status,created,params) VALUES(?,?,?,?,?)',
                                    (uuid.uuid4().hex, 'analysis-site', 'pending', now(), json.dumps(dict(site, parent=parent), ensure_ascii=False)))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            del self.previews[token]
        self.wake.set()
        return parent

    def analysis_children(self, parent):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE kind='analysis-site' ORDER BY rowid")
                    if json.loads(r['params']).get('parent') == parent]

    def run_analysis_batch(self, row):
        from control_jobs import now
        for child in self.analysis_children(row['id']):
            if self.get(child['id'])['status'] == 'cancelled':
                continue
            if self.stopping.is_set() or self.get(row['id'])['cancel']:
                self._update(child['id'], status='cancelled', ended=now(), error='Пакет отменён')
                continue
            self._update(child['id'], status='running', started=now())
            self._run(child)
        children = self.analysis_children(row['id'])
        cancelled = self.stopping.is_set() or self.get(row['id'])['cancel'] or any(c['status'] == 'cancelled' for c in children)
        self._update(row['id'], status='cancelled' if cancelled else 'failed' if any(c['status'] == 'failed' for c in children) else 'succeeded', ended=now())

    def analysis_command(self, params, directory):
        url, host = target(params['url'])
        if host != params['domain']:
            raise ValueError('Домен изменился')
        source = directory / 'input.csv'
        with source.open('x', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['url', 'company_name', 'email'])
            writer.writerow([url, params.get('name', ''), ''])
        return [str(PYTHON), '-B', str(Path(__file__).with_name('control_analysis_runner.py')),
                str(source), str(directory / 'analysis')]
