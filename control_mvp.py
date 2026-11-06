"""Four-step Control Center using the existing queue, engines and saved data."""
import argparse
import fcntl
import hmac
import json
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from collections import Counter
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from control_center import ControlHandler, organization_page
from control_catalog import Catalog, domain
from control_jobs import Jobs
from control_leads import DEFAULT_PARSER_DIR, analysis_rows, merge_leads
from control_analysis import target
from control_pages import esc, link, crm_status, crm_entity_links
from control_enrichment import observation
from control_results import read_file
from control_mailing import Mailing
from control_followup import Followup, ID


NAV = '''<a href="/">Обзор</a><a href="/collect">Сбор организаций</a>
<a href="/analysis">Проверка сайтов</a><a href="/proposals">Готовые предложения</a>
<a href="/crm">Передача в CRM</a><a href="/mailing">Рассылка</a>
<a href="/followup">Контроль после отправки</a><a href="/service">Служебное</a>'''


def hold_category(row):
    reason = row['saved']['reason']
    if reason == 'no_confirmed_triggers_1_2_3_5':
        return 'evidence'
    if reason == 'finding_reliability_requires_review':
        return 'review'
    if reason == 'review_run_mismatch':
        return 'identity'
    if row['saved']['status'] == 'SKIPPED' and reason:
        return 'technical'
    return 'unknown'


def analysis_summary(records, history, completed=()):
    """Read-only partition; the queue remains the authority for submission."""
    counts = Counter()
    hosts = set()
    for record in records:
        for member in record.get('members', [record]):
            counts['collected'] += 1
            try:
                domains = {target(url)[1] for url in member['websites']}
                if len(domains) != 1:
                    raise ValueError()
            except ValueError:
                counts['invalid'] += 1
                continue
            host = next(iter(domains))
            if host in hosts:
                counts['duplicates'] += 1
            hosts.add(host)
    active = {key[1] for key, row in history.items()
              if row['status'] in {'pending', 'queued', 'running'}}
    excluded = set(completed) | {key[1] for key in history}
    counts['unique'] = len(hosts)
    counts['active'] = len(hosts & active)
    counts['checked'] = len((hosts & excluded) - active)
    counts['available'] = len(hosts - excluded)
    available = set()
    seen = set(excluded)
    for record in sorted(records, key=lambda r: (r['name'].casefold(), r['id'])):
        try:
            domains = {target(url)[1] for url in record['websites']}
        except ValueError:
            continue
        if len(domains) == 1 and not domains & seen:
            available.add(record['id'])
            seen.update(domains)
    return counts, available


def analysis_summary_html(counts):
    content = '<article><h2>Почему организаций больше, чем заданий</h2><dl>'
    for key, label in (('collected', 'Собрано строк организаций в выбранном списке'),
                       ('invalid', 'Сайт отсутствует, недопустим или указаны разные домены'),
                       ('duplicates', 'Повторяющиеся домены: лишние строки'),
                       ('unique', 'Уникальных допустимых доменов'),
                       ('active', 'В очереди / работе'),
                       ('checked', 'Уже проверялись ранее или есть сохранённый аудит'),
                       ('available', 'Новых сайтов доступно к запуску')):
        content += '<dt>' + label + '</dt><dd><strong>' + str(counts[key]) + '</strong></dd>'
    content += '</dl><p>' + str(counts['collected']) + ' = ' + str(counts['invalid']) + ' без однозначного сайта + ' + str(counts['duplicates']) + ' повторов + ' + str(counts['unique']) + ' уникальных доменов.</p>'
    content += '<p>' + str(counts['unique']) + ' = ' + str(counts['active']) + ' активных + ' + str(counts['checked']) + ' ранее проверявшихся + ' + str(counts['available']) + ' новых.</p>'
    return content + '<p>Приоритет: сначала недопустимые адреса, затем повторы; для уникального домена — активное задание, затем любая прежняя попытка (включая ошибку или отмену) / аудит, затем новый сайт. Каждая строка и каждый домен учтены один раз. «Проверить все новые» берёт до 200; выбранная часть может быть меньше. Перед подтверждением состав проверяется повторно.</p></article>'


class MVPJobs(Jobs):
    def preview_analysis(self, records, identifiers=None, count=None, completed=(), retry_errors=False):
        if retry_errors:
            return super().preview_analysis(records, identifiers, count=count, completed=completed, retry_errors=True)
        if count is not None and (type(count) is not int or not 1 <= count <= 200):
            raise ValueError('Количество сайтов должно быть от 1 до 200')
        if identifiers is not None and (not identifiers or len(identifiers) > 200
                                       or not set(identifiers) <= {r['id'] for r in records}):
            raise ValueError('Выберите от 1 до 200 организаций из текущего списка')
        with self.lock:
            excluded = set(completed) | {key[1] for key in self.analysis_history()}
            candidates, seen = [], set()
            for record in sorted(records, key=lambda r: (r['name'].casefold(), r['id'])):
                if identifiers is not None and record['id'] not in identifiers:
                    continue
                try:
                    hosts = {target(url)[1] for url in record['websites']}
                except ValueError:
                    continue
                if len(hosts) != 1 or hosts & (excluded | seen):
                    continue
                candidates.append(record)
                seen.update(hosts)
                if count is not None and len(candidates) >= count:
                    break
            if not candidates:
                raise ValueError('Нет новых сайтов для анализа: сайты уже проверены, находятся в очереди или недоступны. Для ошибок используйте отдельный повтор.')
            token, preview = super().preview_analysis(candidates, completed=completed)
            preview['new_only'] = True
            return token, preview

    def submit_analysis(self, token):
        with self.lock:
            preview = self.previews.get(token, {})
            if preview.get('new_only'):
                done = {key[1] for key in self.analysis_history()}
                if any(site['domain'] in done for site in preview['params']['sites']):
                    raise ValueError('Сайт уже проверен; обновите предпросмотр')
            return super().submit_analysis(token)


