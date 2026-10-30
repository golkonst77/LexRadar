"""Four-step Control Center using the existing queue, engines and saved data."""
import argparse
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
from control_pages import esc, link
from control_enrichment import observation
from control_results import read_file


NAV = '''<a href="/">Обзор</a><a href="/collect">Сбор организаций</a>
<a href="/analysis">Проверка сайтов</a><a href="/proposals">Готовые предложения</a>
<a href="/crm">Передача в CRM</a><a href="/service">Служебное</a>'''


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
                page = page[:start] + '<footer><p>Локальный режим · Автоматическая рассылка отключена</p>' + page[end:]
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
        checked, active, available = set(), set(), set()
        for record in data['leads']:
            try:
                hosts = {target(url)[1] for url in record['websites']}
            except ValueError:
                continue
            if len(hosts) != 1:
                continue
            host = next(iter(hosts))
            attempts = analysis_rows(record, history)
            if host in completed or any(r['status'] in {'succeeded', 'failed', 'cancelled'} for r in attempts):
                checked.add(record['id'])
            if any(r['status'] in {'pending', 'queued', 'running'} for r in attempts):
                active.add(record['id'])
            if not attempts and host not in completed:
                available.add(record['id'])
        content = '<h1>' + ('Ошибки анализа' if errors_only else '2. Проверить сайты') + '</h1>'
        if run:
            content += '<p>Организации запуска: ' + esc(run) + '</p>'
        content += '<p>Новых (ещё не проверенных): ' + str(len(data['leads']) - len(checked)) + ' · Уже проверено: ' + str(len(checked)) + ' · Доступно для анализа: ' + str(len(available)) + ' · В очереди / работе: ' + str(len(active)) + '</p>'
        if not available and data['leads']:
            content += '<p role="status">' + ('Все сайты этого списка уже проверены.' if len(checked) == len(data['leads']) else 'Новых доступных сайтов нет. Есть сайты в очереди или без допустимого адреса.') + '</p>'
        content += '<p>Собрано: ' + str(len(data['leads'])) + ' · READY: ' + str(counts['READY'])
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
        if counts['active']:
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
            if reviewed is None or usable is None:
                database = jobs.state_dir / row['id'] / 'parser.sqlite'
                try:
                    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as db:
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
            for child in children:
                info = json.loads(child['params'])
                if child['status'] == 'running':
                    content += '<p>Текущий сайт: ' + esc(info['domain']) + '</p>'
                if child['error']:
                    content += '<p role="status">' + esc(info['domain']) + ': ошибка проверки. Подробности в «Служебном».</p>'
            content += '<p><a class="cta" href="/proposals">Посмотреть готовые предложения →</a></p>'
        if row['error']:
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
        elif status == 200 and path.startswith('/jobs/') and jobs:
            row = jobs.get(path.removeprefix('/jobs/'))
            if row and row['kind'] in {'parser-v3', 'analysis-batch', 'analysis-site'}:
                content = self.execution_progress(row) + '<details><summary>Служебное</summary>' + content + '</details>'
        super().html(content, status)

    def proposals_page(self):
        data, _, counts = self.workflow()
        content = '<h1>Готовые предложения</h1><p>READY: ' + str(counts['READY'])
        content += ' · REJECT — нет предложения: ' + str(counts['REJECT'])
        content += ' · HOLD — требует внимания: ' + str(counts['HOLD'])
        content += ' · ' + link('/analysis?analysis_errors=yes', 'Ошибки анализа: ' + str(counts['ERROR'])) + '</p>'
        content += '<p>Показаны коммерческие READY с существующим КП. Это не разрешение на отправку писем.</p>'
        if not data['offers']:
            self.html(content + '<p>Готовых КП пока нет. ' + link('/analysis', 'Перейти к проверке сайтов') + '</p>')
            return
        content += '<form method="get" action="/crm/preview" id="crm-selection"><button>Передать выбранные в CRM →</button></form>'
        content += '<div class="table-scroll" tabindex="0" role="region" aria-label="Список организаций"><table><thead><tr><th>CRM</th><th>Организация</th><th>Сайт</th><th>Контакт</th><th>Основание КП</th><th>Статус</th><th>Дата КП (UTC)</th><th>КП</th></tr></thead><tbody>'
        for offer in data['offers']:
            lead = offer['lead']
            contact = lead.get('email') or lead.get('phone') or (offer['saved']['contacts'][0]['email'] if offer['saved']['contacts'] else '')
            content += '<tr><td><input form="crm-selection" type="checkbox" name="lead" value="' + esc(lead['id']) + '" aria-label="Выбрать ' + esc(lead['name']) + '"' + ('' if contact else ' disabled') + '></td>'
            content += '<td>' + link('/organizations/' + lead['id'], lead['name']) + '</td><td>' + link(lead['websites'][0], target(lead['websites'][0])[1]) + '</td><td>' + esc(contact or 'Не указан') + '</td>'
            content += '<td>' + '<br>'.join(esc(b) for b in offer['basis']) + '</td><td><span class="qualification ready">READY</span>' + (('<br>Последний анализ: ' + esc(offer['latest_status'])) if offer['latest_status'] != 'READY' else '') + '</td><td>' + esc(offer['job']['ended'] or offer['job']['created']) + '</td><td><a class="cta" href="/proposal-files/' + offer['job']['id'] + '/pdf">Открыть КП</a>'
            content += '<br>' + link('/proposal-files/' + offer['job']['id'] + '/html', 'HTML') + '</td></tr>'
        self.html(content + '</tbody></table></div><p>' + link('/analysis', '← Проверка сайтов') + ' · ' + link('/crm', '4. Передать в CRM →') + '</p>')

    def crm_selection_preview(self):
        selected = set(parse_qs(urlsplit(self.path).query).get('lead', []))
        content = '<h1>Предварительное подтверждение передачи в CRM</h1>'
        content += '<p>Предпросмотр без запросов в CRM. После подтверждения будет проверен Account. Для ACCOUNT_VERIFIED можно отдельно подготовить PDF и черновик письма.</p>'
        back = '<p>' + link('/proposals', '← Вернуться к выбору готовых предложений') + '</p>'
        if not selected:
            self.html(content + '<p>Ничего не выбрано. Отметьте готовые предложения галочками.</p>' + back)
            return
        offers = [o for o in self.workflow()[0]['offers'] if o['lead']['id'] in selected]
        content += '<p>Выбрано организаций: ' + str(len(selected)) + '. Доступно READY с КП: ' + str(len(offers)) + '.</p>'
        if len(offers) != len(selected):
            content += '<p>Часть выбранных предложений больше недоступна как READY с КП. Вернитесь к списку и проверьте выбор.</p>'
        for offer in offers:
            lead = offer['lead']
            contacts = list(dict.fromkeys(c for c in [lead.get('email'), lead.get('phone')]
                                         + [c['email'] for c in offer['saved']['contacts']] if c))
            content += '<article><h2>' + esc(lead['name']) + '</h2><p>Сайт: '
            content += link(lead['websites'][0], lead['websites'][0]) + '</p><p>Контакты: '
            content += esc(', '.join(contacts) if contacts else 'Не указаны — требуется проверка') + '</p><p>'
            content += link('/proposal-files/' + offer['job']['id'] + '/pdf', 'Открыть КП (PDF)') + ' · '
            content += link('/proposal-files/' + offer['job']['id'] + '/html', 'Открыть КП (HTML)') + '</p></article>'
        if len(offers) == len(selected) and all(o['lead'].get('email') or o['lead'].get('phone')
                                               or o['saved']['contacts'] for o in offers):
            fields = ''.join('<input type="hidden" name="lead" value="' + esc(o['lead']['id']) + '">' for o in offers)
            content += self.form('/crm/preview', 'Подтвердить поиск или создание Account', fields, confirm=False)
        self.html(content + '<p>Подтверждение разрешает только поиск или создание Account. Документы и письма не создаются.</p>' + back)

    def do_POST(self):
        if urlsplit(self.path).path != '/crm/preview':
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
        if values.get('stage', ['account']) not in (['account'], ['draft']):
            self.reject_post(400, 'invalid_form', 'invalid_stage')
            return
        selected = set(values.get('lead', []))
        offers = [o for o in self.workflow()[0]['offers'] if o['lead']['id'] in selected]
        back = '<p>' + link('/proposals', '← Вернуться к выбору готовых предложений') + '</p>'
        if not selected or len(offers) != len(selected) or any(
                not (o['lead'].get('email') or o['lead'].get('phone') or o['saved']['contacts']) for o in offers):
            self.html('<h1>Подтверждение отклонено</h1><p>Выбор пуст или предложения больше недоступны как READY с КП и контактом. Проверьте выбор.</p>' + back, 400)
            return
        from src.lexradar import crm_prepare
        from src.lexradar import espo_export as espo
        root = self.server.jobs.state_dir.resolve()
        drafts = values.get('stage') == ['draft']
        content = '<h1>Результат подготовки CRM</h1>'
        for offer in offers:
            lead = offer['lead']
            try:
                pdf = read_file(root, offer['base'] + 'proposal.pdf', 8_000_000)
                row = {'id': 'web-account-' + lead['id'], 'company_name': lead['name'],
                       'website': lead['websites'][0], 'pdf_path': offer['base'] + 'proposal.pdf',
                       'pdf_sha256': crm_prepare.digest(pdf), 'dossier': offer['base'] + 'dossier',
                       'letter_text': 'Account only: подтверждённый READY из Control Center'}
                row['email'] = lead.get('email') or (offer['saved']['contacts'][0]['email'] if offer['saved']['contacts'] else '')
                row['recipient_verified'] = bool(row['email'])
                result = crm_prepare.run(
                    [row], root, Path.home() / '.local/state/lexradar/crm-prepare.json',
                    confirm=True, accounts_only=not drafts, prepare_drafts=drafts)[0]
                message = ('Account проверен: ' + result['account_id']
                           if result['status'] == 'ACCOUNT_VERIFIED' else
                           result['status'] + ': ' + '; '.join(e['error'] for e in result['errors']))
                if drafts:
                    message = result['status'] + ': ' + '; '.join(e['error'] for e in result['errors'])
                details = ''.join('<p>' + label + ': ' + esc(result.get(key, '—')) + '</p>'
                                  for label, key in [('Account', 'account_id'), ('Document', 'document_id'), ('Email', 'email_id')])
                if result.get('reason'):
                    details += '<p>' + esc(result['reason']) + '</p>'
                if result.get('email_id'):
                    label = 'Открыть проверенный черновик' if result['status'] == 'DRAFT_VERIFIED' else 'Открыть Email для сверки (не подтверждён)'
                    details += link('http://192.168.233.105/#Email/view/' + result['email_id'], label)
                if result['status'] == 'ACCOUNT_VERIFIED':
                    fields = '<input type="hidden" name="stage" value="draft"><input type="hidden" name="lead" value="' + esc(lead['id']) + '">'
                    details += self.form('/crm/preview', 'Подготовить PDF и Email Draft', fields, confirm=False)
            except (espo.Hold, OSError, ValueError, KeyError, TypeError):
                result = {'errors': [True]}
                details = ''
                message = 'Ошибка подготовки CRM; требуется сверка журнала перед повтором.'
            content += '<article><h2>' + esc(lead['name']) + '</h2><p>' + esc(message) + '</p>' + details + '</article>'
        self.html(content + '<p>Отправка не выполнялась; sendAt не устанавливался.</p>' + back)

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
        data, history, counts = self.workflow()
        content = '<h1>От организаций к готовым предложениям</h1><p class="lead">Пройдите четыре шага. Письма автоматически не отправляются.</p><div class="grid">'
        for label, description, url, enabled in (
            ('1. Собрать организации', 'Собрано организаций: ' + str(len(data['leads'])), '/collect', True),
            ('2. Проверить сайты', 'Проверено: ' + str(counts['checked']) + '; в работе: ' + str(counts['active']), '/analysis', counts['usable'] > 0),
            ('3. Готовые предложения', 'Готовых КП: ' + str(counts['READY']), '/proposals', counts['READY'] > 0),
            ('4. Передать в CRM', 'Подходящих READY с контактом: ' + str(counts['crm_ready']) + '. Передача ещё не проверена.', '/crm', counts['crm_ready'] > 0),
        ):
            content += '<article><h2>' + label + '</h2><strong class="metric">' + str({'/collect': len(data['leads']), '/analysis': counts['checked'], '/proposals': counts['READY'], '/crm': counts['crm_ready']}[url]) + '</strong><p>' + description + '</p>'
            content += ('<a class="cta" href="' + url + '">' + label + '</a>' if enabled else '<button disabled>' + label + '</button><p>Пока нет подходящих данных предыдущего шага.</p>')
            content += '</article>'
        self.html(content + '</div><p><a href="/service">Служебное</a></p>')

    def service(self):
        content = '<h1>Служебное</h1><p class="lead">Задания, диагностика и сохранённые технические материалы.</p>'
        for url, title in (('/jobs', 'Журнал заданий и прогресс'), ('/settings', 'Настройки и диагностика'),
                           ('/reports', 'Все технические отчёты и PDF'), ('/organizations', 'Организации и контакты'),
                           ('/mentions', 'Упоминания в аудитах'), ('/analysis?analysis_errors=yes', 'Ошибки анализа')):
            content += '<p>' + link(url, title) + '</p>'
        self.html(content)

    def do_GET(self):
        if not self.valid_local_request():
            self.respond_json(403, {'error': 'local_host_required'})
            return
        path = urlsplit(self.path).path
        if path == '/service':
            self.service()
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
        print(f'LexRadar Control Center: http://127.0.0.1:{args.port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.jobs.close()


if __name__ == '__main__':
    main()
