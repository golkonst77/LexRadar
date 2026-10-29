"""Local Control Center pages and protected actions."""
import hmac
import json
import shlex
import sqlite3
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from control_catalog import Catalog
from control_jobs import TASKS
from control_leads import website
from control_results import read_file


def esc(value):
    return escape(str(value), quote=True)


def link(url, title):
    return '<a href="' + esc(url) + '">' + esc(title) + '</a>'


def external(value):
    return ('<a rel="noreferrer" href="' + esc(value) + '">' + esc(value) + '</a>') if website(value) else esc(value)


class Pages:
    def catalog(self):
        if not hasattr(self.server, 'catalog'):
            self.server.catalog = Catalog(self.server.results_dir, self.server.parser_dir)
        return self.server.catalog

    def index(self):
        return self.catalog().snapshot()

    def form(self, action, label, fields=''):
        token = getattr(self.server, 'csrf_token', '')
        return ('<form method="post" action="' + esc(action) + '">'
                '<input type="hidden" name="csrf" value="' + esc(token) + '">' + fields
                + '<label><input type="checkbox" name="confirm" value="yes" required> Подтверждаю локальное действие</label> '
                '<button type="submit">' + esc(label) + '</button></form>')

    def home(self):
        data = self.index()
        jobs = self.server.jobs.list() if hasattr(self.server, 'jobs') else []
        content = '<h1>Центр управления LexRadar</h1><p>Сохранённые данные читаются без изменения источников. Сетевой сбор, платный анализ и отправка отключены.</p><div class="grid">'
        for title, value, url in (
            ('Лиды Parser v3', len(data['leads']), '/organizations'),
            ('Анализы', sum(r['kind'] == 'audit' for r in data['reports']), '/reports?kind=audit'),
            ('PDF', sum(r['kind'] == 'pdf' for r in data['reports']), '/reports?kind=pdf'),
            ('Задания', len(jobs), '/jobs')):
            content += '<article><h2>' + link(url, title) + '</h2><strong>' + str(value) + '</strong></article>'
        content += '</div><p>Индекс: ' + esc(data['indexed_at']) + '; предупреждений: ' + str(data['warnings']) + '.</p>'
        content += '<h2>Последние задания</h2>' + self.job_table(jobs[:5])
        content += '<h2>Последние сохранённые материалы</h2>'
        for r in sorted(data['reports'], key=lambda r: r['date'], reverse=True)[:5]:
            content += '<p>' + link('/reports/' + r['id'], r['source']) + ' · ' + esc(r['date']) + '</p>'
        self.html(content)

    def reports(self, path):
        data = self.index()
        identifier = path.removeprefix('/reports/')
        if path != '/reports':
            record = next((r for r in data['reports'] if r['id'] == identifier), None)
            if not record:
                self.respond_json(404, {'error': 'not_found'})
                return
            content = '<h1>Сохранённый результат</h1><p>' + esc(record['source']) + '</p><p>Дата изменения файла: ' + esc(record['date']) + '</p>'
            content += '<p>Готово к просмотру · требуется проверка. Связь по сайту не подтверждает юридическое лицо или разрешение отправки.</p>'
            content += '<p>' + link('/files/' + record['id'], 'Открыть PDF' if record['kind'] == 'pdf' else 'Исходный JSON') + ' · '
            content += link('/files/' + record['id'] + '?download=1', 'Скачать') + '</p>'
            if record['kind'] == 'audit':
                content += '<p>Проверенный ресурс: ' + external(record['target']) + '</p>'
                content += self.audit_content(record)
            self.html(content)
            return
        params = parse_qs(urlsplit(self.path).query)
        query = params.get('q', [''])[0].strip()
        kind = params.get('kind', [''])[0]
        since = params.get('since', [''])[0]
        until = params.get('until', [''])[0]
        # ISO dates sort lexicographically; malformed filters are shown but ignored.
        from datetime import date
        for value, key in ((since, 'since'), (until, 'until')):
            try:
                if value:
                    date.fromisoformat(value)
            except ValueError:
                if key == 'since':
                    since = ''
                else:
                    until = ''
        records = [r for r in data['reports'] if (not kind or r['kind'] == kind)
                   and (not since or r['date'][:10] >= since) and (not until or r['date'][:10] <= until)
                   and query.casefold() in (r['source'] + ' ' + r['target'] + ' ' + r['lead_name']).casefold()]
        records.sort(key=lambda r: (r['date'], r['source']), reverse=True)
        content = '<h1>Отчёты и PDF</h1><p>Только сохранённые материалы. Дата — изменение файла, не дата юридической проверки.</p>'
        content += '<form><label>Организация / домен / путь <input name="q" value="' + esc(query) + '"></label> <label>Тип <select name="kind"><option value="">Все</option>'
        for key, label in (('audit', 'Анализ'), ('pdf', 'PDF')):
            content += '<option value="' + key + '"' + (' selected' if kind == key else '') + '>' + label + '</option>'
        content += '</select></label> <label>С <input type="date" name="since" value="' + esc(since) + '"></label> <label>По <input type="date" name="until" value="' + esc(until) + '"></label> <button>Найти</button></form>'
        content += '<p>Найдено: ' + str(len(records)) + '; предупреждений индекса: ' + str(data['warnings']) + '</p>'
        try:
            page = max(1, min(int(params.get('page', ['1'])[0]), max(1, (len(records) + 19) // 20)))
        except ValueError:
            page = 1
        for r in records[(page - 1)*20:page*20]:
            content += '<article><h2>' + link('/reports/' + r['id'], r['source']) + '</h2><p>' + esc(r['kind']) + ' · ' + esc(r['lead_name']) + ' · ' + esc(r['date']) + ' · ' + external(r['target']) + '</p></article>'
        if not records:
            content += '<p>Сохранённых материалов по этому запросу нет.</p>'
        content += '<nav aria-label="Страницы отчётов"><span>Страница ' + str(page) + '</span>'
        for target, label in ((page-1, 'Назад'), (page+1, 'Далее')):
            if target >= 1 and (target-1)*20 < len(records):
                content += link('/reports?' + urlencode(dict(q=query, kind=kind, since=since, until=until, page=target)), label)
        self.html(content + '</nav>')

    def file(self, path):
        record = next((r for r in self.index()['reports'] if r['id'] == path.removeprefix('/files/')), None)
        if not record:
            self.respond_json(404, {'error': 'not_found'})
            return
        try:
            raw = read_file(self.server.results_dir, record['source'], 50_000_000 if record['kind'] == 'pdf' else 8_000_000)
            if record['kind'] == 'pdf':
                if not raw.startswith(b'%PDF-'):
                    raise ValueError('Invalid PDF')
                content_type = 'application/pdf'
            else:
                data = json.loads(raw)
                if not isinstance(data, dict) or data.get('schema_version') != 'primary-audit-0.1':
                    raise ValueError('Unsupported audit')
                content_type = 'application/json; charset=utf-8'
        except (OSError, ValueError, RecursionError):
            self.respond_json(404, {'error': 'file_unavailable'})
            return
        disposition = 'attachment' if parse_qs(urlsplit(self.path).query).get('download') == ['1'] else 'inline'
        self.send_response(200)
        self.send_header('Content-Disposition', disposition + '; filename="' + ('proposal.pdf' if record['kind'] == 'pdf' else 'audit.json') + '"')
        self.write_response(raw, content_type)

    def job_table(self, jobs):
        if not jobs:
            return '<p>Заданий пока нет.</p>'
        content = '<div class="table-scroll"><table><tr><th>Задание</th><th>Статус</th><th>Создано UTC</th></tr>'
        for job in jobs:
            content += '<tr><td>' + link('/jobs/' + job['id'], TASKS.get(job['kind'], job['kind'])) + '</td><td>' + esc(job['status']) + '</td><td>' + esc(job['created']) + '</td></tr>'
        return content + '</table></div>'

    def jobs_page(self, path):
        jobs = getattr(self.server, 'jobs', None)
        if jobs is None:
            self.html('<h1>Задания</h1><p>Очередь недоступна в этом режиме.</p>')
            return
        if path == '/jobs':
            content = '<h1>Локальные задания</h1><p>Максимум одно активное задание; таймаут 60 секунд. Только чтение существующих файлов, без сети. Обновите страницу для нового статуса.</p>'
            options = '<label>Тип <select name="kind">' + ''.join('<option value="' + k + '">' + v + '</option>' for k, v in TASKS.items()) + '</select></label> '
            content += self.form('/jobs/preview', 'Предпросмотр команды', options)
            content += self.job_table(jobs.list())
            content += '<h2>Сетевой сбор и анализ</h2><p>Запуск недоступен. MedAdvisor для медицинских организаций, Orgpage для остальных; вся Россия, без регионального ограничения. N — число пригодных контактов с официальным сайтом и телефоном или email. CLI и ограничения боевого запуска требуют отдельной проверки.</p>'
        else:
            row = jobs.get(path.removeprefix('/jobs/'))
            if not row:
                self.respond_json(404, {'error': 'not_found'})
                return
            content = '<h1>' + esc(TASKS.get(row['kind'], row['kind'])) + '</h1><dl>'
            for label, key in (('Статус', 'status'), ('Создано UTC', 'created'), ('Начало UTC', 'started'), ('Окончание UTC', 'ended'), ('Причина / ограничение', 'error')):
                content += '<dt>' + label + '</dt><dd>' + esc(row[key] or '—') + '</dd>'
            content += '</dl><h2>stdout / stderr (до 64 КиБ)</h2><pre>' + esc(row['output']) + '</pre>'
            if row['status'] in {'queued', 'running'}:
                content += self.form('/jobs/' + row['id'] + '/cancel', 'Отменить')
        self.html(content)

    def settings(self):
        data = self.index()
        content = '<h1>Настройки и диагностика</h1><dl>'
        for label, path in (('Результаты: только чтение', self.server.results_dir), ('Parser v3: только чтение', self.server.parser_dir),
                            ('Состояние панели', getattr(self.server, 'state_dir', 'не подключено'))):
            content += '<dt>' + label + '</dt><dd>' + esc(path) + ' · ' + ('доступен' if Path(path).is_dir() else 'недоступен') + '</dd>'
        content += '</dl><p>Bind: 127.0.0.1. Основной проект не изменяется. Сеть, CRM API, письма и генерация PDF отключены.</p>'
        content += '<p>Индекс: ' + esc(data['indexed_at']) + '; предупреждений: ' + str(data['warnings']) + '. Проверка изменений не чаще раза в 5 секунд.</p>'
        content += self.form('/refresh', 'Перечитать индекс')
        self.html(content)

    def crm(self):
        data = self.index()
        content = '<h1>CRM: локальный preview</h1><p>Требуется проверка. Здесь нет отправки и CRM-записей. Состояние живой CRM не проверяется; сохранённые ID не подтверждены повторно.</p>'
        records = data['crm']
        content += '<h2>Сохранённые результаты подготовки</h2>'
        if not records:
            content += '<p>Файлы *crm-result.json не найдены в доступной части каталога результатов.</p>'
        for row in records[:200]:
            content += '<article><h2>' + esc(row.get('company_name') or row.get('website') or 'Запись подготовки') + '</h2>'
            for key in ('id', 'status', 'website', 'account_id', 'document_id', 'source', 'date'):
                if row.get(key):
                    content += '<p>' + key + ': ' + esc(row[key]) + '</p>'
            content += '<p>Отправка этой панелью: не выполнялась. Требуется проверка.</p></article>'
        content += '<p>Показано ' + str(min(200, len(records))) + ' из ' + str(len(records)) + '.</p><h2>Кандидаты Parser v3: не отправлено панелью</h2>'
        content += '<p>Кандидатов: ' + str(len(data['leads'])) + '. ' + link('/organizations', 'Открыть контакты и источники') + '. Наличие контакта не означает готовность КП или разрешение отправки.</p>'
        self.html(content)

    def valid_local_request(self):
        if len(self.headers.get_all('Host', [])) > 1:
            return False
        host = self.headers.get('Host', '')
        if not host:
            return self.request_version == 'HTTP/1.0'
        try:
            parsed = urlsplit('http://' + host)
            return (parsed.hostname in {'localhost', '127.0.0.1'} and not parsed.username
                    and not parsed.password and parsed.path == '' and not parsed.query and not parsed.fragment
                    and parsed.port == getattr(self.server, 'server_port', 8080))
        except ValueError:
            return False

    def do_POST(self):
        if not self.valid_local_request():
            self.respond_json(403, {'error': 'local_host_required'})
            return
        expected_origin = 'http://' + self.headers.get('Host', '')
        if self.headers.get('Origin') not in (None, expected_origin):
            self.respond_json(403, {'error': 'origin_rejected'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if (not 0 < length <= 16384 or self.headers.get('Transfer-Encoding')
                    or len(self.headers.get_all('Content-Length', [])) != 1):
                raise ValueError('size')
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                raise ValueError('type')
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete body')
            values = parse_qs(raw.decode('utf-8'), strict_parsing=True, max_num_fields=12)
        except (ValueError, UnicodeError):
            self.respond_json(400, {'error': 'invalid_form'})
            return
        token = getattr(self.server, 'csrf_token', '')
        if not token or not hmac.compare_digest(values.get('csrf', [''])[0].encode('utf-8'), token.encode('utf-8')) or values.get('confirm') != ['yes']:
            self.respond_json(403, {'error': 'confirmation_required'})
            return
        path = urlsplit(self.path).path
        jobs = getattr(self.server, 'jobs', None)
        try:
            if path == '/refresh':
                self.catalog().snapshot(force=True)
                destination = '/settings'
            elif jobs and path in {'/jobs/preview', '/jobs'}:
                kind = values.get('kind', [''])[0]
                command = jobs.command(kind)
                if path == '/jobs/preview':
                    content = '<h1>Предпросмотр локальной команды</h1><pre>' + esc(shlex.join(command)) + '</pre><p>Без сети. Вывод и состояние только в каталоге панели. Исходники и результаты не изменяются.</p>'
                    content += self.form('/jobs', 'Поставить в очередь', '<input type="hidden" name="kind" value="' + esc(kind) + '">')
                    self.html(content)
                    return
                destination = '/jobs/' + jobs.submit(kind)
            elif jobs and path.startswith('/jobs/') and path.endswith('/cancel'):
                identifier = path[len('/jobs/'):-len('/cancel')]
                jobs.cancel(identifier)
                destination = '/jobs/' + identifier
            else:
                self.respond_json(404, {'error': 'not_found'})
                return
        except ValueError as exc:
            self.html('<h1>Действие отклонено</h1><p>' + esc(exc) + '</p>', 400)
            return
        except (OSError, sqlite3.Error):
            self.html('<h1>Действие не выполнено</h1><p>Локальное хранилище недоступно. Проверьте права и свободное место.</p>', 503)
            return
        self.send_response(303)
        self.send_header('Location', destination)
        self.write_response(b'', 'text/plain')
