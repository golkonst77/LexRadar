"""Local LexRadar Control Center: read-only sources and fixed offline checks."""

import argparse
import json
import secrets
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from control_pages import Pages, external, link
from control_catalog import Catalog
from control_jobs import Jobs
from control_leads import DEFAULT_PARSER_DIR
from control_results import audit_details, proposals, read_file, rows


PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LexRadar — Центр управления</title>
<style>
:root { color-scheme: dark; font-family: system-ui, sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; background: #0c111b; color: #e7edf7; line-height: 1.6; }
a { color: #a5c7ff; text-underline-offset: 4px; }
input, select, button { font: inherit; padding: 6px; max-width: 100%; }
th, td { text-align: left; padding: 10px; border-bottom: 1px solid #29364d; }
table { width: 100%; }
.table-scroll { overflow-x: auto; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 600px; overflow: auto; }
article + article { margin-top: 16px; }
form { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; margin: 20px 0; }
button { cursor: pointer; }
@media (max-width: 600px) { main, header, footer { padding: 16px; } td, th { padding: 6px; } }
dd { margin-bottom: 12px; overflow-wrap: anywhere; }
a:focus-visible { outline: 2px solid #8dbbff; outline-offset: 5px; }
header, main, footer { max-width: 1100px; margin: auto; padding: 24px; }
header { display: flex; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 20px; border-bottom: 1px solid #273248; }
.brand { font-size: 22px; font-weight: 750; letter-spacing: -.5px; }
.brand span { color: #84b4ff; }
nav { display: flex; gap: 20px; flex-wrap: wrap; }
nav a { font-size: 14px; }
main { padding-top: 56px; }
h1 { font-size: clamp(30px, 5vw, 48px); line-height: 1.2; margin: 16px 0; }
h2 { font-size: 20px; margin-top: 0; }
p { color: #aab8cc; }
.badge { display: inline-block; background: #15352e; color: #9ae2c2;
  padding: 5px 12px; border-radius: 20px; font-size: 13px; }
.lead { max-width: 680px; font-size: 18px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 18px; margin: 36px 0; }
article { background: #141d2d; border: 1px solid #29364d; border-radius: 14px;
  padding: 24px; scroll-margin-top: 24px; }
.soon { color: #b4c5df; font-size: 13px; }
.status { border-top: 1px solid #273248; padding-top: 24px; }
footer { color: #8c9bb1; font-size: 13px; padding-bottom: 40px; }
</style>
</head>
<body>
<header>
<div class="brand">Lex<span>Radar</span> / Control Center</div>
<nav aria-label="Разделы центра управления">
<a href="/">Обзор</a><a href="/organizations">Организации</a><a href="/reports">Отчёты</a>
<a href="/jobs">Задания</a><a href="/crm">CRM preview</a><a href="/settings">Настройки</a>
</nav>
</header>
<main id="overview">
</main>
<footer>LexRadar Control Center · Локальный режим · Анализ и рассылка не запускаются</footer>
</body>
</html>
"""


def organization_page(content):
    start, tail = PAGE.split('<main id="overview">', 1)
    return start + '<main>' + content + '</main>' + tail.split('</main>', 1)[1]


def status_label(value):
    return {"not_confirmed": "Не подтверждён", "unknown": "Не указан"}.get(value, value)


def saved_details(root, record):
    content = '<h2>Проблемы и наблюдения сайта</h2>'
    data = audit_details(root, record)
    if data:
        content = '<h2>Сохранённый анализ сайта</h2><p>Это предварительные наблюдения и гипотезы, требующие проверки.</p><dl>'
        for label, key in (('Начало сбора', 'source_started_at'), ('Дата оценки', 'assessed_at'),
                           ('Правовой статус', 'legal_status'), ('Результат', 'production_outcome'),
                           ('Полнота проверки', 'coverage'), ('Ограничения', 'limitations')):
            if key in data:
                content += '<dt>' + label + '</dt><dd>' + escape(str(data[key])) + '</dd>'
        content += '</dl><h2>Стадии проверки</h2>'
        for stage in rows(data, 'stages'):
            content += '<p>' + escape(str(stage.get('title', stage.get('stage_id', '')))) + ': ' + escape(str(stage.get('status', 'not_checked'))) + '</p>'
        content += '<h2>Проблемы и наблюдения сайта</h2>'
    observations = rows(data or {}, 'observations')
    hypotheses = rows(data or {}, 'hypotheses')
    if not observations and not hypotheses:
        content += '<p>Сохранённых проблем и наблюдений нет. Это не подтверждает отсутствие проблем.</p>'
    else:
        content += ('<p>Данные сайта из этого результата; принадлежность каждой проблемы '
                    'организации не установлена. Юридическое нарушение не подтверждено.</p>')
        stages = {str(r.get('stage_id')): r for r in rows(data, 'stages')}
        indexed = {str(r.get('id')): r for r in observations}
        for item in observations + hypotheses:
            stage = stages.get(str(item.get('stage_id')), {})
            title = item.get('description') or item.get('question') or item.get('id', 'Наблюдение')
            content += '<article><h3>' + escape(str(title)) + '</h3>'
            for label, value in (
                ('Этап', stage.get('title', item.get('stage_id', 'Не указан'))),
                ('Статус проверки этапа', stage.get('status', 'not_checked')),
                ('Статус гипотезы', item.get('status')),
                ('Область проверки', item.get('scope') or stage.get('status_scope')),
                ('Недостающие сведения', item.get('missing_information')),
                ('Требуемые проверки', item.get('required_checks')),
                ('Ограничения', stage.get('limitations')),
            ):
                if value:
                    content += '<p>' + label + ': ' + escape(str(value)) + '</p>'
            evidence = [item] if 'description' in item else []
            references = item.get('observation_ids', [])
            if isinstance(references, list):
                evidence += [indexed[i] for i in references if isinstance(i, str) and i in indexed]
            content += '<h4>Подтверждения из результата</h4>'
            citations = [c for observation in evidence for c in rows(observation, 'citations')]
            if not citations:
                content += '<p>Подтверждения не сохранены.</p>'
            for citation in citations:
                source_url = citation.get('url') or citation.get('source_url')
                if isinstance(source_url, str):
                    content += '<p>Источник: ' + external(source_url) + '</p>'
                content += ('<pre style="white-space:pre-wrap;overflow-wrap:anywhere">'
                            + escape(json.dumps(citation, ensure_ascii=False, indent=2)) + '</pre>')
            content += '</article>'
    checks = rows(data or {}, 'missing_checks')
    if checks:
        content += '<h3>Невыполненные проверки сайта</h3>'
        for check in checks:
            content += ('<p>' + escape(str(check.get('question', ''))) + ' — '
                        + escape(str(check.get('state', 'not_performed'))) + '</p>')
    content += '<h2>Готовые PDF-коммерческие предложения</h2>'
    documents = proposals(root, record)
    if not documents:
        content += '<p>Готовых PDF-коммерческих предложений для этого результата нет.</p>'
    for document in documents:
        link = '/documents/' + record['id'] + '/' + document['id']
        content += ('<p>' + escape(document['name']) + ' · <a href="' + link
                    + '">Открыть PDF</a> · <a href="' + link
                    + '?download=1">Скачать PDF</a></p>')
    return content


class ControlHandler(Pages, BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        if hasattr(self.connection, 'settimeout'):
            self.connection.settimeout(10)

    def html(self, content, status=200):
        self.respond(status, organization_page(content).encode('utf-8'), 'text/html; charset=utf-8')

    def audit_content(self, record):
        content = saved_details(self.server.results_dir, record)
        return content.replace('/documents/' + record['id'] + '/', '/files/')

    def organizations(self, path):
        mentions = path.startswith('/mentions')
        base = '/mentions' if mentions else '/organizations'
        status_title = 'Правовой статус' if mentions else 'Статус обработки'
        data = self.index()
        records = data['mentions' if mentions else 'leads']
        warnings = data['warnings']
        warning = ('<p role="status">Часть файлов недоступна или имеет неподдерживаемый формат: '
                   + str(warnings) + '.</p>') if warnings else ''
        notice = ('<p>Упоминания организаций в аудитах; это не лиды Parser v3.</p>' if mentions else
                  '<p>Лиды Parser v3. Контакты из экспорта; скрининг не является юридическим аудитом.</p>')
        notice += '<p><a href="/organizations">Лиды Parser v3</a> · <a href="/mentions">Упоминания в аудитах</a></p>'
        if path != base:
            identifier = path.removeprefix(base + '/')
            record = next((r for r in records if r['id'] == identifier), None)
            if record is None:
                self.respond_json(404, {"error": "not_found"})
                return
            content = '<a href="' + base + '">← Организации</a><h1>' + escape(record['name']) + '</h1>'
            content += notice + warning + '<dl>'
            for label, value in (
                ('ИНН', record['inn'] or 'Не указан'),
                ('Сайт', ', '.join(record['websites']) or '—'),
                (status_title, status_label(record['status'])),
                ('Источник', record['source']),
                ('Запись в файле', record.get('record_number', str(record['row'].get('id', '')))),
                ('Телефон', record.get('phone', '') or '—'),
                ('Email', record.get('email', '') or '—'),
                ('Карточка источника / агрегатор', record.get('source_card', '') or '—'),
            ):
                if label == status_title and not record['status']:
                    continue
                content += '<dt>' + label + '</dt><dd>' + (external(value) if label in {'Сайт', 'Карточка источника / агрегатор'} else escape(value)) + '</dd>'
            content += ('</dl><h2>Сохранённые сведения</h2>'
                        '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">')
            content += escape(json.dumps(record['row'], ensure_ascii=False, indent=2)) + '</pre>'
            if mentions:
                content += saved_details(self.server.results_dir, record)
            else:
                related = self.catalog().linked(record)
                content += '<h2>Анализы сайта и PDF</h2>'
                content += '<p>Связь только по нормализованному домену; юридическое лицо и адресат PDF требуют проверки.</p>'
                if not related:
                    content += '<p>Подтверждённой связи с аудитом и PDF нет. Не связано либо связь неоднозначна.</p>'
                for result in related:
                    content += '<p>' + link('/reports/' + result['id'], result['source']) + '</p>'
                if record.get('import_file_date'):
                    content += '<p>Дата файла импорта UTC: ' + escape(record['import_file_date']) + '</p>'

        else:
            params = parse_qs(urlsplit(self.path).query)
            query = params.get('q', [''])[0].strip()
            selected = params.get('status', [''])[0]
            statuses = sorted({r['status'] for r in records if r['status']})
            try:
                page = max(1, int(params.get('page', ['1'])[0]))
            except ValueError:
                page = 1
            filtered = [r for r in records if (not selected or r['status'] == selected)
                        and query.casefold() in ' '.join(
                            [r['name'], r['inn'], *r['websites'], r.get('phone', ''), r.get('email', '')]).casefold()]
            sort = params.get('sort', ['name'])[0]
            if sort == 'source':
                filtered.sort(key=lambda r: (r['source'], r['name'].casefold(), r['id']))
            elif sort == 'name-desc':
                filtered.sort(key=lambda r: (r['name'].casefold(), r['id']), reverse=True)
            pages = max(1, (len(filtered) + 19) // 20)
            page = min(page, pages)
            content = '<h1>' + ('Упоминания в аудитах' if mentions else 'Лиды Parser v3') + '</h1>' + notice + warning
            content += ('<form method="get" action="' + base + '"><label>Поиск по названию, сайту, ИНН, телефону, email '
                        '<input name="q" value="' + escape(query, quote=True) + '"></label> ')
            if statuses or selected:
                content += '<label>' + status_title + ' <select name="status"><option value="">Все</option>'
            for status in sorted(set(statuses + ([selected] if selected else []))):
                content += '<option value="' + escape(status, quote=True) + '"'
                content += (' selected' if status == selected else '') + '>'
                content += escape(status_label(status)) + '</option>'
            if statuses or selected:
                content += '</select></label> '
            content += '<label>Сортировка <select name="sort">'
            for key, label in (('name', 'Название А–Я'), ('name-desc', 'Название Я–А'), ('source', 'Источник')):
                content += '<option value="' + key + '"' + (' selected' if sort == key else '') + '>' + label + '</option>'
            content += '</select></label><button type="submit">Найти</button></form>'
            content += '<p>Найдено: ' + str(len(filtered)) + '</p>'
            if not filtered:
                content += ('<p>Организаций пока нет.</p>' if not records
                            else '<p>По запросу ничего не найдено.</p>')
            else:
                content += ('<div style="overflow-x:auto"><table><thead><tr>'
                            '<th>Название</th><th>Сайт</th><th>ИНН</th>'
                            '<th>Телефон</th><th>Email</th><th>Источник / агрегатор</th>'
                            + ('<th>' + status_title + '</th>' if statuses else '') + '</tr></thead><tbody>')
                for record in filtered[(page - 1) * 20:page * 20]:
                    content += ('<tr><td><a href="' + base + '/' + record['id'] + '">'
                                + escape(record['name']) + '</a></td>')
                    for value in (', '.join(record['websites']) or '—',
                                  record['inn'] or '—', record.get('phone', '') or '—',
                                  record.get('email', '') or '—', record.get('source_card', '') or '—',
                                  *((status_label(record['status']) or '—',) if statuses else ())):
                        content += '<td>' + escape(value) + '</td>'
                    content += '</tr>'
                content += '</tbody></table></div>'
            content += ('<nav aria-label="Страницы организаций"><span>Страница '
                        + str(page) + ' из ' + str(pages) + '</span>')
            for target, label in ((page - 1, 'Назад'), (page + 1, 'Далее')):
                if 1 <= target <= pages:
                    page_link = base + '?' + urlencode(
                        {'q': query, 'status': selected, 'page': target, **({'sort': sort} if sort != 'name' else {})})
                    content += '<a href="' + escape(page_link, quote=True) + '">' + label + '</a>'
            content += '</nav>'
        self.respond(200, organization_page(content).encode('utf-8'), 'text/html; charset=utf-8')

    def document(self, path):
        parts = path.split('/')
        root = self.server.results_dir
        records = self.index()['mentions']
        record = next((r for r in records if len(parts) == 4 and r['id'] == parts[2]), None)
        documents = proposals(root, record) if record else []
        document = next((d for d in documents if d['id'] == parts[3]), None)
        if document:
            try:
                raw = read_file(root, document['relative'], 50_000_000)
                if not raw.startswith(b'%PDF-'):
                    raise ValueError('Invalid PDF')
            except (OSError, ValueError):
                document = None
        if not document:
            self.respond_json(404, {"error": "not_found"})
            return
        download = parse_qs(urlsplit(self.path).query).get('download') == ['1']
        disposition = 'attachment' if download else 'inline'
        self.send_response(200)
        self.send_header('Content-Disposition',
                         disposition + "; filename=proposal.pdf; filename*=UTF-8''"
                         + quote(document['name'], safe=''))
        self.write_response(raw, 'application/pdf')

    def do_GET(self):
        if not self.valid_local_request():
            self.respond_json(403, {"error": "local_host_required"})
            return
        path = urlsplit(self.path).path
        if path == "/":
            self.home()
        elif path == '/reports' or path.startswith('/reports/'):
            self.reports(path)
        elif path.startswith('/files/'):
            self.file(path)
        elif path == '/jobs' or path.startswith('/jobs/'):
            self.jobs_page(path)
        elif path == '/settings':
            self.settings()
        elif path == '/crm':
            self.crm()
        elif path in {'/organizations', '/mentions'} or path.startswith(('/organizations/', '/mentions/')):
            self.organizations(path)
        elif path.startswith("/documents/"):
            self.document(path)
        elif path == "/health":
            self.respond_json(200, {"status": "ok"})
        elif path == "/api/status":
            self.respond_json(200, {
                "service": "LexRadar Control Center",
                "status": "ok",
                "mode": "read_only",
                "analysis_enabled": False,
                "mailing_enabled": False,
            })
        else:
            self.respond_json(404, {"error": "not_found"})

    def respond_json(self, status, payload):
        self.respond(status, json.dumps(payload).encode("utf-8"),
                     "application/json; charset=utf-8")

    def respond(self, status, body, content_type):
        self.send_response(status)
        self.write_response(body, content_type)

    def write_response(self, body, content_type):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=Path.cwd(),
                        help="Directory containing saved primary-audit results (default: cwd)")
    parser.add_argument("--parser-dir", type=Path, default=DEFAULT_PARSER_DIR,
                        help="Directory containing Parser v3 CSV/JSON exports")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    with ThreadingHTTPServer(("127.0.0.1", args.port), ControlHandler) as server:
        server.results_dir = args.results_dir.absolute()
        server.parser_dir = args.parser_dir.absolute()
        server.state_dir = Path(__file__).resolve().parent / '.control-center-state'
        server.csrf_token = secrets.token_urlsafe(32)
        server.catalog = Catalog(server.results_dir, server.parser_dir)
        server.jobs = Jobs(server.state_dir, server.parser_dir, server.results_dir)
        print(f"LexRadar Control Center: http://127.0.0.1:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.jobs.close()


if __name__ == "__main__":
    main()