class MVPHandler(ControlHandler):
    def index(self):
        data = super().index()
        if urlsplit(self.path).path in {'/analysis', '/analysis/preview'}:
            run = parse_qs(urlsplit(self.path).query).get('run', [''])[0]
            if run:
                members = [m for r in data['leads'] for m in r.get('members', [r])
                           if m.get('run_id') == run]
                data = {**data, 'leads': merge_leads(members)}
        return data

    def analysis_form(self, fields, label='Анализ', identifier=''):
        form = super().analysis_form(fields, label, identifier)
        run = parse_qs(urlsplit(self.path).query).get('run', [''])[0]
        if run:
            form = form.replace('action="/analysis/preview"',
                                'action="/analysis/preview?' + esc(urlencode({'run': run})) + '"')
        return form

    def respond(self, status, body, content_type):
        if content_type.startswith('text/html'):
            page = body.decode('utf-8')
            start = page.find('<nav aria-label="Разделы центра управления">')
            end = page.find('</nav>', start)
            if start >= 0 and end >= 0:
                nav = NAV
                page = page[:start] + '<nav aria-label="Разделы центра управления">' + nav + page[end:]
            start = page.find('<footer>')
            end = page.find('</footer>', start)
            if start >= 0 and end >= 0:
                page = page[:start] + '<footer><p>Локальная панель · Статус рассылки — в разделе «Рассылка»</p>' + page[end:]
            body = page.encode('utf-8')
        super().respond(status, body, content_type)

    def workflow(self):
        jobs = getattr(self.server, 'jobs', None)
        if jobs:
            with jobs.lock:
                return self._workflow()
        return self._workflow()

    def _workflow(self):
        data = self.index()
        jobs = getattr(self.server, 'jobs', None)
        history = jobs.analysis_history() if jobs else {}
        counts = Counter()
        offers = []
        holds = []
        from control_offers import current_offers, verified
        current = current_offers(jobs) if jobs else {}
        seen = set()
        for lead in data['leads']:
            try:
                _, host = target(next(iter(lead['websites']), ''))
            except ValueError:
                continue
            if host in seen:
                continue
            seen.add(host)
            counts['usable'] += 1
            attempts = analysis_rows(lead, history)
            if attempts:
                counts['checked'] += any(r['status'] in {'succeeded', 'failed', 'cancelled'} for r in attempts)
                counts['active'] += attempts[0]['status'] in {'pending', 'queued', 'running'}
                job = attempts[0]
                host = target(lead['websites'][0])[1]
                saved = observation(jobs.state_dir, job, host)
                if job['status'] == 'succeeded':
                    counts[saved['qualification'] or 'HOLD'] += 1
                    if (saved['qualification'] or 'HOLD') == 'HOLD':
                        holds.append(dict(lead=lead, job=job, host=host, saved=saved))
                elif job['status'] in {'failed', 'cancelled'}:
                    counts['ERROR'] += 1
                latest_status = saved['qualification']
                active = current.get(host)
                if not active or not verified(jobs, active):
                    continue
                job = jobs.get(active['job_id'])
                saved = observation(jobs.state_dir, job, host)
                base = job['id'] + '/analysis/sites/row-000002/'
                try:
                    pdf = read_file(jobs.state_dir, base + 'proposal.pdf', 8_000_000, prefix=5)
                    if not pdf.startswith(b'%PDF-'):
                        continue
                    review = json.loads(read_file(jobs.state_dir, base + 'review.json', 1_000_000))
                except (OSError, ValueError, TypeError):
                    continue
                from src.lexradar.proposal_rules import BLOCKS
                basis = [BLOCKS.get(f['rule_id'], {}).get('title', 'Подтверждённый признак №' + str(f['rule_id']))
                         for f in review['findings']]
                offers.append(dict(lead=lead, job=job, saved=saved, base=base, basis=basis, latest_status=latest_status))
        counts['READY'] = len(offers)
        counts['crm_ready'] = sum(bool(o['lead'].get('phone') or o['lead'].get('email')
                                      or o['saved']['contacts']) for o in offers)
        data['holds'] = holds
        data['offers'] = offers
        return data, history, counts

    def organizations(self, path):
        if urlsplit(self.path).path != '/analysis':
            return super().organizations(path)
        data, history, counts = self.workflow()
        params = parse_qs(urlsplit(self.path).query)
        run = params.get('run', [''])[0]
        errors_only = params.get('analysis_errors') == ['yes']
        errors = self.analysis_errors(data['leads'], history)
        completed = {domain(r['target']) for r in data['reports'] if r['kind'] == 'audit'}
        summary, available = analysis_summary(data['leads'], history, completed)
        checked = {r['id'] for r in data['leads'] if analysis_rows(r, history)
                   or any(domain(url) in completed for url in r['websites'])}
        content = '<h1>' + ('Ошибки анализа' if errors_only else '2. Проверить сайты') + '</h1>'
        if run:
            content += '<p>Организации запуска: ' + esc(run) + '</p>'
        content += '<p>Область: ' + ('выбранный запуск сбора; история проверок — по всей базе.' if run else 'вся доступная база и исторический импорт.') + '</p>'
        if data.get('warnings'):
            content += '<p role="status">Часть исходных файлов недоступна. Расчёт относится только к загруженным строкам; это не полный итог сбора.</p>'
        content += analysis_summary_html(summary)
        content += '<p>Строк после объединения по домену: ' + str(len(data['leads'])) + ' · READY: ' + str(counts['READY'])
        content += ' · REJECT: ' + str(counts['REJECT']) + ' · HOLD: ' + str(counts['HOLD']) + '</p>'
        content += '<p>' + link('/analysis', 'Вся база организаций') + ' · ' + link('/analysis?' + urlencode(dict(analysis_errors='yes', **({'run': run} if run else {}))), 'Ошибки анализа (' + str(len(errors)) + ')') + '</p>'
        if not errors_only and available:
            content += self.analysis_form('<input type="hidden" name="count" value="200">', 'Проверить все новые (до 200 за раз)')
            content += '<details><summary>Проверить часть новых сайтов</summary>' + self.analysis_form('<label>Количество <input type="number" name="count" min="1" max="200" value="10" required></label>', 'Подготовить проверку') + '</details>'
        records = [r for r in data['leads'] if not errors_only or r['id'] in errors]
        if errors_only and records:
            content += self.analysis_form('<input type="hidden" name="retry_errors" value="yes"><input type="hidden" name="all" value="' + ','.join(r['id'] for r in records[:200]) + '">', 'Повторить ошибки (до 200)')
            content += '<p>Без парсинга и повторения успешных сайтов. Перед запуском будет предпросмотр.</p>'
        if records:
            if errors_only or available:
                content += self.analysis_form('<input type="hidden" name="retry_errors" value="' + ('yes' if errors_only else 'no') + '">', 'Проверить выбранные', 'analysis-selection')
            content += '<div class="table-scroll" tabindex="0" role="region" aria-label="Список организаций"><table><thead><tr><th>Выбор</th><th>Организация</th><th>Сайт</th><th>Статус</th></tr></thead><tbody>'
            for record in records[:200]:
                badge, report = self.analysis_status(record, history, compact=True)
                if record['id'] in checked and not analysis_rows(record, history):
                    badge = '<span class="qualification">Уже проверен (сохранённый аудит)</span>'
                content += '<tr><td><input type="checkbox" form="analysis-selection" name="lead" value="' + esc(record['id']) + ('" disabled' if not errors_only and record['id'] not in available else '"') + ' aria-label="Выбрать ' + esc(record['name']) + '"></td><td>' + link('/organizations/' + record['id'], record['name']) + '</td><td>'
                content += '<br>'.join(link(url, urlsplit(url).hostname or url) for url in record['websites'] if url)
                content += '</td><td>' + badge + ' ' + report + '</td></tr>'
            content += '</tbody></table></div><p>Показано ' + str(min(200, len(records))) + ' из ' + str(len(records)) + '. ' + link('/organizations', 'Все организации и фильтры') + '</p>'
        else:
            content += '<p>' + ('Ошибок для повтора нет.' if errors_only else 'Сначала соберите организации.') + '</p>'
        content += '<p><a class="cta" href="/proposals">Посмотреть готовые предложения →</a></p><p>' + link('/collect', '← Собрать организации') + ' · ' + link('/jobs', 'Прогресс заданий') + '</p>'
        if summary['active']:
            self.refresh_seconds = 5
        self.html(content)

    def execution_progress(self, row):
        jobs = self.server.jobs
        active = row['status'] in {'pending', 'queued', 'running'}
        params, result = json.loads(row['params']), json.loads(row['result'])
        if active:
            self.refresh_seconds = 5
        start = datetime.fromisoformat(row['started'] or row['created'])
        end = datetime.fromisoformat(row['ended']) if row['ended'] else datetime.now(timezone.utc)
        seconds = max(0, int((end - start).total_seconds()))
        elapsed = str(seconds // 60) + ' мин ' + str(seconds % 60) + ' с'
        content = '<style>.activity{display:inline-block;width:1em;height:1em;border:3px solid #ddd;border-top-color:#2875b9;border-radius:50%;animation:activity 1s linear infinite;vertical-align:middle}@keyframes activity{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.activity{animation:none}}</style>'
        if row['kind'] == 'parser-v3':
            reviewed, usable = result.get('cards_reviewed'), result.get('usable')
            # Read committed decisions only; the parser remains the sole writer.
            sequential = params.get('industry') == '1'
            if sequential or reviewed is None or usable is None:
                database = jobs.state_dir / ('medadvisor-sequential' if sequential else row['id']) / 'parser.sqlite'
                if sequential:
                    reviewed = usable = None
                try:
                    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as db:
                        if sequential:
                            # Parser run IDs are local to SQLite; bind by the job's execution interval.
                            runs = db.execute(
                                'SELECT id FROM runs WHERE julianday(started_at) >= julianday(?) '
                                'AND julianday(started_at) <= julianday(?) LIMIT 2',
                                (row['started'], end.isoformat())).fetchall() if row['started'] else []
                            if len(runs) == 1:
                                reviewed, usable = db.execute(
                                    'SELECT count(*), coalesce(sum(accepted), 0) FROM collection_decisions WHERE run_id=?',
                                    (runs[0][0],)).fetchone()
                        else:
                            reviewed, usable = db.execute('SELECT count(*), coalesce(sum(accepted), 0) FROM collection_decisions').fetchone()
                except sqlite3.Error:
                    pass
            title = 'Собираем организации' if active else 'Собрано ' + str(usable if usable is not None else '—') + ' из ' + str(params['target'])
            content += '<article class="progress-card" aria-live="polite"><h1>' + title + '</h1>'
            if active:
                content += '<p role="status"><span class="activity" aria-hidden="true"></span> Сбор выполняется</p>'
            content += '<p>Прошло: ' + elapsed + '</p>'
            content += '<p>Просмотрено карточек: <strong>' + str(reviewed if reviewed is not None else 'пока нет данных') + '</strong> · Пригодных организаций: <strong>' + str(usable if usable is not None else 'пока нет данных') + '</strong></p>'
            if not active:
                content += '<p>' + {'succeeded': 'Сбор завершён.', 'failed': 'Сбор завершён с ошибкой или цель не подтверждена.', 'cancelled': 'Сбор остановлен.'}.get(row['status'], '') + '</p>'
                if usable is None:
                    content += '<p>Итоговое количество не подтверждено.</p>'
                if row['status'] == 'succeeded':
                    content += '<p><a class="cta" href="/analysis?run=' + esc(row['id']) + '">Перейти к проверке сайтов →</a></p>'
        else:
            children = jobs.analysis_children(row['id']) if row['kind'] == 'analysis-batch' else [row]
            finished = sum(c['status'] not in {'pending', 'queued', 'running'} for c in children)
            totals = Counter()
            for child in children:
                if child['status'] == 'succeeded':
                    saved = observation(jobs.state_dir, child, json.loads(child['params'])['domain'])
                    totals[saved['qualification'] or 'HOLD'] += 1
                elif child['status'] in {'failed', 'cancelled'}:
                    totals['ERROR'] += 1
            content += '<article class="progress-card" aria-live="polite"><h1>Проверено ' + str(finished) + ' из ' + str(len(children)) + '</h1>'
            if active:
                content += '<p role="status"><span class="activity" aria-hidden="true"></span> Проверяем сайты</p>'
            content += '<p>Прошло: ' + elapsed + '</p><p>Готово предложений: ' + str(totals['READY']) + ' · Без предложения: ' + str(totals['REJECT']) + ' · Требует внимания: ' + str(totals['HOLD']) + ' · Ошибок / остановлено: ' + str(totals['ERROR']) + '</p>'
            running = [json.loads(c['params'])['domain'] for c in children if c['status'] == 'running']
            content += '<p>' + ('Сейчас проверяются сайты этого задания: ' if len(running) > 1 else 'Сейчас проверяется сайт этого задания: ') + esc(', '.join(running) or ('нет; ожидаем очередь или завершение' if active else 'нет; задание завершено')) + '</p>'
            content += '<p>READY — готово КП · REJECT — без КП · HOLD — требует внимания · ERROR — ошибка / остановлено. Итоги относятся только к этому заданию.</p>'
            for child in children:
                info = json.loads(child['params'])
                if child['error']:
                    content += '<p role="status">' + esc(info['domain']) + ': ошибка проверки. Подробности в «Служебном».</p>'
            content += '<p><a class="cta" href="/proposals">Посмотреть готовые предложения →</a></p>'
        if row['error'] and row['status'] == 'succeeded':
            content += '<p role="status">Есть служебные предупреждения. Подробности в «Служебном».</p>'
        elif row['error']:
            content += '<p role="alert">' + ('Не удалось подтвердить результат сбора.' if row['kind'] == 'parser-v3' else 'Задание завершено с ошибкой.') + ' Подробности в «Служебном».</p>'
        if active:
            content += '<p>Экран обновляется автоматически каждые 5 секунд.</p>'
            content += self.form('/jobs/' + row['id'] + '/cancel', 'Остановить', confirm=not row['kind'].startswith('analysis-'))
        return content + '</article>'

    def html(self, content, status=200):
        path = urlsplit(self.path).path
        jobs = getattr(self.server, 'jobs', None)
        if status == 200 and path == '/collect' and jobs:
            latest = next((j for j in jobs.list() if j['kind'] == 'parser-v3'), None)
            if latest:
                start = content.find('<article><h2>Последний сбор</h2>')
                end = content.find('</article>', start)
                if start >= 0 and end >= 0:
                    content = content[:start] + self.execution_progress(jobs.get(latest['id'])) + '<details><summary>Служебное</summary>' + content[start:end + 10] + '</details>' + content[end + 10:]
        elif status == 200 and path == '/crm':
            content = '<p>' + link('/proposals', 'Выбрать READY и открыть предпросмотр передачи →') + '</p>' + content
        elif status == 200 and path == '/analysis/preview':
            data = self.index()
            history = jobs.analysis_history() if jobs else {}
            summary, _ = analysis_summary(data['leads'], history, {domain(r['target']) for r in data['reports'] if r['kind'] == 'audit'})
            run = parse_qs(urlsplit(self.path).query).get('run', [''])[0]
            content = '<p>' + link('/analysis' + ('?' + urlencode({'run': run}) if run else ''), '← Вернуться к выбранному списку сайтов') + '</p>' + analysis_summary_html(summary) + content
        elif status == 200 and path.startswith('/jobs/') and jobs:
            row = jobs.get(path.removeprefix('/jobs/'))
            if row and row['kind'] in {'parser-v3', 'analysis-batch', 'analysis-site'}:
                content = self.execution_progress(row) + '<details><summary>Служебное</summary>' + content + '</details>'
        super().html(content, status)

    def network_selection(self, data, state=None):
        from src.lexradar.crm_networks import select_networks
        if state is None:
            state = self.crm_history()
        offers = {o['lead']['id']: o for o in data['offers']}
        rows = []
        for lead in data['leads']:
            offer = offers.get(lead['id'])
            rows.append(dict(id='web-account-' + lead['id'], company_name=lead['name'],
                             website=next(iter(lead['websites']), ''), email=lead.get('email') or '',
                             emails=[c['email'] for c in offer['saved']['contacts']] if offer else [],
                             eligible=bool(offer)))
        return select_networks(rows, state)

    @staticmethod
    def local_history(path, lock_path):
        with lock_path.open('r') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if path.stat().st_size > 8_000_000:
                raise ValueError('History too large')
            state = json.loads(path.read_text())
        if not isinstance(state, dict) or any(not isinstance(v, dict) for v in state.values()):
            raise ValueError('Invalid history')
        return state

    def crm_history(self):
        directory = Path.home() / '.local/state/lexradar'
        # Missing primary history is unknown, never an empty CRM.
        state = self.local_history(directory / 'crm-prepare.json', directory / 'crm-prepare.lock')
        path = directory / 'espo-export.json'
        if path.exists():
            state.update({'export:' + k: v for k, v in self.local_history(path, path.with_suffix('.lock')).items()})
        return state

    @staticmethod
    def crm_remaining_ids(offers, networks, prepared, root):
        from src.lexradar.crm_networks import network_keys
        from src.lexradar import crm_prepare
        from src.lexradar import espo_export as espo
        prepared = dict(prepared)
        for offer in offers:
            lead = offer['lead']
            source = 'web-account-' + lead['id']
            entry = prepared.get(source, {})
            result = entry.get('result', {})
            # Only a pre-write identity HOLD can be reconsidered; pending steps remain blocking.
            if (result.get('status') != 'HOLD_IDENTITY' or entry.get('steps') != {}
                    or source + ':draft' in prepared
                    or any(entry.get(k) or result.get(k) for k in
                           ('account_id', 'document_id', 'email_id', 'draft_error', 'draft_failure'))
                    or any(m.get('document_id') or m.get('attachment_id')
                           for m in result.get('materials', []))):
                continue
            try:
                row = dict(id=source, company_name=lead['name'], website=lead['websites'][0],
                           pdf_path=offer['base'] + 'proposal.pdf', dossier=offer['base'] + 'dossier',
                           letter_text='Account only: подтверждённый READY из Control Center')
                row['pdf_sha256'] = crm_prepare.digest(read_file(root, row['pdf_path'], 8_000_000))
                items = crm_prepare.materials(row, root)
                binding = dict(host=espo.website_domain(row['website']), company=row['company_name'],
                               materials=[{k: v for k, v in item.items() if k != 'data'} for item in items],
                               letter=row['letter_text'])
                if entry.get('binding') != crm_prepare.digest(json.dumps(
                        binding, sort_keys=True, ensure_ascii=False).encode()):
                    continue
                crm_prepare.identify(row, root, entry, clinic_account=True)
            except (espo.Hold, OSError, ValueError, KeyError, TypeError):
                continue
            del prepared[source]
        used = set()
        for entry in prepared.values():
            for binding in [entry.get('network'), entry.get('binding')] + entry.get('previous_bindings', []):
                if isinstance(binding, dict):
                    used.update(network_keys(binding))
        return [o['lead']['id'] for o in offers
                if networks['web-account-' + o['lead']['id']]['status'] == 'REPRESENTATIVE'
                and 'web-account-' + o['lead']['id'] not in prepared
                and 'web-account-' + o['lead']['id'] + ':draft' not in prepared
                and not network_keys(dict(website=o['lead']['websites'][0], company_name=o['lead']['name'])) & used
                and (networks['web-account-' + o['lead']['id']]['email'] or o['lead'].get('phone'))]

    def crm_availability(self, data):
        prepared = self.crm_history()
        directory = self.server.jobs.state_dir
        observed = {}
        for name in ('mailing-archive.json', 'mailing.json'):
            path = directory / name
            if path.exists():
                observed.update(self.local_history(path, directory / 'mailing.lock'))
        # Saved queue bindings also prevent a new transfer after journal changes.
        for eid, row in observed.items():
            prepared['mailing:' + eid] = dict(row, binding=dict(
                website=row.get('website', ''), company_name=row.get('name', '')))
            source = row.get('source_id')
            if source:
                prepared.setdefault(source, row)
        networks = self.network_selection(data, prepared)
        remaining = self.crm_remaining_ids(data['offers'], networks, prepared, directory.resolve())
        transferred, sent, accounts = set(), set(), set()
        for offer in data['offers']:
            lead = offer['lead']
            source = 'web-account-' + lead['id']
            host = domain(lead['websites'][0])
            for key, entry in prepared.items():
                binding = entry.get('network') or entry.get('binding') or {}
                result = entry.get('result', {})
                steps = entry.get('steps', {})
                if (key.removesuffix(':draft') != source and entry.get('source_id') != source
                        and (not isinstance(binding, dict) or domain(binding.get('website', '')) != host)):
                    continue
                aid = entry.get('account_id') or result.get('account_id') or steps.get('account', {}).get('id')
                if aid:
                    accounts.add(aid)
                if (aid or result.get('document_id') or result.get('email_id')
                        or entry.get('document_id') or steps.get('document', {}).get('id')
                        or steps.get('email', {}).get('id')):
                    transferred.add(lead['id'])
                if entry.get('status') == 'SENT' and entry.get('checked_at'):
                    sent.add(aid or lead['id'])
        # Older confirmed Sent remain evidence even if the queue no longer contains them.
        path = directory / 'followup.sqlite3'
        if path.exists():
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                for (raw,) in db.execute('SELECT record FROM snapshot'):
                    row = json.loads(raw)
                    if row.get('account_id') in accounts and row.get('date_sent'):
                        sent.add(row['account_id'])
        return dict(networks=networks, remaining=remaining, transferred=len(transferred), sent=len(sent))

    def holds_page(self):
        data, _, counts = self.workflow()
        params = parse_qs(urlsplit(self.path).query)
        category = params.get('category', [''])[0]
        sort = params.get('sort', [''])[0]
        query = params.get('q', [''])[0].strip().casefold()
        categories = {
            'evidence': ('Нет подтверждённых признаков', 'Проверить сохранённые материалы вручную. Без доказательств не делать юридических выводов и не создавать КП.'),
            'review': ('Надёжность доказательств требует проверки', 'Вручную сверить доказательства и ограничения результата; HOLD не подтверждает нарушение.'),
            'technical': ('Техническая недоступность / ограничения сбора', 'Согласовать повторную проверку; при robots_disallow сначала проверить разрешение на сбор.'),
            'identity': ('Связь результата с организацией', 'Сверить домен и запуск; проверить возможный дубль без удаления данных.'),
            'unknown': ('Причина не определена', 'Проверить сохранённый результат и журнал; не переводить в READY автоматически.'),
        }
        rows = []
        for row in data['holds']:
            rows.append((hold_category(row), row))
        totals = Counter(key for key, _ in rows)
        if params.get('format') == ['stats']:
            self.respond_json(200, dict(
                generated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                scope='latest_audit_hold_per_normalized_domain',
                total=len(rows), categories={key: totals[key] for key in categories},
                index_warnings=data.get('warnings', 0)))
            return
        if sort == 'category':
            order = list(categories)
            rows.sort(key=lambda item: (order.index(item[0]), item[1]['host']))
        content = '<h1>Разобрать HOLD</h1><p>Актуальных HOLD: <strong>' + str(counts['HOLD']) + '</strong>. Один последний анализ на нормализованный домен. Филиалы одного домена объединены; разные домены одной сети могут оставаться отдельно. CRM HOLD и ошибки заданий сюда не входят.</p>'
        content += '<p>Повтор доступен только для технических ограничений и проверки надёжности доказательств. Используется штатный анализ с соблюдением robots.txt; HOLD изменится только по новому результату. Юридический итог движка и коммерческий HOLD различаются; исходный итог доступен в results.csv и review.json.</p>'
        content += '<p>' + link('/holds?format=stats', 'Обезличенная статистика (JSON, вся выборка)') + '</p>'
        if data.get('warnings'):
            content += '<p role="status">Есть ограничения индекса: ' + str(data['warnings']) + '. Полнота базы не гарантирована.</p>'
        content += '<table><thead><tr><th>Категория</th><th>Количество</th><th>Рекомендуемое действие</th></tr></thead><tbody>'
        for key, (title, action) in categories.items():
            content += '<tr><td>' + link('/holds?' + urlencode({'category': key}), title) + '</td><td>' + str(totals[key]) + '</td><td>' + esc(action) + '</td></tr>'
        content += '</tbody></table><form method="get" action="/holds"><label>Категория <select name="category"><option value="">Все</option>'
        for key, (title, _) in categories.items():
            content += '<option value="' + key + '"' + (' selected' if category == key else '') + '>' + esc(title) + '</option>'
        content += '</select></label><label>Порядок <select name="sort"><option value="">По списку организаций</option><option value="category"' + (' selected' if sort == 'category' else '') + '>По категории и домену</option></select></label>'
        content += '<label>Поиск <input name="q" value="' + esc(params.get('q', [''])[0]) + '"></label><button>Показать</button></form>'
        content += '<form method="post" action="/holds/retry"><input type="hidden" name="csrf" value="' + esc(self.server.csrf_token) + '"><input type="hidden" name="confirm" value="yes">'
        if category in {'technical', 'review'}:
            content += '<label><input type="checkbox" id="hold-all"> Выбрать все показанные в текущей категории</label>'
        content += '<p role="status" aria-live="polite">Выбрано организаций: <strong id="hold-count">0</strong></p><button>Повторно проверить выбранные</button>'
        content += '<table><thead><tr><th>Выбор</th><th>Организация</th><th>Домен</th><th>Категория и причина</th><th>Сохранённый источник</th></tr></thead><tbody>'
        shown = 0
        for key, row in rows:
            reason = row['saved']['reason'] or 'Причина не определена'
            if category and key != category:
                continue
            if query and query not in (row['lead']['name'] + ' ' + row['host'] + ' ' + reason + ' ' + categories[key][0]).casefold():
                continue
            shown += 1
            base = '/analysis-files/' + row['job']['id'] + '/'
            checkbox = ('<input type="checkbox" name="lead" value="' + esc(row['lead']['id']) + '" aria-label="Выбрать ' + esc(row['lead']['name']) + '">' if key in {'technical', 'review'} else '—')
            content += '<tr><td>' + checkbox + '</td><td>' + esc(row['lead']['name']) + '</td><td>' + esc(row['host']) + '</td><td>' + esc(categories[key][0]) + '<br>' + esc(reason) + '</td><td>' + link('/jobs/' + row['job']['id'], 'Результат задания') + ' · ' + link(base + 'results.csv', 'results.csv') + ' · ' + link(base + 'review.json', 'review.json (если сохранён)') + '</td></tr>'
            if row['saved'].get('warning'):
                content += '<tr><td colspan="5">Ограничение сохранённого источника: ' + esc(row['saved']['warning']) + '</td></tr>'
        self.followup_nonce = secrets.token_urlsafe(24)
        content += '''</tbody></table></form><script nonce="''' + self.followup_nonce + '''">
const boxes = [...document.querySelectorAll('input[name="lead"]')];
const all = document.getElementById('hold-all');
function countHolds() {
  const count = boxes.filter(box => box.checked).length;
  document.getElementById('hold-count').textContent = count;
  if (all) { all.checked = boxes.length > 0 && count === boxes.length; all.indeterminate = count > 0 && count < boxes.length; }
}
boxes.forEach(box => box.addEventListener('change', countHolds));
if (all) all.addEventListener('change', () => { boxes.forEach(box => box.checked = all.checked); countHolds(); });
countHolds();
</script>'''
        try:
            self.html(content + '<p>Показано: ' + str(shown) + '</p>')
        finally:
            self.followup_nonce = None

    def retry_holds(self, values):
        back = '<p>' + link('/holds', 'Вернуться к HOLD') + '</p>'
        try:
            if set(values) - {'csrf', 'confirm', 'lead'}:
                raise ValueError('Некорректная форма')
            selected = set(values.get('lead', []))
            jobs = self.server.jobs
            with jobs.lock:
                records = [row['lead'] for row in self.workflow()[0]['holds']
                           if hold_category(row) in {'technical', 'review'}]
                if not selected or len(selected) > 200 or not selected <= {r['id'] for r in records}:
                    raise ValueError('Выбор пуст или HOLD уже изменился; обновите список')
                # Explicit repeats use the existing queue; new-only filtering does not apply to HOLD.
                token, preview = Jobs.preview_analysis(jobs, records, identifiers=selected)
                try:
                    if not preview['params']['sites']:
                        raise ValueError('Выбрано: ' + str(len(selected)) + '. Поставлено: 0. ' + '; '.join(preview['params']['skipped']))
                    identifier = jobs.submit_analysis(token)
                finally:
                    jobs.previews.pop(token, None)
            content = '<h1>Повторная проверка поставлена в очередь</h1><p>Выбрано организаций: ' + str(len(selected)) + '. Поставлено: ' + str(len(preview['params']['sites'])) + '. Пропущено: ' + str(len(preview['params']['skipped'])) + '.</p>'
            for reason in preview['params']['skipped']:
                content += '<p>' + esc(reason) + '</p>'
            self.html(content + link('/jobs/' + identifier, 'Прогресс проверки') + back)
        except ValueError as exc:
            self.html('<h1>Повторная проверка не поставлена в очередь</h1><p>' + esc(exc) + '</p>' + back, 400)
        except (OSError, sqlite3.Error):
            self.html('<h1>Не удалось подтвердить постановку в очередь</h1><p>Проверьте журнал заданий перед повтором.</p>' + back, 503)

    def proposals_page(self):
        data, _, counts = self.workflow()
        content = '<h1>Готовые предложения</h1><p>Всего подготовлено КП (актуальные по доменам): <strong>' + str(counts['READY']) + '</strong>'
        content += ' · REJECT — нет предложения: ' + str(counts['REJECT'])
        content += ' · HOLD — требует внимания: ' + str(counts['HOLD'])
        content += ' · ' + link('/analysis?analysis_errors=yes', 'Ошибки анализа: ' + str(counts['ERROR'])) + '</p>'
        content += '<p>' + link('/holds', 'Разобрать HOLD') + '</p>'
        params = parse_qs(urlsplit(getattr(self, 'path', '/proposals')).query)
        historical = params.get('view') == ['history']
        content += '<p>' + link('/proposals', 'Ожидают передачи') + ' · ' + link('/proposals?view=history', 'Все подготовленные КП / история') + '</p>'
        content += '<form method="get" action="/proposals"><button name="select" value="remaining">Выбрать оставшиеся для CRM</button></form>'
        run = '' if params.get('select') == ['remaining'] else params.get('run', [''])[0]
        runs = {m['run_id']: m.get('run_name', m['run_id']) for lead in data['leads']
                for m in lead.get('members', [lead]) if m.get('run_id')}
        content += '<form method="get" action="/proposals">' + ('<input type="hidden" name="view" value="history">' if historical else '') + '<label>Запуск сбора <select name="run"><option value="">Вся база</option>'
        for key, name in sorted(runs.items()):
            content += '<option value="' + esc(key) + '"' + (' selected' if run == key else '') + '>' + esc(name) + '</option>'
        content += '</select></label><button>Показать</button></form>'
        try:
            availability = self.crm_availability(data)
            networks = availability['networks']
            remaining_ids = availability['remaining']
            remaining_count = len(remaining_ids)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
            networks, remaining_ids = {}, []
            remaining_count = '—'
            content += '<p role="status">История CRM недоступна; выбор заблокирован до сверки.</p>'
        offers = [o for o in data['offers']
                  if (historical or o['lead']['id'] in remaining_ids)
                  and (not run or any(m.get('run_id') == run for m in o['lead'].get('members', [o['lead']])))]
        new_ids = [o['lead']['id'] for o in offers if o['lead']['id'] in remaining_ids]
        content += '<p>Ожидают новой передачи по локальной истории: ' + str(remaining_count) + '. Показано КП: ' + str(len(offers)) + '.</p>'
        limit = params.get('select', [''])[0]
        selected = set(new_ids if limit == 'remaining' else new_ids if limit == 'all' else new_ids[:int(limit)] if limit in {'10', '20', '30'} else [])
        content += '<p>Доступно для новой передачи в этом списке: ' + str(len(new_ids)) + '. Исключены прежние попытки, Account/Document/Email, HOLD и дубли сетей. Отсутствие записи не доказывает отсутствие организации в CRM; при подтверждении сохраняются штатные проверки дублей.</p>'
        content += '<form method="get" action="/proposals"><input type="hidden" name="run" value="' + esc(run) + '">' + ('<input type="hidden" name="view" value="history">' if historical else '')
        for value, label in [('10', 'Выбрать первые 10'), ('20', 'Выбрать первые 20'), ('30', 'Выбрать первые 30'), ('all', 'Выбрать все новые')]:
            content += '<button name="select" value="' + value + '"' + ('' if new_ids else ' disabled') + '>' + label + '</button>'
        content += '</form><p role="status">Отмечено быстрым выбором: ' + str(len(selected)) + '. Галочки можно изменить перед передачей.</p>'
        content += '<form method="get" action="/crm/preview" id="crm-selection"><button>Передать выбранные в CRM →</button></form>'
        excluded = sum(networks.get('web-account-' + o['lead']['id'], {}).get('status') == 'DUPLICATE_NETWORK' for o in offers)
        content += '<p>Исключено филиалов из CRM-выбора в показанных КП: <strong>' + str(excluded) + '</strong>. Аудиты и КП сохранены.</p>'
        content += '<div class="table-scroll" tabindex="0" role="region" aria-label="Список организаций"><table><thead><tr><th>№</th><th>CRM</th><th>Включить в рассылку</th><th>Организация</th><th>Сайт</th><th>Контакт</th><th>Основание КП</th><th>Статус</th><th>Дата КП (UTC)</th><th>КП</th></tr></thead><tbody>'
        for number, offer in enumerate(offers, 1):
            lead = offer['lead']
            network = networks.get('web-account-' + lead['id'], {})
            contact = network.get('email') or lead.get('phone') or ''
            content += '<tr><td>' + str(number) + '</td><td><input form="crm-selection" type="checkbox" name="lead" value="' + esc(lead['id']) + '" aria-label="Выбрать ' + esc(lead['name']) + '"' + (' checked' if lead['id'] in selected else '') + ('' if lead['id'] in remaining_ids else ' disabled') + '></td>'
            content += '<td><label><input form="crm-selection" type="checkbox" name="mailing" value="' + esc(lead['id']) + '" checked> Включить в рассылку</label></td>'
            content += '<td>' + link('/organizations/' + lead['id'], lead['name']) + '</td><td>' + link(lead['websites'][0], target(lead['websites'][0])[1]) + '</td><td>' + esc(contact or 'Не указан') + '</td>'
            content += '<td>' + '<br>'.join(esc(b) for b in offer['basis']) + '</td><td><span class="qualification ready">READY</span>' + (('<br>Последний анализ: ' + esc(offer['latest_status'])) if offer['latest_status'] != 'READY' else '') + '</td><td>' + esc(offer['job']['ended'] or offer['job']['created']) + '</td><td><a class="cta" href="/proposal-files/' + offer['job']['id'] + '/pdf">Открыть КП</a>'
            content += '<br>' + esc(network.get('status', 'HOLD')) + ': представитель — ' + esc(network.get('representative_name', 'требуется сверка')) + '; исключено в группе: ' + str(network.get('excluded', 0))
            content += '<br>' + link('/proposal-files/' + offer['job']['id'] + '/html', 'HTML') + '</td></tr>'
        self.html(content + '</tbody></table></div><p>' + link('/analysis', '← Проверка сайтов') + ' · ' + link('/crm', '4. Передать в CRM →') + '</p>')

    def crm_selection_preview(self):
        params = parse_qs(urlsplit(self.path).query)
        selected = set(params.get('lead', []))
        mailing = set(params.get('mailing', [])) & selected
        content = '<h1>Передача готовых КП в CRM</h1>'
        content += '<p>Предпросмотр без запросов в CRM. При передаче внутреннее согласование завершено автоматически: Account, PDF и черновик письма готовятся последовательно с техническими проверками.</p>'
        back = '<p>' + link('/proposals', '← Вернуться к выбору готовых предложений') + '</p>'
        if not selected:
            self.html(content + '<p>Ничего не выбрано. Отметьте готовые предложения галочками.</p>' + back)
            return
        data = self.workflow()[0]
        try:
            availability = self.crm_availability(data)
            networks = availability['networks']
            remaining_ids = availability['remaining']
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
            networks, remaining_ids = {}, []
        offers = [o for o in data['offers'] if o['lead']['id'] in selected
                  and o['lead']['id'] in remaining_ids]
        content += '<p>Выбрано организаций: ' + str(len(selected)) + '. Доступно READY с КП: ' + str(len(offers)) + '.</p>'
        if len(offers) != len(selected):
            content += '<p>Часть предложений уже передана, имеет прежнюю попытку/HOLD либо недоступна для новой передачи. Вернитесь к списку и проверьте выбор.</p>'
        for offer in offers:
            lead = offer['lead']
            contacts = list(dict.fromkeys(c for c in [lead.get('email'), lead.get('phone')]
                                         + [c['email'] for c in offer['saved']['contacts']] if c))
            content += '<article><h2>' + esc(lead['name']) + '</h2><p>Сайт: '
            content += link(lead['websites'][0], lead['websites'][0]) + '</p><p>Контакты: '
            content += esc(', '.join(contacts) if contacts else 'Не указаны — требуется проверка') + '</p><p>Автоматическая рассылка: ' + ('включить при подтверждённом основании' if lead['id'] in mailing else 'исключена оператором') + '</p><p>'
            content += link('/proposal-files/' + offer['job']['id'] + '/pdf', 'Открыть КП (PDF)') + ' · '
            content += link('/proposal-files/' + offer['job']['id'] + '/html', 'Открыть КП (HTML)') + '</p></article>'
        if len(offers) == len(selected) and all(o['lead'].get('email') or o['lead'].get('phone')
                                               or o['saved']['contacts'] for o in offers):
            fields = ''.join('<input type="hidden" name="lead" value="' + esc(o['lead']['id']) + '">' for o in offers)
            fields += ''.join('<input type="hidden" name="mailing" value="' + esc(lead_id) + '">' for lead_id in sorted(mailing))
            content += self.form('/crm/preview', 'Передать КП в CRM', fields, confirm=False)
        self.html(content + '<p>Передача не подтверждает согласие получателя на рекламную отправку. Без отдельного подтверждённого основания отправка заблокирована.</p>' + back)

    def do_POST(self):
        if urlsplit(self.path).path not in {'/crm/preview', '/followup/action', '/holds/retry'}:
            return super().do_POST()
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
                    or len(self.headers.get_all('Content-Length', [])) != 1
                    or self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded'):
                raise ValueError('Invalid headers')
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete body')
            values = parse_qs(raw.decode('utf-8'), strict_parsing=True, max_num_fields=220)
        except (ValueError, UnicodeError):
            self.reject_post(400, 'invalid_form', 'invalid_body_or_headers')
            return
        token = getattr(self.server, 'csrf_token', '')
        if (not token or len(values.get('csrf', [])) != 1
                or not hmac.compare_digest(values['csrf'][0].encode('utf-8'), token.encode('utf-8'))
                or values.get('confirm') != ['yes']):
            self.reject_post(403, 'confirmation_required', 'csrf_or_confirmation_invalid')
            return
        if urlsplit(self.path).path == '/holds/retry':
            self.retry_holds(values)
            return
        if urlsplit(self.path).path == '/followup/action':
            eid = values.get('email_id', [''])[0]
            followup_back = ('/followup?' + urlencode({'email': eid}) + '#followup-detail'
                             if ID.fullmatch(eid) else '/followup')
            try:
                allowed = {'csrf', 'confirm', 'email_id', 'status', 'due', 'comment', 'revision'}
                if set(values) - allowed or any(len(v) != 1 for v in values.values()):
                    raise ValueError('Некорректная форма.')
                self.server.followup.update(*(values.get(k, [''])[0] for k in
                    ('email_id', 'status', 'due', 'comment', 'revision')))
            except ValueError as exc:
                self.html('<h1>Результат не сохранён</h1><p>' + esc(exc) + '</p><p>'
                          + link(followup_back, 'Вернуться к выбранной записи') + '</p>', 400)
                return
            except (OSError, sqlite3.Error, TypeError, KeyError):
                self.html('<h1>Результат не сохранён</h1><p>Локальное хранилище недоступно. '
                          'Прежние данные не сброшены; обновите очередь перед повтором.</p><p>'
                          + link(followup_back, 'Вернуться к выбранной записи') + '</p>', 503)
                return
            self.send_response(303)
            self.send_header('Location', '/followup?' + urlencode({
                'email': values['email_id'][0], 'saved': int(values['revision'][0]) + 1}) + '#followup-detail')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if values.get('stage', ['account']) not in (['account'], ['draft'], ['draft_batch']):
            self.reject_post(400, 'invalid_form', 'invalid_stage')
            return
        selected = set(values.get('lead', []))
        data = self.workflow()[0]
        try:
            availability = self.crm_availability(data)
            networks = availability['networks']
            remaining_ids = availability['remaining']
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
            networks, remaining_ids = {}, []
        offers = [o for o in data['offers'] if o['lead']['id'] in selected
                  and o['lead']['id'] in remaining_ids]
        back = '<p>' + link('/proposals', '← Вернуться к выбору готовых предложений') + '</p>'
        if not selected or len(offers) != len(selected) or any(
                not (o['lead'].get('email') or o['lead'].get('phone') or o['saved']['contacts']) for o in offers):
            self.html('<h1>Подтверждение отклонено</h1><p>Выбор пуст или предложения уже переданы, имеют прежнюю попытку/HOLD либо недоступны для новой передачи. Проверьте выбор.</p>' + back, 400)
            return
        mailing = set(values.get('mailing', [])) & selected
        try:
            self.server.mailing.select(selected, mailing)
        except (OSError, ValueError, TypeError):
            self.html('<h1>Подготовка остановлена</h1><p>Не удалось сохранить выбор рассылки; повторите после сверки журнала.</p>' + back, 400)
            return
        from src.lexradar import crm_prepare
        from src.lexradar import espo_export as espo
        root = self.server.jobs.state_dir.resolve()
        batch = values.get('stage', ['account']) != ['draft']
        drafts = values.get('stage') in (['draft_batch'], ['draft'])
        journal = Path.home() / '.local/state/lexradar/crm-prepare.json'
        if values.get('stage') == ['draft_batch']:
            try:
                state = json.loads(journal.read_text())
                offers = [o for o in offers if state.get('web-account-' + o['lead']['id'], {}).get('result', {}).get('status') == 'ACCOUNT_VERIFIED']
            except (OSError, ValueError, AttributeError):
                self.html('<h1>Подготовка остановлена</h1><p>Не удалось прочитать журнал CRM; требуется сверка.</p>' + back, 400)
                return
        content = '<h1>Результат подготовки CRM</h1>'
        counts = Counter()
        if batch:
            nonce = secrets.token_urlsafe(24)

            def progress_update(done, finished=False):
                percent = done * 100 // len(offers) if offers else 100
                text = ('Завершено' if finished else 'Подготовка') + ': обработано ' + str(done) + ' из ' + str(len(offers))
                text += '. Успешных: ' + str(counts['success']) + ' · HOLD: ' + str(counts['hold']) + ' · Ошибок: ' + str(counts['error']) + ' · ' + str(percent) + '%'
                return ('<script nonce="' + nonce + '">document.getElementById("crm-progress-text").textContent='
                        + json.dumps(text) + ';document.getElementById("crm-progress-bar").value='
                        + str(percent) + ';</script>')

            content += ('<div style="position:sticky;top:0;z-index:10;background:#fff;padding:16px;border:1px solid #E5E7EB;border-radius:12px">'
                        '<p id="crm-progress-text" role="status" aria-live="polite">Подготовка: обработано 0 из ' + str(len(offers))
                        + '. Успешных: 0 · HOLD: 0 · Ошибок: 0 · 0%</p>'
                        '<progress id="crm-progress-bar" aria-label="Выполнение подготовки CRM" max="100" value="0" style="width:100%"></progress></div>')
            page = organization_page(content + '<p>Пакетная подготовка: ' + str(len(offers)) + ' Account. Черновики с галочкой «Включить в рассылку» попадут в очередь; без разрешения отправка заблокирована. Не закрывайте страницу до завершения.</p><!--progress-->')
            head, tail = page.split('<!--progress-->')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'nonce-" + nonce + "'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(head.encode('utf-8'))
            self.wfile.flush()
        for number, offer in enumerate(offers, 1):
            lead = offer['lead']
            if batch:
                self.wfile.write(('<p role="status">Обработано ' + str(number - 1) + ' из ' + str(len(offers)) + '. Сейчас: ' + esc(lead['name']) + '</p>').encode('utf-8'))
                self.wfile.flush()
            try:
                pdf = read_file(root, offer['base'] + 'proposal.pdf', 8_000_000)
                row = {'id': 'web-account-' + lead['id'], 'company_name': lead['name'],
                       'website': lead['websites'][0], 'pdf_path': offer['base'] + 'proposal.pdf',
                       'pdf_sha256': crm_prepare.digest(pdf), 'dossier': offer['base'] + 'dossier',
                       'letter_text': 'Account only: подтверждённый READY из Control Center'}
                row['email'] = networks['web-account-' + lead['id']]['email']
                row['recipient_verified'] = bool(row['email'])
                result = crm_prepare.run(
                    [row], root, journal,
                    confirm=True, accounts_only=not drafts, prepare_drafts=drafts)[0]
                if result['status'] == 'ACCOUNT_VERIFIED':
                    result = crm_prepare.run(
                        [row], root, journal, confirm=True, prepare_drafts=True)[0]
                if result.get('status') == 'DRAFT_VERIFIED' and result.get('email_id'):
                    try:
                        self.server.mailing.enqueue(result, row)
                    except (OSError, ValueError, TypeError, KeyError):
                        result.setdefault('warnings', []).append('Очередь будет восстановлена из CRM-журнала; требуется сверка.')
                message = ('Account проверен: ' + result['account_id']
                           if result['status'] == 'ACCOUNT_VERIFIED' else
                           result['status'] + ': ' + '; '.join(e['error'] for e in result['errors']))
                if drafts:
                    message = result['status'] + ': ' + '; '.join(e['error'] for e in result['errors'])
                from control_jobs import log_text
                message = crm_status(result['status']) + '. ' + log_text(message.encode())
                details = ''.join('<p>' + label + ': ' + esc(result.get(key, '—')) + '</p>'
                                  for label, key in [('Account', 'account_id'), ('Document', 'document_id'), ('Email', 'email_id')])
                details += '<p>' + crm_entity_links(result) + '</p>'
                if result.get('status') == 'DRAFT_VERIFIED':
                    if lead['id'] not in mailing:
                        details += '<p>Исключено из автоматической рассылки. Account, PDF и Email Draft сохранены.</p>'
                    elif 'Очередь будет восстановлена из CRM-журнала; требуется сверка.' in result.get('warnings', []):
                        details += '<p role="status">Постановка сейчас не подтверждена; требуется сверка очереди. CRM-экспорт сохранён.</p>'
                    else:
                        details += '<p>Письмо передано в накопительную очередь; без отдельного разрешения — ожидает разрешения. Статус смотрите в блоке «Рассылка LexRadar» на главной.</p>'
                if result.get('reason'):
                    details += '<p>' + esc(log_text(result['reason'].encode())) + '</p>'
                if result.get('email_id'):
                    label = 'Открыть проверенный черновик' if result['status'] == 'DRAFT_VERIFIED' else 'Открыть Email для сверки (не подтверждён)'
                    details += link('http://192.168.233.105/#Email/view/' + result['email_id'], label)
            except (espo.Hold, OSError, ValueError, KeyError, TypeError):
                result = {'status': 'ERROR', 'errors': [True]}
                details = ''
                message = 'Ошибка подготовки CRM; требуется сверка журнала перед повтором.'
            counts['success' if result['status'] in {'DRAFT_VERIFIED', 'DOCUMENT_VERIFIED_NO_DRAFT'} else 'hold' if result['status'].startswith('HOLD') else 'error'] += 1
            article = '<article><h2>' + esc(lead['name']) + '</h2><p>' + esc(message) + '</p>' + details + '</article>'
            if batch:
                progress = '<p role="status">Обработано ' + str(number) + ' из ' + str(len(offers)) + '. Успешно: ' + str(counts['success']) + ' · HOLD: ' + str(counts['hold']) + ' · Ошибки: ' + str(counts['error']) + '</p>'
                self.wfile.write((article + progress + progress_update(number)).encode('utf-8'))
                self.wfile.flush()
            else:
                content += article
        if batch:
            summary = '<p role="status">Завершено. Успешно: ' + str(counts['success']) + ' · HOLD: ' + str(counts['hold']) + ' · Ошибки: ' + str(counts['error']) + '. Пропущено без ACCOUNT_VERIFIED: ' + str(len(selected) - len(offers)) + '.</p>'
            self.wfile.write((progress_update(len(offers), finished=True) + summary + '<p>DRAFT_VERIFIED передаётся в очередь только с галочкой «Включить в рассылку». Планирование разрешено только при отдельном документированном разрешении на рекламную отправку.</p>' + back + tail).encode('utf-8'))
            self.wfile.flush()
            return
        self.html(content + '<p>DRAFT_VERIFIED передаётся в очередь только с галочкой «Включить в рассылку». Планирование разрешено только при отдельном документированном разрешении на рекламную отправку.</p>' + back)

    def proposal_file(self, path):
        parts = path.split('/')
        offer = next((o for o in self.workflow()[0]['offers'] if len(parts) == 4
                      and o['job']['id'] == parts[2] and parts[3] in {'pdf', 'html'}), None)
        try:
            if offer is None:
                raise ValueError('Unavailable proposal')
            raw = read_file(self.server.jobs.state_dir, offer['base'] + 'proposal.' + parts[3], 8_000_000)
            if parts[3] == 'pdf' and not raw.startswith(b'%PDF-'):
                raise ValueError('Invalid PDF')
        except (OSError, ValueError):
            self.respond_json(404, {'error': 'proposal_unavailable'})
            return
        # Serve saved HTML with the existing CSP; scripts and external resources stay blocked.
        self.respond(200, raw, 'application/pdf' if parts[3] == 'pdf' else 'text/html; charset=utf-8')

    def home(self):
        self.refresh_seconds = 60
        data, history, counts = self.workflow()
        summary, _ = analysis_summary(data['leads'], history, {domain(r['target']) for r in data.get('reports', []) if r['kind'] == 'audit'})
        crm_remaining = transferred = sent = '—'
        crm_description = 'История CRM недоступна; показатели требуют сверки.'
        try:
            availability = self.crm_availability(data)
            crm_remaining = len(availability['remaining'])
            transferred, sent = availability['transferred'], availability['sent']
            crm_description = ('По локальным журналам и последним сохранённым наблюдениям CRM. '
                               'Передано — зафиксирован Account, Document или Email; это не обязательно завершённый Draft. '
                               'Отправлено — сохранённый Sent, не подтверждение доставки. '
                               'Отсутствие локальной записи не доказывает отсутствие организации в CRM; '
                               'внешняя история может быть неполной. Перед записью действуют штатные проверки CRM.')
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
            pass
        content = '<h1>От организаций к готовым предложениям</h1><p class="lead">Пройдите четыре шага. После CRM-экспорта письма попадают в очередь; отправка требует отдельного разрешения.</p><p>Показатели по всей доступной базе, включая исторический импорт; организации объединены по домену. Это не итоги последнего запуска.</p><div class="grid">'
        for label, description, url, enabled in (
            ('1. Собрать организации', 'Организаций в базе', '/collect', True),
            ('2. Проверить сайты', 'Ожидают проверки', '/analysis', counts['usable'] > 0),
            ('3. Готовые предложения', 'Ожидают передачи в CRM', '/proposals', isinstance(crm_remaining, int) and crm_remaining > 0),
            ('4. Передать в CRM', 'Готовы к передаче', '/crm', isinstance(crm_remaining, int) and crm_remaining > 0),
        ):
            number = {
                '/collect': summary['unique'],
                '/analysis': summary['available'],
                '/proposals': crm_remaining,
                '/crm': crm_remaining,
            }[url]
            content += (
                '<article><h2>' + label + '</h2>'
                '<strong class="metric">' + str(number) + '</strong>'
                '<p>' + description + '</p>'
            )
            content += (
                '<a class="cta" href="' + ('/proposals?select=remaining' if url == '/crm' else url) + '">' + label + '</a>'
                if enabled else
                '<button disabled>' + label + '</button>'
            )
            content += '</article>'
        content += '</div><div class="grid">'
        for label, value in (('Всего подготовлено КП', counts['READY']),
                             ('Передано в CRM', transferred), ('Отправлено клиентам', sent)):
            content += '<article><h2>' + label + '</h2><strong class="metric">' + str(value) + '</strong></article>'
        content += '</div><p>Всего подготовлено — актуальные сохранённые КП по доменам. ' + link('/proposals?view=history', 'Все подготовленные КП / история') + '</p><p>' + crm_description + '</p>'
        mailing = getattr(self.server, 'mailing', None)
        if mailing:
            content += '<article><h2>Рассылка</h2><a class="cta" href="/mailing">Открыть рассылку</a></article>'
        jobs = getattr(self.server, 'jobs', None)
        if jobs:
            for row in jobs.list():
                if row['kind'] in {'parser-v3', 'analysis-batch'} and row['status'] in {'queued', 'running'}:
                    content += '<p role="status">Выполняется: ' + link('/jobs/' + row['id'], 'Сбор организаций' if row['kind'] == 'parser-v3' else 'Прогресс проверки сайтов') + '</p>'
        if data.get('warnings'):
            content += '<p role="status">Часть файлов не загружена; показатели неполные. Подробности в служебном разделе.</p>'
        self.html(content + '<p><a href="/service">Служебное</a></p>')

    def service(self):
        content = '<h1>Служебное</h1><p class="lead">Задания, диагностика и сохранённые технические материалы.</p>'
        for url, title in (('/jobs', 'Журнал заданий и прогресс'), ('/settings', 'Настройки и диагностика'),
                           ('/reports', 'Все технические отчёты и PDF'), ('/organizations', 'Организации и контакты'),
                           ('/mentions', 'Упоминания в аудитах'), ('/analysis?analysis_errors=yes', 'Ошибки анализа')):
            content += '<p>' + link(url, title) + '</p>'
        self.html(content)

    def followup_page(self):
        self.followup_nonce = secrets.token_urlsafe(24)
        try:
            self.html(self.server.followup.html(self))
        finally:
            self.followup_nonce = None

    def do_GET(self):
        if not self.valid_local_request():
            self.respond_json(403, {'error': 'local_host_required'})
            return
        path = urlsplit(self.path).path
        if path == '/followup':
            self.followup_page()
        elif path == '/mailing':
            self.html(self.server.mailing.html(details=True, status=parse_qs(urlsplit(self.path).query).get('status', [''])[0]))
        elif path == '/service':
            self.service()
        elif path == '/holds':
            self.holds_page()
        elif path == '/crm':
            self.path = '/proposals?select=remaining'
            self.proposals_page()
        elif path == '/proposals':
            self.proposals_page()
        elif path == '/crm/preview':
            self.crm_selection_preview()
        elif path.startswith('/proposal-files/'):
            self.proposal_file(path)
        else:
            super().do_GET()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results-dir', type=Path, default=Path.cwd())
    parser.add_argument('--parser-dir', type=Path, default=DEFAULT_PARSER_DIR)
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    with ThreadingHTTPServer(('127.0.0.1', args.port), MVPHandler) as server:
        server.results_dir = args.results_dir.absolute()
        server.parser_dir = args.parser_dir.absolute()
        server.state_dir = Path(__file__).resolve().parent / '.control-center-state'
        server.csrf_token = secrets.token_urlsafe(32)
        server.catalog = Catalog(server.results_dir, server.parser_dir)
        server.jobs = MVPJobs(server.state_dir, server.parser_dir, server.results_dir)
        server.followup = Followup(server.state_dir)
        server.mailing = Mailing(server.state_dir)
        server.mailing.start()
        print(f'LexRadar Control Center: http://127.0.0.1:{args.port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.mailing.close()
            server.jobs.close()


if __name__ == '__main__':
    main()
