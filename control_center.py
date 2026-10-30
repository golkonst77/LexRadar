"""Local LexRadar Control Center: read-only sources and explicitly confirmed tasks."""

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
from control_leads import DEFAULT_PARSER_DIR, website
from control_results import audit_details, proposals, read_file, rows


PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LexRadar — Центр управления</title>
<style>
:root { color-scheme: light; font-family: Inter, system-ui, -apple-system, sans-serif;
  font-size: 16px; color: #111827; background: #F7F8FA; }
* { box-sizing: border-box; }
body { margin: 0; line-height: 1.6; }
a { color: #2563EB; text-underline-offset: 4px; }
a:hover { color: #1D4ED8; }
:focus-visible { outline: 3px solid #2563EB; outline-offset: 4px; }
.skip-link { position: fixed; top: -100px; left: 16px; z-index: 10;
  padding: 12px 16px; background: #fff; border: 1px solid #2563EB; border-radius: 12px; }
.skip-link:focus { top: 16px; }
header { position: fixed; inset: 0 auto 0 0; width: 248px; padding: 32px 16px;
  background: #fff; border-right: 1px solid #E5E7EB; overflow-y: auto; }
.brand { display: block; margin: 0 12px 32px; color: #111827; font-size: 24px;
  line-height: 1.3; font-weight: 750; letter-spacing: -.5px; }
.brand span { color: #2563EB; }
.brand small { display: block; margin-top: 8px; font-size: 13px; font-weight: 500; letter-spacing: 0; }
header nav { display: flex; flex-direction: column; gap: 8px; }
header nav a { padding: 12px; border-radius: 12px; color: #374151;
  text-decoration: none; font-size: 14px; font-weight: 550; border-left: 3px solid transparent; }
header nav a:hover { background: #F7F8FA; color: #1D4ED8; }
header nav a[aria-current="page"] { background: #EFF6FF; color: #1D4ED8; border-left-color: #2563EB; }
main, footer { width: calc(100% - 248px); max-width: 1440px; margin-left: 248px; padding: 32px 40px; }
main { min-height: calc(100vh - 100px); overflow-wrap: anywhere; }
footer { padding-top: 16px; color: #6B7280; font-size: 13px; }
footer p { border-top: 1px solid #E5E7EB; padding-top: 16px; }
main, article, .grid > *, form, label { min-width: 0; }
h1 { font-size: clamp(26px, 3vw, 36px); line-height: 1.25; letter-spacing: -.6px; margin: 8px 0 16px; }
h2 { font-size: 20px; line-height: 1.4; margin: 28px 0 12px; }
h3 { font-size: 17px; line-height: 1.5; }
p { color: #4B5563; margin: 12px 0; }
small, .soon, .job-note { color: #6B7280; font-size: 13px; }
.lead { max-width: 800px; font-size: 17px; }
.grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; margin: 28px 0; }
article, main > form, main > dl { background: #fff; border: 1px solid #E5E7EB;
  border-radius: 12px; padding: 24px; box-shadow: 0 2px 8px #11182706; }
article + article { margin-top: 16px; }
.grid article { margin: 0; display: flex; flex-direction: column; align-items: flex-start; }
article h2 { margin-top: 0; }
.grid article > .cta, .grid article > button { margin-top: auto; }
.metric { display: block; font-size: 36px; line-height: 1.2; font-weight: 750; color: #111827; margin: 16px 0 8px; }
.progress-card h1 { font-size: clamp(32px, 4vw, 44px); }
.progress-card { margin: 24px 0; border-top: 3px solid #2563EB; }
form { display: flex; flex-wrap: wrap; gap: 16px; align-items: end; margin: 20px 0; }
label { display: flex; flex-direction: column; gap: 8px; color: #374151; font-size: 14px; }
label:has(input[type="checkbox"]) { flex-direction: row; align-items: center; }
input, select, button { font: inherit; max-width: 100%; }
input, select { background: #fff; color: #111827; border: 1px solid #D1D5DB; border-radius: 8px; padding: 10px 12px; min-height: 44px; }
input[type="checkbox"] { accent-color: #2563EB; width: 20px; height: 20px; min-height: 20px; flex-shrink: 0; }
button, .cta { display: inline-block; background: #2563EB; color: #fff; border: 1px solid #2563EB;
  border-radius: 8px; padding: 10px 16px; min-height: 44px; font-weight: 600; text-decoration: none; cursor: pointer; }
button:hover, .cta:hover { background: #1D4ED8; color: #fff; border-color: #1D4ED8; }
button:disabled, input:disabled, select:disabled { opacity: .55; cursor: not-allowed; }
button:disabled:hover { background: #2563EB; border-color: #2563EB; }
.analysis-secondary button, #analysis-selection button, details button, td button, td .cta, .grid article .cta {
  background: #fff; color: #2563EB; border-color: #D1D5DB; }
.analysis-secondary button:hover, #analysis-selection button:hover, details button:hover, td button:hover, td .cta:hover, .grid article .cta:hover { background: #EFF6FF; border-color: #2563EB; }
main:has(> form) > p > .cta { background: #fff; color: #2563EB; border-color: #D1D5DB; }
main:has(> form) > p > .cta:hover { background: #EFF6FF; border-color: #2563EB; }
.grid article:first-child .cta { background: #2563EB; color: #fff; border-color: #2563EB; }
.grid article:first-child .cta:hover { background: #1D4ED8; }
form[action$="/cancel"] { border: 0; border-top: 1px solid #E5E7EB; padding: 20px 0 0; box-shadow: none; background: transparent; }
form[action$="/cancel"] button { color: #DC2626; background: #fff; border-color: #FECACA; }
form[action$="/cancel"] button:hover { background: #FEF2F2; border-color: #DC2626; }
#analysis-selection { padding: 16px; border-color: #BFDBFE; background: #EFF6FF; }
table { width: 100%; border-collapse: collapse; background: #fff; font-size: 14px; }
th, td { text-align: left; padding: 16px; border-bottom: 1px solid #E5E7EB; vertical-align: top; overflow-wrap: anywhere; }
th { background: #F9FAFB; color: #4B5563; font-weight: 600; }
tbody tr:hover { background: #F9FAFB; }
tbody tr:last-child td { border-bottom: 0; }
.table-scroll { max-width: 100%; overflow-x: auto; border: 1px solid #E5E7EB; border-radius: 12px; margin: 20px 0; }
.table-scroll table { min-width: 720px; }
.organizations-table { min-width: 980px; table-layout: fixed; }
.organizations-table th, .organizations-table td { padding: 12px; vertical-align: middle; }
.organizations-table .clip { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.organizations-table .selection-column { width: 48px; }
.organizations-table .name-column { width: auto; }
.organizations-table .site-column { width: 15%; }
.organizations-table .contacts-column { width: 22%; }
.organizations-table .status-column { width: 165px; }
.organizations-table .actions-column { width: 235px; }
.organizations-table .row-actions { display: flex; align-items: center; gap: 8px; }
.organizations-table button { padding: 8px; font-size: 13px; }
.mentions-table { min-width: 1600px; }
.mentions-table th, .mentions-table .short-field { white-space: nowrap; }
.mentions-table .source-column { min-width: 260px; }
.mentions-table .status-column { min-width: 200px; }
.email-value { overflow-wrap: anywhere; }
.qualification, .badge { display: inline-block; padding: 4px 8px; border-radius: 8px; background: #F3F4F6; color: #374151; font-size: 13px; font-weight: 600; }
.qualification.ready, .badge { background: #F0FDF4; color: #166534; border: 1px solid #16A34A; }
.qualification.hold { background: #FFFBEB; color: #92400E; border: 1px solid #F59E0B; }
.qualification.reject { background: #F3F4F6; color: #4B5563; border: 1px solid #D1D5DB; }
.qualification.error { background: #FEF2F2; color: #991B1B; border: 1px solid #DC2626; }
.job-note { display: block; }
details { margin: 16px 0; border: 1px solid #E5E7EB; border-radius: 12px; padding: 16px; background: #fff; }
summary { cursor: pointer; font-weight: 550; color: #374151; }
td details { max-width: 440px; }
pre { padding: 16px; background: #F7F8FA; border-radius: 8px; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 600px; overflow: auto; font-size: 13px; }
dt { font-size: 14px; color: #6B7280; }
dd { margin: 4px 0 16px; overflow-wrap: anywhere; }
[role="status"], [role="alert"] { padding: 12px 16px; border-radius: 8px; background: #EFF6FF; color: #1E40AF; }
[role="alert"] { background: #FEF2F2; color: #991B1B; }
nav { display: flex; flex-wrap: wrap; gap: 16px; }
@media (min-width: 1688px) { main, footer { margin-left: calc(248px + (100% - 1688px) / 2); } }
@media (max-width: 1200px) { .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } main, footer { padding: 28px; } }
@media (max-width: 760px) {
  header { position: static; width: 100%; padding: 16px; border-right: 0; border-bottom: 1px solid #E5E7EB; }
  .brand { margin: 0 0 16px; font-size: 20px; }
  .brand small { display: inline; margin-left: 8px; }
  header nav { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px; }
  header nav a { padding: 8px; }
  main, footer { width: 100%; margin-left: 0; padding: 24px 16px; }
  main { min-height: 0; }
  article, main > form, main > dl { padding: 20px; }
  form > label { width: 100%; }
}
@media (max-width: 540px) { .grid { grid-template-columns: 1fr; } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; } }
</style>
</head>
<body>
<a class="skip-link" href="#overview">Перейти к содержимому</a>
<header>
<div class="brand">Lex<span>Radar</span><small>Control Center</small></div>
<nav aria-label="Разделы центра управления">
<a href="/">Обзор</a><a href="/collect">Сбор организаций</a><a href="/analysis">Проверка сайтов</a>
<a href="/reports">Готовые предложения</a><a href="/crm">Передача в CRM</a><a href="/settings">Служебное</a>
</nav>
</header>
<main id="overview" tabindex="-1">
</main>
<footer><p>LexRadar Control Center · Локальный режим · Сеть — после подтверждения · Рассылка отключена</p></footer>
</body>
</html>
"""


def organization_page(content):
    start, tail = PAGE.split('<main id="overview" tabindex="-1">', 1)
    return start + '<main id="overview" tabindex="-1">' + content + '</main>' + tail.split('</main>', 1)[1]


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
    content += '<h2>Сохранённые PDF · утверждение КП не подтверждено</h2>'
    documents = proposals(root, record)
    if not documents:
        content += '<p>Сохранённых PDF для этого результата нет.</p>'
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
        listing = '/analysis' if urlsplit(self.path).path == '/analysis' else base
        status_title = 'Правовой статус' if mentions else 'Статус обработки'
        data = self.index()
        records = data['mentions' if mentions else 'leads']
        jobs = getattr(self.server, 'jobs', None)
        history = jobs.analysis_history() if jobs and not mentions else {}
        warnings = data['warnings']
        warning = ('<p role="status">Часть файлов недоступна или имеет неподдерживаемый формат: '
                   + str(warnings) + '.</p>') if warnings else ''
        notice = ('<p>Упоминания организаций в аудитах; это не лиды Parser v3.</p>' if mentions else
                  '<p>Выберите организации для анализа или откройте карточку: контакты, квалификация, подробности и ссылки на отчёты. Скрининг не является юридическим аудитом.</p><p><a href="/collect">Собрать организации</a> · <a href="/analysis">Анализ сайтов</a> · <a href="/reports">Все отчёты и PDF</a></p>')
        notice += '<p><a href="/organizations">Организации и контакты</a> · <a href="/mentions">Упоминания в аудитах</a></p>'
        if path != base:
            identifier = path.removeprefix(base + '/')
            record = next((r for r in records if identifier in r.get('aliases', [r['id']])), None)
            if record is None:
                self.respond_json(404, {"error": "not_found"})
                return
            content = '<a href="' + base + '">← Организации</a><h1>' + escape(record['name']) + '</h1>'
            if not mentions and jobs:
                content += self.analysis_form('<input type="hidden" name="lead" value="' + escape(record['id']) + '">')
                content += '<p>Последний анализ: ' + self.analysis_status(record, history) + '</p>'
            content += notice + warning + '<dl>'
            for label, value in (
                ('ИНН', record['inn'] or 'Не указан'),
                ('Сайт', ', '.join(record['websites']) or '—'),
                (status_title, status_label(record['status'])),
                ('Источник', record['source']),
                ('Запуск', record.get('run_name', 'Исторический импорт; запуск панели неизвестен')),
                ('Запись в файле', record.get('record_number', str(record['row'].get('id', '')))),
                ('Телефон', record.get('phone', '') or '—'),
                ('Email', record.get('email', '') or '—'),
                ('Карточка источника / агрегатор', record.get('source_card', '') or '—'),
            ):
                if label == status_title and not record['status']:
                    continue
                content += '<dt>' + label + '</dt><dd>' + (external(value) if label in {'Сайт', 'Карточка источника / агрегатор'} else self.analysis_email(record, history) if label == 'Email' and jobs and not mentions else escape(value)) + '</dd>'
            content += ('</dl><details><summary>Исходные технические сведения</summary>'
                        '<pre style="white-space:pre-wrap;overflow-wrap:anywhere">')
            content += escape(json.dumps(record.get('members', [record['row']]), ensure_ascii=False, indent=2)) + '</pre></details>'
            if mentions:
                content += saved_details(self.server.results_dir, record)
            else:
                related = self.catalog().linked(record, data)
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
            run = params.get('run', [''])[0]
            errors_only = not mentions and params.get('analysis_errors') == ['yes']
            error_ids = self.analysis_errors(records, history) if not mentions else set()
            statuses = sorted({m['status'] for r in records for m in r.get('members', [r]) if m['status']})
            try:
                page = max(1, int(params.get('page', ['1'])[0]))
            except ValueError:
                page = 1
            filtered = [r for r in records if (not run or any(m.get('run_id') == run for m in r.get('members', [r]))) and (not selected or any(m['status'] == selected for m in r.get('members', [r])))
                        and (not errors_only or r['id'] in error_ids) and query.casefold() in ' '.join(
                            [r['name'], r['inn'], *r['websites'], r.get('phone', ''), r.get('email', '')]).casefold()]
            sort = params.get('sort', ['name'])[0]
            if sort == 'source':
                filtered.sort(key=lambda r: (r['source'], r['name'].casefold(), r['id']))
            elif sort == 'name-desc':
                filtered.sort(key=lambda r: (r['name'].casefold(), r['id']), reverse=True)
            pages = max(1, (len(filtered) + 19) // 20)
            page = min(page, pages)
            content = '<h1>' + ('Упоминания в аудитах' if mentions else 'Анализ сайтов' if urlsplit(self.path).path == '/analysis' else 'Результаты') + '</h1>' + notice + warning
            content += ('<form method="get" action="' + listing + '"><label>Поиск по названию, сайту, ИНН, телефону, email '
                        '<input name="q" value="' + escape(query, quote=True) + '"></label> ')
            if statuses or selected:
                content += '<label>' + status_title + ' <select name="status"><option value="">Все</option>'
            for status in sorted(set(statuses + ([selected] if selected else []))):
                content += '<option value="' + escape(status, quote=True) + '"'
                content += (' selected' if status == selected else '') + '>'
                content += escape(status_label(status)) + '</option>'
            if statuses or selected:
                content += '</select></label> '
            if not mentions:
                content += '<label>Запуск <select name="run"><option value="">Все запуски и импорт</option>'
                runs = {m['run_id']: m.get('run_name') for r in records for m in r.get('members', [r]) if m.get('run_id')}
                for key, name in runs.items():
                    content += '<option value="' + key + '"' + (' selected' if run == key else '') + '>' + escape(name) + '</option>'
                content += '</select></label>'
                if run in runs:
                    content += link('/exports/' + run, 'Скачать CSV запуска')
            content += '<label>Сортировка <select name="sort">'
            for key, label in (('name', 'Название А–Я'), ('name-desc', 'Название Я–А'), ('source', 'Источник')):
                content += '<option value="' + key + '"' + (' selected' if sort == key else '') + '>' + label + '</option>'
            content += '</select></label><button type="submit">Найти</button></form>'
            if not mentions and jobs:
                content = content.replace('<button type="submit">Найти</button>',
                    '<label><input type="checkbox" name="analysis_errors" value="yes"'
                    + (' checked' if errors_only else '') + '> Ошибки анализа (' + str(len(error_ids))
                    + ')</label> <button type="submit">Найти</button>', 1)
            content += '<p>Найдено: ' + str(len(filtered)) + '</p>'
            if not mentions and jobs:
                content += '<p><a href="/jobs">Журнал анализа →</a>. Выбор относится только к текущей странице; при переходе сбрасывается. Следующие N выбираются из всего доступного каталога независимо от фильтра.</p>'
                options = '<label>Следующие N необработанных <select name="count">'
                for n in (10, 20, 50, 100, 200):
                    options += '<option' + (' selected' if n == 50 else '') + '>' + str(n) + '</option>'
                content += '<div class="analysis-secondary">' + self.analysis_form(options + '</select></label>', 'Анализировать следующие N') + '</div>'
                current = filtered[(page - 1) * 20:page * 20]
                fields = '<label><input type="checkbox" name="all" value="' + ','.join(r['id'] for r in current) + '"> Выбрать все на текущей странице</label>'
                if errors_only:
                    fields += '<input type="hidden" name="retry_errors" value="yes"><p>Число выбранных для повтора будет показано в предпросмотре перед подтверждением.</p>'
                content += self.analysis_form(fields, 'Повторить анализ ошибок' if errors_only else 'Анализировать выбранные', 'analysis-selection')
            if not filtered:
                content += ('<p>Организаций пока нет.</p>' if not records
                            else '<p>По запросу ничего не найдено.</p>')
            else:
                if not mentions:
                    content += ('<div class="table-scroll" tabindex="0" role="region" aria-label="Организации"><table class="organizations-table"><thead><tr>'
                                '<th class="selection-column"><span title="Выбор организаций">✓</span></th>'
                                '<th class="name-column">Организация</th><th class="site-column">Сайт</th>'
                                '<th class="contacts-column">Контакты</th><th class="status-column">Статус</th>'
                                '<th class="actions-column">Действия</th></tr></thead><tbody>')
                    for record in filtered[(page - 1) * 20:page * 20]:
                        details_url = base + '/' + record['id']
                        badge, report = self.analysis_status(record, history, compact=True) if jobs else ('<span class="qualification">Не анализировался</span>', '')
                        content += '<tr><td>'
                        if jobs:
                            content += '<input aria-label="Выбрать ' + escape(record['name'], quote=True) + '" type="checkbox" form="analysis-selection" name="lead" value="' + record['id'] + '">'
                        content += '</td><td><a class="clip" title="' + escape(record['name'], quote=True) + '" href="' + details_url + '">' + escape(record['name']) + '</a></td><td>'
                        for url in record['websites']:
                            content += '<span class="clip" title="' + escape(url, quote=True) + '">' + (link(url, urlsplit(url).hostname or url) if website(url) else escape(url)) + '</span>'
                        if not record['websites']:
                            content += '—'
                        content += '</td><td>'
                        phone = record.get('phone') or '—'
                        content += '<span class="clip" title="' + escape(phone, quote=True) + '">' + escape(phone) + '</span>'
                        content += self.analysis_email(record, history, compact=True) if jobs else '<span class="clip" title="' + escape(record.get('email') or '—', quote=True) + '">' + escape(record.get('email') or '—') + '</span>'
                        content += '</td><td>' + badge + '</td><td><div class="row-actions">'
                        if jobs:
                            content += '<button form="analysis-selection" name="single" value="' + record['id'] + '">Анализ</button>'
                        content += report + link(details_url, 'Подробности') + '</div></td></tr>'
                    content += '</tbody></table></div>'
                else:
                    content += ('<div class="table-scroll" tabindex="0" role="region" aria-label="Организации"><table class="mentions-table"><thead><tr>'
                                + ('<th>Выбор / анализ</th>' if not mentions and jobs else '') + '<th>Название</th><th>Сайт</th><th>ИНН</th>'
                                '<th>Телефон</th><th>Email</th><th class="source-column">Источник / агрегатор</th>'
                                + ('<th class="status-column">' + status_title + '</th>' if statuses else '') + '</tr></thead><tbody>')
                    for record in filtered[(page - 1) * 20:page * 20]:
                        content += '<tr>'
                        if not mentions and jobs:
                            content += '<td><input aria-label="Выбрать ' + escape(record['name'], quote=True) + '" type="checkbox" form="analysis-selection" name="lead" value="' + record['id'] + '">'
                            content += '<button form="analysis-selection" name="single" value="' + record['id'] + '">Анализ</button><br>' + self.analysis_status(record, history) + '</td>'
                        content += ('<td><a href="' + base + '/' + record['id'] + '">'
                                    + escape(record['name']) + '</a></td>')
                        for column, value in enumerate((', '.join(record['websites']) or '—',
                                      record['inn'] or '—', record.get('phone', '') or '—',
                                      record.get('email', '') or '—', record.get('source_card', '') or '—',
                                      *((status_label(record['status']) or '—',) if statuses else ()))):
                            cell_class = ('', 'short-field', 'short-field', '', 'source-column', 'status-column')[column]
                            content += '<td class="' + cell_class + '">' + (self.analysis_email(record, history) if column == 3 and jobs and not mentions else escape(value)) + '</td>'
                        content += '</tr>'
                    content += '</tbody></table></div>'
            content += ('<nav aria-label="Страницы организаций"><span>Страница '
                        + str(page) + ' из ' + str(pages) + '</span>')
            for target, label in ((page - 1, 'Назад'), (page + 1, 'Далее')):
                if 1 <= target <= pages:
                    page_link = listing + '?' + urlencode(
                        {'q': query, 'status': selected, 'page': target, **({'analysis_errors': 'yes'} if errors_only else {}), **({'run': run} if run else {}), **({'sort': sort} if sort != 'name' else {})})
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
        elif path == '/collect':
            self.collect_page()
        elif path == '/analysis':
            self.organizations('/organizations')
        elif path == '/reports' or path.startswith('/reports/'):
            self.reports(path)
        elif path.startswith('/files/'):
            self.file(path)
        elif path == '/jobs' or path.startswith('/jobs/'):
            self.jobs_page(path)
        elif path.startswith('/analysis-files/'):
            self.analysis_file(path)
        elif path.startswith('/exports/'):
            self.export(path.removeprefix('/exports/'))
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
                "mode": "confirmed_tasks" if hasattr(self.server, "jobs") else "read_only",
                "analysis_enabled": hasattr(self.server, "jobs"),
                "mailing_enabled": False,
            })
        else:
            self.respond_json(404, {"error": "not_found"})

    def respond_json(self, status, payload):
        self.respond(status, json.dumps(payload).encode("utf-8"),
                     "application/json; charset=utf-8")

    def respond(self, status, body, content_type):
        if content_type.startswith('text/html'):
            page = body.decode('utf-8')
            marker = '<nav aria-label="Разделы центра управления">'
            start = page.find(marker)
            end = page.find('</nav>', start)
            if start >= 0 and end >= 0:
                path = urlsplit(self.path).path
                section = next((route for route in ('/collect', '/analysis', '/proposals', '/crm')
                                if path == route or path.startswith(route + '/')), None)
                section = section or ('/' if path == '/' else '/service')
                nav = page[start:end]
                if path == '/reports' and 'href="/proposals"' not in nav:
                    section = '/reports'
                if section == '/service' and 'href="/service"' not in nav:
                    section = '/settings'
                nav = nav.replace('href="' + section + '"', 'href="' + section + '" aria-current="page"')
                page = page[:start] + nav + page[end:]
                body = page.encode('utf-8')
        self.send_response(status)
        self.write_response(body, content_type)

    def write_response(self, body, content_type):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if getattr(self, "refresh_seconds", None):
            self.send_header("Refresh", str(self.refresh_seconds))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
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
