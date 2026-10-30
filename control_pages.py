"""Local Control Center pages and protected actions."""
import hmac
import json
import shlex
import sqlite3
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from collections import Counter
from control_catalog import Catalog, domain
from control_jobs import TASKS
from control_analysis import target, ARTIFACTS
from control_leads import website, merge_leads, analysis_rows
from control_results import read_file


def esc(value):
    return escape(str(value), quote=True)


def link(url, title):
    return '<a href="' + esc(url) + '">' + esc(title) + '</a>'


def external(value):
    return ('<a rel="noreferrer" href="' + esc(value) + '">' + esc(value) + '</a>') if website(value) else esc(value)


def job_status(value):
    return {'pending': 'Ожидает', 'queued': 'Ожидает', 'running': 'Выполняется',
            'succeeded': 'Успешно', 'failed': 'Ошибка / прервано', 'cancelled': 'Отменено'}.get(value, value)


def report_title(record):
    subject = record['lead_name'] or record['target'] or Path(record['source']).parent.name
    return ('Анализ сайта' if record['kind'] == 'audit' else 'Сохранённый PDF') + ' — ' + subject


class Pages:
    def catalog(self):
        if not hasattr(self.server, 'catalog'):
            self.server.catalog = Catalog(self.server.results_dir, self.server.parser_dir)
        return self.server.catalog

    def index(self):
        data = self.catalog().snapshot()
        jobs = getattr(self.server, 'jobs', None)
        if jobs:
            leads, warnings = jobs.collected()
            if leads or warnings:
                data = {**data, 'leads': data['leads'] + leads, 'warnings': data['warnings'] + warnings}
        data = {**data, 'leads': merge_leads(data['leads'])}
        data['domain_counts'] = Counter(domain(url) for lead in data['leads'] for url in lead['websites'])
        names = {domain(url): lead['name'] for lead in data['leads'] for url in lead['websites']}
        data['reports'] = [{**r, 'lead_name': names.get(domain(r['target']), '')} for r in data['reports']]
        return data

    def form(self, action, label, fields='', confirm=True):
        token = getattr(self.server, 'csrf_token', '')
        return ('<form method="post" action="' + esc(action) + '">'
                '<input type="hidden" name="csrf" value="' + esc(token) + '">' + fields
                + ('<label><input type="checkbox" name="confirm" value="yes" required> Подтверждаю действие</label> ' if confirm else '<input type="hidden" name="confirm" value="yes">')
                + '<button type="submit">' + esc(label) + '</button></form>')

    def analysis_form(self, fields, label='Анализ', identifier=''):
        return ('<form method="post" action="/analysis/preview"' + (' id="' + esc(identifier) + '"' if identifier else '') + '>'
                '<input type="hidden" name="csrf" value="' + esc(getattr(self.server, 'csrf_token', '')) + '">' + fields
                + '<button>' + esc(label) + '</button></form>')

    def analysis_status(self, record, history):
        try:
            _, host = target(next(iter(record['websites']), ''))
        except ValueError:
            return 'Нет допустимого сайта'
        rows = analysis_rows(record, history)
        row = next((r for r in rows if r['status'] == 'succeeded'), rows[0] if rows else None)
        if not row:
            return 'Не запускался для этой записи'
        from control_enrichment import observation
        saved = observation(self.server.jobs.state_dir, row, host)
        content = link('/jobs/' + rows[0]['id'], 'Последнее задание: ' + job_status(rows[0]['status']))
        if row['id'] != rows[0]['id']:
            content += '<br>' + link('/jobs/' + row['id'], 'Сохранённый успешный анализ')
        qualification = saved['qualification']
        content += '<br>Квалификация сайта: ' + esc('Требует ручной проверки' if qualification == 'HOLD' else qualification or 'Нет данных')
        content += '<details><summary>Подробности анализа</summary>'
        content += 'Сохранённый анализ UTC: ' + esc(row['ended'] or row['started'] or row['created'])
        content += '<br>Статус движка: ' + esc(saved['status'] or 'Нет данных')
        content += '<br>Reason: ' + esc(saved['reason'] or 'Нет данных')
        if saved['warning']:
            content += '<br>' + esc(saved['warning'])
        content += '</details>'
        reports = []
        for name, relative in ARTIFACTS.items():
            try:
                read_file(self.server.jobs.state_dir, row['id'] + '/analysis/' + relative, 8_000_000, prefix=5)
            except (OSError, ValueError):
                continue
            reports.append(link('/analysis-files/' + row['id'] + '/' + name, name))
        if reports:
            content += '<details><summary>Отчёты</summary>' + ' · '.join(reports) + '</details>'
        if len(rows) > 1:
            content += '<details><summary>История домена</summary>'
            content += ' · '.join(link('/jobs/' + r['id'], r['created'] + ' — ' + job_status(r['status'])) for r in rows)
            content += '</details>'
        return content

    def analysis_email(self, record, history):
        content = '<span class="email-value">' + esc(record.get('email') or '—') + '</span>'
        try:
            _, host = target(next(iter(record['websites']), ''))
        except ValueError:
            return content
        rows = analysis_rows(record, history)
        if not rows:
            return content
        from control_enrichment import observation
        saved = {'contacts': [contact for r in rows
                              for contact in observation(self.server.jobs.state_dir, r, host)['contacts']]}
        if saved['contacts']:
            if not record.get('email'):
                content = '<span class="email-value">' + esc(saved['contacts'][0]['email']) + '</span>'
                content += '<br><small>Найден на сайте · не подтверждён</small>'
            sources = list(dict.fromkeys(url for contact in saved['contacts'] for url in contact['sources']))
            content += '<details class="email-sources"><summary>Источники (' + str(len(sources)) + ')</summary>'
            content += '<small>Кандидаты, принадлежность не подтверждена</small>'
            for contact in saved['contacts']:
                content += '<div>' + esc(contact['email']) + '</div>'
            for url in sources:
                content += '<div>' + external(url) + '</div>'
            content += '</details>'
        return content

    def home(self):
        data = self.index()
        jobs = self.server.jobs.list() if hasattr(self.server, 'jobs') else []
        content = '<h1>Центр управления LexRadar</h1><p>Сохранённые данные читаются без изменения источников. Сбор запускается только после отдельного подтверждения в заданиях. Платный анализ и отправка отключены.</p><p><a href="/jobs">Собрать организации</a> · <a href="/organizations">Лиды</a> · <a href="/reports">Отчёты</a></p><div class="grid">'
        for title, value, url in (
            ('Лиды Parser v3', len(data['leads']), '/organizations'),
            ('Анализы', sum(r['kind'] == 'audit' for r in data['reports']), '/reports?kind=audit'),
            ('PDF', sum(r['kind'] == 'pdf' for r in data['reports']), '/reports?kind=pdf'),
            ('Задания', len(jobs), '/jobs')):
            content += '<article><h2>' + link(url, title) + '</h2><strong>' + str(value) + '</strong></article>'
        content += '</div><p>Индекс: ' + esc(data['indexed_at']) + '; предупреждений: ' + str(data['warnings']) + '.</p>'
        content += '<h2>Последние задания</h2>' + self.job_table(jobs[:5])
        for job in jobs[:5]:
            if job['error']:
                content += '<p role="status">' + link('/jobs/' + job['id'], job['status']) + ': ' + esc(job['error']) + '</p>'
        content += '<h2>Последние сохранённые материалы</h2>'
        for r in sorted(data['reports'], key=lambda r: r['date'], reverse=True)[:5]:
            content += '<p>' + link('/reports/' + r['id'], report_title(r)) + ' · ' + esc(r['date']) + '</p>'
        self.html(content)

    def reports(self, path):
        data = self.index()
        identifier = path.removeprefix('/reports/')
        if path != '/reports':
            record = next((r for r in data['reports'] if r['id'] == identifier), None)
            if not record:
                self.respond_json(404, {'error': 'not_found'})
                return
            content = '<h1>' + esc(report_title(record)) + '</h1><p><small>' + esc(record['source']) + '</small>' + '</p><p>Дата изменения файла: ' + esc(record['date']) + '</p>'
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
        content = '<h1>Отчёты и PDF</h1><p>Разделы: анализы сайта и сохранённые PDF. Утверждённые КП отдельно не установлены: имя proposal.pdf не доказывает одобрение. Прочие файлы досье не публикуются.</p><p>Только сохранённые материалы. Дата — изменение файла, не дата юридической проверки.</p>'
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
            content += '<article><h2>' + link('/reports/' + r['id'], report_title(r)) + '</h2><p>' + esc('Сохранённый PDF · утверждение КП не подтверждено' if r['kind'] == 'pdf' else 'Анализ сайта') + ' · ' + esc(r['lead_name']) + ' · ' + esc(r['date']) + ' · ' + external(r['target']) + '</p><p><small>' + esc(r['source']) + '</small></p></article>'
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
            progress = ''
            if job['kind'] == 'analysis-batch':
                children = self.server.jobs.analysis_children(job['id'])
                progress = ' · ' + str(sum(c['status'] not in {'pending', 'queued', 'running'} for c in children)) + '/' + str(len(children))
            elif job['kind'] == 'analysis-site':
                progress = ' · ' + json.loads(self.server.jobs.get(job['id'])['params'])['domain']
            content += '<tr><td>' + link('/jobs/' + job['id'], ('Собрать организации' if job['kind'] == 'parser-v3' else TASKS.get(job['kind'], {'analysis-batch': 'Пакет анализа', 'analysis-site': 'Анализ сайта'}.get(job['kind'], job['kind'])))) + '</td><td>' + esc(job_status(job['status']) + progress) + '</td><td>' + esc(job['created']) + '</td></tr>'
        return content + '</table></div>'

    def analysis_file(self, path):
        parts = path.split('/')
        jobs = getattr(self.server, 'jobs', None)
        row = jobs.get(parts[2]) if jobs and len(parts) == 4 else None
        if not row or row['kind'] != 'analysis-site' or parts[3] not in ARTIFACTS:
            self.respond_json(404, {'error': 'not_found'})
            return
        try:
            raw = read_file(jobs.state_dir, row['id'] + '/analysis/' + ARTIFACTS[parts[3]], 8_000_000)
            if parts[3] == 'manifest.json':
                data = json.loads(raw)
                # Paths, input values and collection bodies remain local to the state directory.
                raw = json.dumps({'summary': data.get('summary'), 'rows': [
                    {k: r[k] for k in ('status', 'reason', 'stages', 'qualification') if k in r}
                    for r in data.get('rows', [])]}, ensure_ascii=False, indent=2).encode()
        except (OSError, ValueError):
            self.respond_json(404, {'error': 'file_unavailable'})
            return
        if parts[3] == 'proposal.pdf':
            if not raw.startswith(b'%PDF-'):
                self.respond_json(404, {'error': 'invalid_pdf'})
                return
            self.respond(200, raw, 'application/pdf')
        else:
            from control_jobs import log_text
            self.respond(200, log_text(raw).encode(), 'text/plain; charset=utf-8')

    def jobs_page(self, path):
        jobs = getattr(self.server, 'jobs', None)
        if jobs is None:
            self.html('<h1>Задания</h1><p>Очередь недоступна в этом режиме.</p>')
            return
        if path == '/jobs':
            content = '<h1>Задания</h1><p>Максимум один активный процесс. Локальные проверки: без сети, до 60 секунд. Сбор организаций: отдельное сетевое действие, до 4 часов.</p>'
            options = '<label>Тип <select name="kind">' + ''.join('<option value="' + k + '">' + v + '</option>' for k, v in TASKS.items()) + '</select></label> '
            content += self.form('/jobs/preview', 'Предпросмотр команды', options)
            content += self.job_table(jobs.list())
            content += '<h2>Собрать организации</h2><p>Вся Россия. MedAdvisor для медицинских клиник; Orgpage для остальных. N — пригодные уникальные сайты с телефоном либо email. До 1000, до 200 страниц и max(200, 10 × N) карточек; таймаут 4 часа. Цель может не быть достигнута. Скрининг главной страницы уже встроен в Parser; платных API и PDF нет.</p>'
            try:
                options = '<label>Отрасль <select name="industry">' + ''.join('<option value="' + esc(k) + '">' + esc(v) + '</option>' for k, v in jobs.parser.industries().items()) + '</select></label>'
                options += '<label>N пригодных лидов <input type="number" name="target" min="1" max="1000" value="10" required></label><label>Название запуска <input name="name" maxlength="100"></label>'
                content += self.form('/collect/preview', 'Проверить и показать команду', options)
            except (OSError, ValueError, SyntaxError, KeyError, StopIteration):
                content += '<p>Конфигурация Parser v3 недоступна.</p>'
            errors = jobs.parser.diagnostic()
            if errors:
                content += '<p role="status">Запуск недоступен: ' + esc('; '.join(errors)) + '</p><button disabled>Запустить сбор</button>'

        else:
            row = jobs.get(path.removeprefix('/jobs/'))
            if not row:
                self.respond_json(404, {'error': 'not_found'})
                return
            content = '<h1>' + esc(('Собрать организации' if row['kind'] == 'parser-v3' else TASKS.get(row['kind'], {'analysis-batch': 'Пакет анализа', 'analysis-site': 'Анализ сайта'}.get(row['kind'], row['kind'])))) + '</h1><dl>'
            for label, key in (('Статус', 'status'), ('Создано UTC', 'created'), ('Начало UTC', 'started'), ('Окончание UTC', 'ended'), ('Причина / ограничение', 'error')):
                content += '<dt>' + label + '</dt><dd>' + esc(job_status(row[key]) if key == 'status' and row['kind'].startswith('analysis-') else row[key] or '—') + '</dd>'
            if row['kind'] == 'analysis-batch':
                children = jobs.analysis_children(row['id'])
                finished = sum(c['status'] not in {'pending', 'queued', 'running'} for c in children)
                content += '<dt>Прогресс</dt><dd>' + str(finished) + '/' + str(len(children)) + '</dd>'
                for child in children:
                    params = json.loads(child['params'])
                    content += '<dd>' + link('/jobs/' + child['id'], params['name'] + ' · ' + params['domain']) + ' · ' + esc(job_status(child['status'])) + '</dd>'
            if row['kind'] == 'analysis-site':
                params = json.loads(row['params'])
                content += '<dd>' + link('/jobs/' + params['parent'], 'Пакет') + ' · ' + link('/organizations/' + params['lead_id'], 'Организация') + '</dd>'
                content += '<dt>Сайт</dt><dd>' + esc(params['url']) + '</dd><dt>Режим</dt><dd>Реальный предварительный анализ правил; не платный AI-аудит. КП требует ручной проверки.</dd>'
                content += '<dt>Артефакты</dt><dd>' + esc(jobs.state_dir / row['id'] / 'analysis') + '</dd>'
                for name, relative in ARTIFACTS.items():
                    if (jobs.state_dir / row['id'] / 'analysis' / relative).is_file():
                        content += '<dd>' + link('/analysis-files/' + row['id'] + '/' + name, name) + '</dd>'
            if row['kind'] == 'parser-v3':
                content += '<dt>Параметры запуска</dt><dd>' + esc(row['params']) + '</dd><dt>Результат</dt><dd>' + esc(row['result']) + '</dd>'
                content += '<dt>Каталог</dt><dd>' + esc(jobs.state_dir / row['id']) + '</dd>'
                if row['status'] == 'succeeded':
                    content += '<dd>' + link('/organizations?run=' + row['id'], 'Лиды этого запуска') + ' · ' + link('/exports/' + row['id'], 'Скачать CSV') + '</dd>'
            content += '</dl><h2>stdout / stderr (до 64 КиБ)</h2><pre>' + esc(row['output']) + '</pre>'
            if row['status'] in {'pending', 'queued', 'running'}:
                self.refresh_seconds = 5
                content += '<p>Статус и журнал обновляются каждые 5 секунд; процент завершения неизвестен.</p>'
                content += self.form('/jobs/' + row['id'] + '/cancel', 'Отменить', confirm=not row['kind'].startswith('analysis-'))
        self.html('<p><a href="/organizations">← Организации</a> · <a href="/jobs">Задания</a></p>' + content)

    def settings(self):
        data = self.index()
        content = '<h1>Настройки и диагностика</h1><dl>'
        for label, path in (('Результаты: только чтение', self.server.results_dir), ('Parser v3: только чтение', self.server.parser_dir),
                            ('Состояние панели', getattr(self.server, 'state_dir', 'не подключено'))):
            content += '<dt>' + label + '</dt><dd>' + esc(path) + ' · ' + ('доступен' if Path(path).is_dir() else 'недоступен') + '</dd>'
        jobs = getattr(self.server, 'jobs', None)
        if jobs:
            import os
            content += '<dt>Исходники CLI (только чтение)</dt><dd>' + esc(jobs.parser.source) + '</dd>'
            content += '<dt>Python Parser</dt><dd>' + esc(jobs.parser.python) + '</dd><dt>Chromium cache (только чтение)</dt><dd>' + esc(jobs.parser.browsers) + '</dd>'
            content += '<dt>Запись состояния</dt><dd>' + ('доступна' if os.access(jobs.state_dir, os.W_OK | os.X_OK) else 'недоступна') + '</dd>'
            try:
                industries = jobs.parser.industries()
            except (OSError, ValueError):
                industries = {'1': 'Parser v3'}
            checked = {}
            for industry, name in industries.items():
                source = 'medical' if industry == '1' else 'orgpage'
                if source not in checked:
                    checked[source] = jobs.parser.diagnostic(industry)
                content += '<dt>' + esc(name) + '</dt><dd>' + esc('; '.join(checked[source]) or 'Локальные зависимости доступны; сеть и доступ источника не проверены') + '</dd>'
        content += '</dl><p>Bind: 127.0.0.1. Основной проект не изменяется. Просмотр не обращается к сайтам. Сетевой сбор — только подтверждённое задание; CRM API, письма и генерация PDF отключены.</p>'
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

    def export(self, identifier):
        jobs = getattr(self.server, 'jobs', None)
        row = jobs.get(identifier) if jobs else None
        if not row or row['kind'] != 'parser-v3' or row['status'] != 'succeeded':
            self.respond_json(404, {'error': 'not_found'})
            return
        try:
            import csv
            import io
            raw = read_file(jobs.state_dir, identifier + '/parser.csv', 8_000_000)
            reader = csv.reader(io.StringIO(raw.decode('utf-8-sig')), delimiter=';', strict=True)
            output = io.StringIO()
            writer = csv.writer(output, delimiter=';')
            for cells in reader:
                writer.writerow(["'" + c if c.lstrip().startswith(('=', '+', '-', '@')) else c for c in cells])
        except (OSError, ValueError, csv.Error):
            self.respond_json(404, {'error': 'export_unavailable'})
            return
        self.send_response(200)
        self.send_header('Content-Disposition', 'attachment; filename="parser.csv"')
        self.write_response(output.getvalue().encode('utf-8-sig'), 'text/csv; charset=utf-8')

    def valid_local_request(self):
        if len(self.headers.get_all('Host', [])) > 1:
            return False
        host = self.headers.get('Host', '')
        if not host:
            return self.request_version == 'HTTP/1.0'
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in host) or '?' in host or '#' in host:
            return False
        try:
            parsed = urlsplit('http://' + host)
            return (parsed.hostname in {'localhost', '127.0.0.1'} and not parsed.username
                    and not parsed.password and parsed.path == '' and not parsed.query and not parsed.fragment
                    and parsed.port == getattr(self.server, 'server_port', 8080))
        except ValueError:
            return False

    def origin_rejection(self):
        origins = self.headers.get_all('Origin', [])
        if not origins:
            return None  # Existing policy: CSRF and confirmation still required.
        if len(origins) != 1:
            return 'multiple_origin'
        origin = origins[0]
        # urlsplit strips some whitespace/control characters; reject before parsing.
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in origin):
            return 'malformed_origin'
        try:
            parsed = urlsplit(origin)
            if (parsed.scheme not in {'http', 'https'} or not parsed.netloc
                    or parsed.hostname not in {'localhost', '127.0.0.1'}
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path or '?' in origin or '#' in origin
                    or parsed.netloc.endswith(':')):
                return 'malformed_or_nonlocal_origin'
            port = parsed.port if parsed.port is not None else (80 if parsed.scheme == 'http' else 443)
            host = urlsplit('http://' + self.headers.get('Host', ''))
            host_port = host.port if host.port is not None else 80
            if parsed.scheme != 'http':
                return 'scheme_mismatch'
            if parsed.hostname != host.hostname:
                return 'hostname_mismatch'
            if port != host_port:
                return 'port_mismatch'
        except ValueError:
            return 'malformed_origin'
        return None

    def reject_post(self, status, error, reason):
        # Only fixed reason codes and counts: no headers, URL, credentials or body.
        self.log_message('POST rejected: error=%s reason=%s host_count=%d origin_count=%d',
                         error, reason, len(self.headers.get_all('Host', [])),
                         len(self.headers.get_all('Origin', [])))
        self.respond_json(status, {'error': error})

    def do_POST(self):
        if not self.valid_local_request():
            self.reject_post(403, 'local_host_required', 'invalid_host')
            return
        reason = self.origin_rejection()
        if reason:
            self.reject_post(403, 'origin_rejected', reason)
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
            values = parse_qs(raw.decode('utf-8'), strict_parsing=True, max_num_fields=220)
        except (ValueError, UnicodeError):
            self.reject_post(400, 'invalid_form', 'invalid_body_or_headers')
            return
        path = urlsplit(self.path).path
        token = getattr(self.server, 'csrf_token', '')
        if not token or not hmac.compare_digest(values.get('csrf', [''])[0].encode('utf-8'), token.encode('utf-8')) or (path != '/analysis/preview' and values.get('confirm') != ['yes']):
            self.reject_post(403, 'confirmation_required', 'csrf_or_confirmation_invalid')
            return
        jobs = getattr(self.server, 'jobs', None)
        try:
            if jobs and path == '/analysis/preview':
                data = self.index()
                count = int(values['count'][0]) if values.get('count') else None
                identifiers = values.get('lead', [])
                if values.get('all'):
                    identifiers = values['all'][0].split(',')
                if values.get('single'):
                    identifiers = values['single']
                token, preview = jobs.preview_analysis(data['leads'], identifiers=None if count is not None else identifiers,
                    count=count, completed={domain(r['target']) for r in data['reports'] if r['kind'] == 'audit'})
                content = '<h1>Предпросмотр анализа</h1><p><a href="/organizations">← Организации</a></p>'
                content += '<p>Выбрано: ' + str(len(preview['params']['sites'])) + '. Последовательно, один процесс. До 180 секунд на сайт. Реальный предварительный анализ правил: 7 HTML, 2 документа, 16 запросов, 60 секунд сбора; robots.txt учитывается. Без AI, CRM и отправки писем. Контакты с URL-источником движок не экспортирует; исходные контакты сохраняются.</p>'
                for site in preview['params']['sites']:
                    content += '<p>' + esc(site['name']) + ': ' + esc(site['url']) + (' · Повторный анализ' if site['repeat'] else '') + '</p>'
                content += '<h2>Пропуски: ' + str(len(preview['params']['skipped'])) + '</h2>'
                for reason in preview['params']['skipped']:
                    content += '<p>' + esc(reason) + '</p>'
                if preview['params']['sites']:
                    content += self.form('/analysis', 'Запустить анализ', '<input type="hidden" name="preview" value="' + esc(token) + '">')
                self.html(content)
                return
            elif jobs and path == '/analysis':
                destination = '/jobs/' + jobs.submit_analysis(values.get('preview', [''])[0])
            elif path == '/refresh':
                self.catalog().snapshot(force=True)
                destination = '/settings'
            elif jobs and path == '/collect/preview':
                token, preview, command = jobs.preview_parser(values.get('industry', [''])[0], values.get('target', [''])[0], values.get('name', [''])[0])
                errors = jobs.parser.diagnostic(preview['params']['industry'])
                content = '<h1>Предпросмотр сетевого сбора</h1><p>Название: ' + esc(preview['params']['name'] or 'Без названия') + '</p><pre>' + esc(shlex.join(command)) + '</pre>'
                content += '<p>Источник: ' + ('MedAdvisor' if preview['params']['industry'] == '1' else 'Orgpage') + '; вся Россия; N=' + str(preview['params']['target']) + ' пригодных уникальных сайтов. Каталог: ' + esc(jobs.state_dir / preview['id']) + '.</p>'
                content += '<p>До 200 страниц, max(200, 10 × N) карточек и 4 часа. Цель не гарантирована. Parser допускает unknown/probable принадлежность бренду и подтверждённую защитную страницу сайта; это не доказательство юридической принадлежности. Строительство проверяет тематику и конечный домен. Контакты не проверяются звонком или письмом.</p>'
                if errors:
                    content += '<p role="status">Запуск недоступен: ' + esc('; '.join(errors)) + '</p><button disabled>Запустить сбор</button>'
                else:
                    content += self.form('/collect', 'Запустить сетевой сбор', '<input type="hidden" name="preview" value="' + esc(token) + '"><p>Подтверждаю сетевой сбор с указанными ограничениями.</p>')
                self.html(content)
                return
            elif jobs and path == '/collect':
                destination = '/jobs/' + jobs.submit_parser(values.get('preview', [''])[0])
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
