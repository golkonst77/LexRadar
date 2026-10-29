"""One durable local worker. Only fixed offline commands, no shell or network CLI."""
import fcntl
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

TASKS = {'inspect-import': 'Проверить существующий импорт',
         'offline-results': 'Проверить индекс сохранённых результатов'}
LOG_LIMIT = 65536
TIMEOUT = 60


def log_text(raw):
    return raw.decode('utf-8', errors='replace').encode('utf-8')[:LOG_LIMIT].decode('utf-8', errors='ignore')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


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
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.state_dir / 'jobs.sqlite3', check_same_thread=False)
        os.chmod(self.state_dir / 'jobs.sqlite3', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, kind TEXT, status TEXT, created TEXT, started TEXT,
            ended TEXT, output TEXT DEFAULT '', error TEXT DEFAULT '', cancel INTEGER DEFAULT 0)''')
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

    def list(self):
        with self.lock:
            return [dict(r) for r in self.db.execute('SELECT id,kind,status,created,started,ended,error,cancel FROM jobs ORDER BY created DESC, rowid DESC')]

    def get(self, identifier):
        with self.lock:
            row = self.db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
            return dict(row) if row else None

    def submit(self, kind):
        self.command(kind)
        with self.lock:
            if self.stopping.is_set():
                raise ValueError('Сервер останавливается')
            if self.db.execute('SELECT count(*) FROM jobs').fetchone()[0] >= 1000:
                raise ValueError('Достигнут лимит журнала: 1000 заданий')
            if self.db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 100:
                raise ValueError('Очередь заполнена')
            identifier = uuid.uuid4().hex
            self.db.execute('INSERT INTO jobs(id,kind,status,created) VALUES(?,?,?,?)',
                            (identifier, kind, 'queued', now()))
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
            process = subprocess.Popen(self.command(row['kind']), cwd=directory,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env={'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PYTHONIOENCODING': 'utf-8'})
            deadline = time.monotonic() + TIMEOUT
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, b'[stdout] ')
                selector.register(process.stderr, selectors.EVENT_READ, b'[stderr] ')
                while selector.get_map() or process.poll() is None:
                    if self.stopping.is_set() or self.get(identifier)['cancel']:
                        status, error = 'cancelled', 'Отменено пользователем или остановкой сервера'
                        if process.poll() is None:
                            process.kill()
                    elif time.monotonic() >= deadline:
                        error = 'Превышен лимит времени'
                        if process.poll() is None:
                            process.kill()
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
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            error = 'Не удалось выполнить локальное задание: ' + type(exc).__name__
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
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
