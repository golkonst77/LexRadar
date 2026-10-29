"""Standalone, read-only LexRadar Control Center; Python standard library only."""

import argparse
import json
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from control_organizations import load_organizations
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
<a href="#overview">Обзор</a><a href="#projects">Проекты</a>
<a href="/organizations">Организации</a><a href="#reports">Отчёты</a><a href="#settings">Настройки</a>
</nav>
</header>
<main id="overview">
<span class="badge">● Сервер работает</span>
<h1>Центр управления LexRadar</h1>
<p class="lead">Единая точка входа для будущей работы с проектами и отчётами.
Доступны организации из сохранённых результатов и проверка состояния сервера.</p>
<div class="grid">
<article id="projects"><h2>Проекты</h2><p>Список проектов и их состояние.</p>
<span class="soon">Скоро · раздел в разработке</span></article>
<article id="reports"><h2>Отчёты</h2><p>Просмотр результатов и сохранённых материалов.</p>
<span class="soon">Скоро · раздел в разработке</span></article>
<article id="settings"><h2>Настройки</h2><p>Параметры центра управления.</p>
<span class="soon">Скоро · раздел в разработке</span></article>
</div>
<section class="status" aria-labelledby="status-title">
<h2 id="status-title">Состояние системы</h2>
<p>Локальный интерфейс запущен. Фоновые задачи отключены.</p>
<a href="/health">Проверка доступности</a> · <a href="/api/status">Состояние API</a>
</section>
</main>
<footer>LexRadar Control Center · Локальный режим · Анализ и рассылка не запускаются</footer>
</body>
</html>
"""


def organization_page(content):
    start, tail = PAGE.split('<main id="overview">', 1)
    start = start.replace('href="#', 'href="/#')
    return start + '<main>' + content + '</main>' + tail.split('</main>', 1)[1]


def status_label(value):
    return {"not_confirmed": "Не подтверждён", "unknown": "Не указан"}.get(value, value)


def saved_details(root, record):
    content = '<h2>Проблемы и наблюдения сайта</h2>'
    data = audit_details(root, record)
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


class ControlHandler(BaseHTTPRequestHandler):
    def organizations(self, path):
        records, warnings = load_organizations(getattr(self.server, 'results_dir', Path.cwd()))
        warning = ('<p role="status">Часть файлов недоступна или имеет неподдерживаемый формат: '
                   + str(warnings) + '.</p>') if warnings else ''
        notice = '<p>Внутренние данные. Упоминание организации не подтверждает её роль или нарушение.</p>'
        if path != '/organizations':
            identifier = path.removeprefix('/organizations/')
            record = next((r for r in records if r['id'] == identifier), None)
            if record is None:
                self.respond_json(404, {"error": "not_found"})
                return
            content = '<a href="/organizations">← Организации</a><h1>' + escape(record['name']) + '</h1>'
            content += notice + warning + '<dl>'
            for label, value in (
                ('ИНН', record['inn'] or 'Не указан'),
                ('Сайт', ', '.join(record['websites']) or '—'),
                ('Правовой статус', status_label(record['status'])),
                ('Источник', record['source']),
                ('ID в результате', record['row']['id']),
            ):
                content += '<dt>' + label + '</dt><dd>' + escape(value) + '</dd>'
            content += ('</dl><h2>Сохранённые сведения</h2>'
                        '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">')
            content += escape(json.dumps(record['row'], ensure_ascii=False, indent=2)) + '</pre>'
            content += saved_details(self.server.results_dir, record)
        else:
            params = parse_qs(urlsplit(self.path).query)
            query = params.get('q', [''])[0].strip()
            selected = params.get('status', [''])[0]
            statuses = sorted({r['status'] for r in records})
            try:
                page = max(1, int(params.get('page', ['1'])[0]))
            except ValueError:
                page = 1
            filtered = [r for r in records if (not selected or r['status'] == selected)
                        and query.casefold() in ' '.join(
                            [r['name'], r['inn'], *r['websites']]).casefold()]
            pages = max(1, (len(filtered) + 19) // 20)
            page = min(page, pages)
            content = '<h1>Организации</h1>' + notice + warning
            content += ('<form method="get" action="/organizations"><label>Поиск по названию, сайту, ИНН '
                        '<input name="q" value="' + escape(query, quote=True) + '"></label> '
                        '<label>Правовой статус <select name="status"><option value="">Все</option>')
            for status in sorted(set(statuses + ([selected] if selected else []))):
                content += '<option value="' + escape(status, quote=True) + '"'
                content += (' selected' if status == selected else '') + '>'
                content += escape(status_label(status)) + '</option>'
            content += '</select></label> <button type="submit">Найти</button></form>'
            content += '<p>Найдено: ' + str(len(filtered)) + '</p>'
            if not filtered:
                content += ('<p>Организаций пока нет.</p>' if not records
                            else '<p>По запросу ничего не найдено.</p>')
            else:
                content += ('<div style="overflow-x:auto"><table><thead><tr>'
                            '<th>Название</th><th>Сайт</th><th>ИНН</th>'
                            '<th>Правовой статус</th></tr></thead><tbody>')
                for record in filtered[(page - 1) * 20:page * 20]:
                    content += ('<tr><td><a href="/organizations/' + record['id'] + '">'
                                + escape(record['name']) + '</a></td>')
                    for value in (', '.join(record['websites']) or '—',
                                  record['inn'] or '—', status_label(record['status'])):
                        content += '<td>' + escape(value) + '</td>'
                    content += '</tr>'
                content += '</tbody></table></div>'
            content += ('<nav aria-label="Страницы организаций"><span>Страница '
                        + str(page) + ' из ' + str(pages) + '</span>')
            for target, label in ((page - 1, 'Назад'), (page + 1, 'Далее')):
                if 1 <= target <= pages:
                    link = '/organizations?' + urlencode(
                        {'q': query, 'status': selected, 'page': target})
                    content += '<a href="' + escape(link, quote=True) + '">' + label + '</a>'
            content += '</nav>'
        self.respond(200, organization_page(content).encode('utf-8'), 'text/html; charset=utf-8')

    def document(self, path):
        parts = path.split('/')
        root = self.server.results_dir
        records, _ = load_organizations(root)
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
        self.send_header('Content-Type', 'application/pdf')
        self.send_header('Content-Disposition',
                         disposition + "; filename=proposal.pdf; filename*=UTF-8''"
                         + quote(document['name'], safe=''))
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            self.respond(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/organizations" or path.startswith("/organizations/"):
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
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=Path.cwd(),
                        help="Directory containing saved primary-audit results (default: cwd)")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    with ThreadingHTTPServer(("127.0.0.1", args.port), ControlHandler) as server:
        server.results_dir = args.results_dir.absolute()
        print(f"LexRadar Control Center: http://127.0.0.1:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
