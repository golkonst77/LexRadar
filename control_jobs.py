"""One durable worker for fixed local checks and explicitly confirmed Parser v3."""
import fcntl
import os
import json
import signal
import secrets
import re
from pathlib import Path
import selectors
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from control_parser import Parser, PARSER_TIMEOUT
from control_leads import load_leads

TASKS = {'inspect-import': 'Проверить существующий импорт',
         'offline-results': 'Проверить индекс сохранённых результатов'}
LOG_LIMIT = 65536
TIMEOUT = 60


def log_text(raw):
    value = raw.decode('utf-8', errors='replace')
    value = re.sub(r'(?i)(authorization|api[_-]?key|token|cookies?|password|secret)([\s\"\']*[:=]\s*)[^\r\n]*', r'\1\2[скрыто]', value)
    value = re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1[скрыто]@', value)
    return value.encode('utf-8')[:LOG_LIMIT].decode('utf-8', errors='ignore')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class Jobs:
    def __init__(self, state_dir, parser_dir, results_dir):
        self.state_dir = Path(state_dir).absolute()
        if any(p.is_symlink() for p in (self.state_dir, *self.state_dir.parents)):
            raise ValueError('State directory must not contain symlinks')
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        for name in ('jobs.sqlite3', 'worker.lock'):
            if (self.state_dir / name).is_symlink():
                raise ValueError('State file must not be a symlink')
        self.owner = open(self.state_dir / 'worker.lock', 'a')
        os.chmod(self.state_dir / 'worker.lock', 0o600)
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner.close()
            raise ValueError('Another Control Center owns this job directory') from None
        self.parser_dir, self.results_dir = Path(parser_dir).absolute(), Path(results_dir).absolute()
        self.parser = Parser()
        self.previews = {}
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.state_dir / 'jobs.sqlite3', check_same_thread=False)
        os.chmod(self.state_dir / 'jobs.sqlite3', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, kind TEXT, status TEXT, created TEXT, started TEXT,
            ended TEXT, output TEXT DEFAULT '', error TEXT DEFAULT '', cancel INTEGER DEFAULT 0)''')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}
        for name in ('params', 'result'):
            if name not in columns:
                self.db.execute('ALTER TABLE jobs ADD COLUMN ' + name + " TEXT DEFAULT '{}'")
        self.db.execute("UPDATE jobs SET status='failed', ended=?, error='Прервано перезапуском; повторный запуск только вручную' WHERE status='running'", (now(),))
        self.db.execute("UPDATE jobs SET status='cancelled', ended=?, error='Очередь остановлена перезапуском' WHERE status='queued'", (now(),))
        self.db.commit()
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._work, daemon=True)
        self.thread.start()

    def command(self, kind):
        if kind not in TASKS:
            raise ValueError('Недопустимый тип задания')
        return [sys.executable, '-E', '-s', str(Path(__file__).with_name('control_worker.py').absolute()),
                kind, '--parser-dir', str(self.parser_dir), '--results-dir', str(self.results_dir)]

    def preview_parser(self, industry, target, name=''):
        params = self.parser.validate(industry, target, name)
        with self.lock:
            self.previews = {k: v for k, v in self.previews.items() if time.monotonic() - v['time'] < 900}
            if len(self.previews) >= 100:
                raise ValueError('Слишком много предпросмотров; повторите через 15 минут')
            token = secrets.token_urlsafe(32)
            preview = dict(params=params, id=uuid.uuid4().hex, time=time.monotonic())
            self.previews[token] = preview
            return token, preview, self.parser.command(params, self.state_dir / preview['id'])

    def submit_parser(self, token):
        with self.lock:
            preview = self.previews.get(token)
            if not preview or time.monotonic() - preview['time'] >= 900:
                raise ValueError('Предпросмотр истёк или уже использован')
            errors = self.parser.diagnostic(preview['params']['industry'])
            if errors:
                raise ValueError('; '.join(errors))
            identifier = self.submit('parser-v3', preview)
            del self.previews[token]
            return identifier

    def collected(self):
        records, warnings, remaining, runs = [], 0, 32_000_000, 0
        for job in self.list():
            if job['kind'] != 'parser-v3' or job['status'] != 'succeeded':
                continue
            # shortcut: index at most 64 successful runs / 32 MB; archive old runs before growing beyond this.
            runs += 1
            try:
                size = (self.state_dir / job['id'] / 'parser.csv').lstat().st_size
            except OSError:
                warnings += 1
                continue
            if runs > 64 or size > remaining:
                warnings += 1
                break
            remaining -= size
            leads, count = load_leads(self.state_dir / job['id'])
            warnings += count
            params = json.loads(self.get(job['id'])['params'])
            for lead in leads:
                lead['id'] = job['id'] + '-' + lead['id']
                lead['run_id'] = job['id']
                lead['run_name'] = params.get('name') or job['id']
            records.extend(leads[:max(0, 20000 - len(records))])
            if len(records) >= 20000:
                warnings += 1
                break
        return records, warnings

    def list(self):
        with self.lock:
            return [dict(r) for r in self.db.execute('SELECT id,kind,status,created,started,ended,error,cancel FROM jobs ORDER BY created DESC, rowid DESC')]

    def get(self, identifier):
        with self.lock:
            row = self.db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
            return dict(row) if row else None

    def submit(self, kind, preview=None):
        if kind == 'parser-v3':
            if preview is None:
                raise ValueError('Нужен подтверждённый предпросмотр')
        else:
            self.command(kind)
        with self.lock:
            if self.stopping.is_set():
                raise ValueError('Сервер останавливается')
            if self.db.execute('SELECT count(*) FROM jobs').fetchone()[0] >= 1000:
                raise ValueError('Достигнут лимит журнала: 1000 заданий')
            if self.db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 100:
                raise ValueError('Очередь заполнена')
            identifier = preview['id'] if preview else uuid.uuid4().hex
            self.db.execute('INSERT INTO jobs(id,kind,status,created) VALUES(?,?,?,?)',
                            (identifier, kind, 'queued', now()))
            if preview:
                self.db.execute('UPDATE jobs SET params=? WHERE id=?', (json.dumps(preview['params'], ensure_ascii=False), identifier))
            self.db.commit()
        self.wake.set()
        return identifier

    def cancel(self, identifier):
        with self.lock:
            row = self.get(identifier)
            if not row:
                raise ValueError('Задание не найдено')
            if row['status'] == 'queued':
                self.db.execute("UPDATE jobs SET status='cancelled',cancel=1,ended=? WHERE id=?", (now(), identifier))
            elif row['status'] == 'running':
                self.db.execute('UPDATE jobs SET cancel=1 WHERE id=?', (identifier,))
            self.db.commit()
        self.wake.set()

    def _update(self, identifier, **values):
        with self.lock:
            self.db.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                            (*values.values(), identifier))
            self.db.commit()

    def _run(self, row):
        identifier = row['id']
        process = None
        output = bytearray()
        truncated = False
        status, error = 'failed', ''
        try:
            directory = self.state_dir / identifier
            directory.mkdir(mode=0o700)
            params = json.loads(row['params'])
            if row['kind'] == 'parser-v3' and self.parser.diagnostic(params['industry']):
                raise ValueError('Parser v3 недоступен; смотрите диагностику')
            command = self.parser.command(params, directory) if row['kind'] == 'parser-v3' else self.command(row['kind'])
            if row['kind'] == 'parser-v3':
                command = [sys.executable, '-B', str(Path(__file__).with_name('control_parser_runner.py').absolute()), *command]
            if self.stopping.is_set() or self.get(identifier)['cancel']:
                return
            process = subprocess.Popen(command, cwd=directory, start_new_session=True,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env={'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PYTHONIOENCODING': 'utf-8',
                                            'HOME': str(directory), 'TMPDIR': str(directory),
                                            'PLAYWRIGHT_BROWSERS_PATH': str(self.parser.browsers),
                                            **({k: os.environ[k] for k in ('DISPLAY', 'XAUTHORITY') if k in os.environ} if row['kind'] == 'parser-v3' else {})})
            deadline = time.monotonic() + (PARSER_TIMEOUT if row['kind'] == 'parser-v3' else TIMEOUT)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, b'[stdout] ')
                selector.register(process.stderr, selectors.EVENT_READ, b'[stderr] ')
                while selector.get_map() or process.poll() is None:
                    if self.stopping.is_set() or self.get(identifier)['cancel']:
                        status, error = 'cancelled', 'Отменено пользователем или остановкой сервера'
                        kill_group(process)
                    elif time.monotonic() >= deadline:
                        error = 'Превышен лимит времени'
                        kill_group(process)
                    if process.poll() is not None:
                        kill_group(process)
                    for key, _ in selector.select(0.1):
                        chunk = os.read(key.fileobj.fileno(), 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        remaining = LOG_LIMIT - len(output)
                        block = key.data + chunk
                        output.extend(block[:remaining])
                        truncated |= len(block) > remaining
                        self._update(identifier, output=log_text(output))
                code = process.wait()
            if not error:
                status = 'succeeded' if code == 0 else 'failed'
                error = '' if code == 0 else f'Код завершения: {code}'
                if code == 0 and row['kind'] == 'parser-v3':
                    result, error = self.parser.result(directory, params['target'])
                    self._update(identifier, result=json.dumps(result, ensure_ascii=False))
                    if error:
                        status = 'failed'
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            status, error = 'failed', 'Не удалось выполнить задание: ' + type(exc).__name__
        finally:
            if process is not None:
                kill_group(process)
                process.wait()
                for stream in (process.stdout, process.stderr):
                    if stream:
                        stream.close()
            if truncated:
                error = (error + '; ' if error else '') + 'Журнал ограничен 64 КиБ'
            with self.lock:
                if self.get(identifier)['cancel'] or self.stopping.is_set():
                    status, error = 'cancelled', 'Отменено пользователем или остановкой сервера'
                self._update(identifier, status=status, error=error, ended=now(), output=log_text(output))

    def _work(self):
        while not self.stopping.is_set():
            with self.lock:
                row = self.db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
                if row:
                    self._update(row['id'], status='running', started=now())
            if row:
                self._run(dict(row))
            else:
                self.wake.wait(0.5)
                self.wake.clear()

    def close(self):
        self.stopping.set()
        self.wake.set()
        self.thread.join()
        with self.lock:
            self.db.execute("UPDATE jobs SET status='cancelled',ended=?,error='Сервер остановлен' WHERE status='queued'", (now(),))
            self.db.commit()
            self.db.close()
        self.owner.close()
