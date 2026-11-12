"""Daily existing call Tasks: local proposal/approval, explicit date-only CRM transfer."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import urlencode

from control_followup import (ID, JOURNALS, MOSCOW, campaign_bindings, call_priority,
                              local_phones, safe_phone, sent_time)
from followup_task_phones import MARKER, NAME, phone_leads
from control_leads import DEFAULT_PARSER_DIR
from src.lexradar import espo_export as espo
from src.lexradar.crm_networks import network_keys


def evidence(journals):
    """Only structured findings bound to the exported PDF may affect ranking."""
    scores, networks = {}, {}
    for path in journals:
        if not path.exists():
            continue
        with path.with_suffix('.lock').open('r') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            state = json.loads(path.read_text())
        for key, entry in state.items():
            eid = entry.get('steps', {}).get('email', {}).get('id')
            if not eid:
                continue
            source = state.get(key.removesuffix(':draft'), {})
            networks[eid] = source.get('network') or entry.get('network') or {}
            try:
                material = source['result']['materials'][0]
                if not material.get('pdf') or material['sha256'] != entry['sha256']:
                    continue
                review_path = Path(material['path']).with_name('review.json')
                if review_path.stat().st_size > 1_000_000:
                    continue
                review = json.loads(review_path.read_text())
                if review['control_center']['hashes']['pdf'] != entry['sha256']:
                    continue
                priority, reason = call_priority(review)
                count = len({f['rule_id'] for f in review.get('findings', [])
                             if isinstance(f, dict) and f.get('confirmed') is True
                             and isinstance(f.get('rule_id'), int)})
                score = (priority, -count, reason)
                # Conflicting historical evidence must not silently win a ranking.
                scores[eid] = score if eid not in scores or scores[eid] == score else (1, 0, 'Противоречивые данные КП; нейтральный балл')
            except (OSError, ValueError, TypeError, KeyError, IndexError):
                continue
    return scores, networks


def local_blocks(directory):
    path = Path(directory) / 'followup.sqlite3'
    if not path.exists():
        return set()
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        return {r[0] for r in db.execute('SELECT account_id FROM blocked')} | {
            r[0] for r in db.execute("SELECT account_id FROM actions WHERE status != 'pending'")}


class SkipCandidate(ValueError):
    pass


def phone_for(task, account, row, *, explain=False):
    """Require agreement between available sources; invalid/optOut veto a number."""
    numbers, blocked = set(), set()
    for item in account.get('phoneNumberData') or []:
        number = safe_phone(item.get('phoneNumber'))
        if number:
            (blocked if item.get('invalid') or item.get('optOut') else numbers).add(number)
    for value in [account.get('phoneNumber'), row.get('phone'),
                  *re.findall(r'^Телефон: ([^\r\n]+)', task.get('description', ''), re.M)]:
        number = safe_phone(value)
        if number:
            numbers.add(number)
    # Treat equivalent Russian domestic/international representations as one phone.
    normalize = lambda n: '+7' + n[-10:] if len(n.lstrip('+')) == 11 and n.lstrip('+')[0] in '78' else n
    numbers = {normalize(n) for n in numbers}
    blocked = {normalize(n) for n in blocked}
    reason = ('optOut / неверный номер' if numbers & blocked or blocked and not numbers else
              'конфликт номеров' if len(numbers) > 1 else 'телефон отсутствует' if not numbers else '')
    if reason and explain:
        raise SkipCandidate(reason)
    return next(iter(numbers)) if len(numbers) == 1 and not numbers & blocked else None


def plan(api, leads, directory, journals=JOURNALS, now=None):
    now = now or datetime.now(MOSCOW)
    bindings, warnings = campaign_bindings(journals)
    if warnings or not bindings:
        raise ValueError('История экспорта неполна или отсутствует')
    scores, networks = evidence(journals)
    blocked = {aid: 'локальный отказ / обработано' for aid in local_blocks(directory)}
    keys = {eid: network_keys(row) for eid, row in networks.items()}
    key_accounts = {}
    for eid, group in keys.items():
        for key in group:
            key_accounts.setdefault(key, set()).add(bindings.get(eid, {}).get('account_id'))
    tasks, seen, offset = [], set(), 0
    while True:
        page = api.request('GET', 'Task?' + urlencode(dict(maxSize=200, offset=offset,
            orderBy='id', order='asc', **{'where[0][type]': 'startsWith',
            'where[0][attribute]': 'name', 'where[0][value]': NAME})))
        items = page.get('list')
        if not isinstance(items, list):
            raise ValueError('Неполная страница Task')
        for item in items:
            tid = item.get('id')
            if not isinstance(tid, str) or not ID.fullmatch(tid) or tid in seen:
                raise ValueError('Неоднозначный ID Task')
            seen.add(tid)
            tasks.append(api.get('Task', tid))
            if tasks[-1].get('id') != tid:
                raise ValueError('ID Task не подтверждён')
        offset += len(items)
        if not items:
            break
    for task in tasks:
        marker = MARKER.match(task.get('description') or '')
        aid = bindings.get(marker[1] if marker else None, {}).get('account_id')
        if aid and (task.get('status') in ('Completed', 'Canceled') or
                    task.get('callResult') not in (None, '', 'Не звонили')):
            blocked[aid] = 'завершено' if task.get('status') in ('Completed', 'Canceled') else 'результат звонка: ' + str(task.get('callResult'))
    candidates, skipped = [], Counter()
    for task in tasks:
        try:
            name = task.get('name', '')
            if task.get('deleted') or not (name == NAME or name.startswith(NAME + ' · ')):
                raise SkipCandidate('ошибка данных: название/удалённая Task')
            marker = MARKER.match(task.get('description') or '')
            eid = marker[1] if marker else None
            aid = bindings.get(eid, {}).get('account_id')
            if not aid or not ((task.get('parentType') == 'Account' and task.get('parentId') == aid
                    and task.get('accountId') in (None, '', aid)) or
                    (task.get('accountId') == aid and not task.get('parentId') and not task.get('parentType'))):
                raise SkipCandidate('ошибка данных: неоднозначная привязка')
            if aid in blocked:
                raise SkipCandidate(blocked[aid])
            if task.get('status') in (None, '', 'Completed', 'Canceled') or task.get('callResult') not in (None, '', 'Не звонили'):
                raise SkipCandidate('ошибка данных: статус / результат звонка')
            due = sent_time(task.get('dateEnd'))
            if due > now:
                raise SkipCandidate('срок не наступил')
            email, account = api.get('Email', eid), api.get('Account', aid)
            if (email.get('id') != eid or email.get('deleted') or email.get('status') != 'Sent'
                    or email.get('sendAt') or email.get('parentType') != 'Account' or email.get('parentId') != aid
                    or sent_time(email.get('dateSent')) > now or account.get('id') != aid or account.get('deleted')):
                raise SkipCandidate('ошибка данных: Email/Account не подтверждены')
            row = dict(email_id=eid, account_id=aid)
            if local_phones([row], leads, journals):
                raise SkipCandidate('ошибка данных: источники телефонов недоступны')
            if 'несколько разных' in row.get('phone_reason', ''):
                raise SkipCandidate('конфликт номеров')
            if account.get('doNotCall') or account.get('optOut'):
                raise SkipCandidate('optOut / Не беспокоить')
            phone = phone_for(task, account, row, explain=True)
            priority, negative_count, reason = scores.get(eid, (1, 0, 'Нет структурированных данных КП; нейтральный балл'))
            network = any(k.startswith('name:') or len(key_accounts[k] - {None}) > 1 for k in keys.get(eid, set()))
            reason += (f'; подтверждённых нарушений: {-negative_count}' if eid in scores else '') + '; ' + ('сеть' if network else 'признаков сети в локальных данных нет')
            candidates.append(((priority, negative_count, network, due, task['id']), aid,
                dict(task_id=task['id'], account_id=aid, email_id=eid,
                     name=account.get('name') or task.get('parentName') or name, phone=phone, reason=reason,
                     due=due.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M МСК'),
                     url='https://crm.prostoburo.com/#Task/view/' + task['id'])))
        except SkipCandidate as exc:
            skipped[str(exc)] += 1
        except (espo.Hold, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            # CRM errors can contain private response details; print only their category.
            skipped['ошибка данных: ' + type(exc).__name__] += 1
    result, accounts = [], set()
    for rank, aid, row in sorted(candidates):
        if aid not in accounts:
            result.append(row)
            accounts.add(aid)
        else:
            skipped['дубликат Account'] += 1
        if len(result) == 10:
            break
    return dict(mode='dry-run', found=len(tasks), selected=len(result), tasks=result,
                skipped=dict(skipped), note='Меньше 10, если пригодных уникальных Account недостаточно')


def save_local(directory, build, now=None):
    day = (now or datetime.now(MOSCOW)).astimezone(MOSCOW).date().isoformat()
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f'daily-call-plan-{day}.json'
    with path.with_suffix('.lock').open('a') as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            pass
        report = dict(build(), mode='save-local', date=day, state='proposed')
        # Publish a complete private file without ever replacing an approved plan.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                         prefix='.daily-call-plan-', delete=False) as tmp:
            temporary = Path(tmp.name)
            try:
                json.dump(report, tmp, ensure_ascii=False, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
                os.link(temporary, path)
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                temporary.unlink()
        return report


def metadata_field(api):
    metadata = api.request('GET', 'Metadata')
    fields = metadata.get('entityDefs', {}).get('Task', {}).get('fields')
    if not isinstance(fields, dict):
        raise ValueError('Metadata Task не подтверждена')
    field = fields.get('callPlanDate')
    return bool(isinstance(field, dict) and field.get('type') == 'date'
                and not field.get('readOnly') and not field.get('disabled'))


def build_plan(api, parser_dir, directory, journals=JOURNALS, now=None):
    try:
        available = metadata_field(api)
        message = ('callPlanDate доступно' if available else
                   'callPlanDate отсутствует или недоступно для записи; план работает локально')
    except (espo.Hold, OSError, ValueError, TypeError, AttributeError):
        available, message = False, 'Metadata не проверена; передача в CRM отключена, план работает локально'
    leads, errors = phone_leads(parser_dir, directory)
    if errors or not leads:
        raise ValueError('Локальные источники неполны')
    return dict(plan(api, leads, directory, journals, now), crm_field=available, metadata_note=message)


class DailyPlan:
    """One proposed/approved snapshot per Moscow day; no background CRM writer."""
    def __init__(self, directory, now=None):
        self.directory = Path(directory)
        self.now = now or datetime.now(MOSCOW)
        self.day = self.now.astimezone(MOSCOW).date().isoformat()
        self.path = self.directory / f'daily-call-plan-{self.day}.json'

    def load(self):
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        if (not isinstance(value, dict) or value.get('date') != self.day
                or not isinstance(value.get('tasks'), list) or len(value['tasks']) > 10
                or value.get('state', 'proposed') not in ('proposed', 'approved')):
            raise ValueError('План повреждён; требуется ручная сверка файла')
        ids = [row['task_id'] for row in value['tasks']]
        if len(set(ids)) != len(ids) or any(not isinstance(tid, str) or not ID.fullmatch(tid) for tid in ids):
            raise ValueError('Некорректные Task плана')
        return value

    @staticmethod
    def token(report):
        return hashlib.sha256(json.dumps(report['tasks'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def action(self, action, day, token='', *, build=None, api=None):
        if day != self.day:
            raise ValueError('Наступил новый день МСК; откройте сегодняшний план')
        if action == 'generate':
            return save_local(self.directory, build, self.now)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix('.lock').open('a') as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            report = self.load()
            if not report or token != self.token(report):
                raise ValueError('План не найден или форма устарела; обновите страницу')
            if action == 'approve':
                if report.get('state') == 'approved':
                    return report
                if len(report['tasks']) != 10:
                    raise ValueError('Для утверждения нужны 10 пригодных организаций; неподходящие задачи не добавляются')
                report.update(state='approved', approved_at=self.now.isoformat())
                espo.save(self.path, report)
            elif action == 'refresh':
                return report  # The redirected GET reads current CRM; never selects replacements.
            elif action == 'transfer':
                if report.get('state') != 'approved':
                    raise ValueError('Сначала утвердите план')
                if not metadata_field(api):
                    raise ValueError('callPlanDate отсутствует или недоступно; используйте локальный план')
                results = report.setdefault('transfer', {})
                intents = report.setdefault('transfer_intents', {})
                for row in report['tasks']:
                    tid = row['task_id']
                    try:
                        current = checked_task(api, row)
                        if current.get('callPlanDate') == self.day:
                            results[tid] = 'Подтверждено: callPlanDate=' + self.day
                        elif tid in intents:
                            # Never repeat a write whose response may have been lost.
                            results[tid] = 'Повтор PUT заблокирован; сверьте callPlanDate вручную'
                        elif current.get('callPlanDate'):
                            results[tid] = 'Пропущено: callPlanDate уже содержит другую дату'
                        else:
                            reason = live_reason(api, row, current, self.directory, self.now)
                            if reason:
                                results[tid] = 'Пропущено: ' + reason
                            else:
                                intents[tid] = self.day
                                results[tid] = 'Исход PUT неизвестен; требуется ручная сверка'
                                espo.save(self.path, report)
                                try:
                                    api.request('PUT', 'Task/' + tid, {'callPlanDate': self.day})
                                except (espo.Hold, OSError) as exc:
                                    results[tid] = 'PUT не подтверждён: ' + error_category(exc) + '; повтор запрещён, сверьте CRM'
                                after = checked_task(api, row)
                                ignored = {'callPlanDate', 'modifiedAt', 'modifiedById', 'modifiedByName'}
                                if (after.get('callPlanDate') == self.day and
                                        {k: v for k, v in current.items() if k not in ignored} ==
                                        {k: v for k, v in after.items() if k not in ignored}):
                                    results[tid] = 'Подтверждено: callPlanDate=' + self.day
                                elif after.get('callPlanDate') == self.day:
                                    results[tid] = 'Дата подтверждена, но другие поля Task изменились; требуется ручная сверка'
                    except (espo.Hold, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                        results[tid] = ('Исход PUT требует сверки; ' if tid in intents else '') + 'Ошибка чтения: ' + error_category(exc)
                    espo.save(self.path, report)
            else:
                raise ValueError('Неизвестное действие')
            return report


def error_category(exc):
    code = re.search(r'CRM HTTP (\d+)', str(exc))
    if str(exc).startswith('Привязка Task изменилась'):
        return 'привязка Task изменилась / не подтверждена'
    return 'HTTP ' + code[1] if code else type(exc).__name__


def checked_task(api, row):
    task = api.get('Task', row['task_id'])
    aid, eid = row.get('account_id'), row.get('email_id')
    marker = MARKER.match(task.get('description') or '')
    if not aid or not eid:
        # Older CLI snapshots contain only Task IDs; recover identity without changing the ten.
        bindings, warnings = campaign_bindings()
        eid = marker[1] if marker else None
        aid = bindings.get(eid, {}).get('account_id') if not warnings else None
    if (task.get('id') != row['task_id'] or task.get('deleted') or not aid or not eid
            or not marker or marker[1] != eid
            or not (task.get('name') == NAME or task.get('name', '').startswith(NAME + ' · '))
            or not ((task.get('parentType') == 'Account' and task.get('parentId') == aid
                     and task.get('accountId') in (None, '', aid)) or
                    (task.get('accountId') == aid and not task.get('parentId') and not task.get('parentType')))):
        raise ValueError('Привязка Task изменилась или старый снимок требует ручной сверки')
    return task


def live_reason(api, row, task, directory, now):
    if task.get('status') in ('Completed', 'Canceled'):
        return 'завершено'
    if task.get('callResult') not in (None, '', 'Не звонили'):
        return 'результат звонка: ' + str(task['callResult'])
    aid = row.get('account_id') or task.get('parentId') or task.get('accountId')
    if aid in local_blocks(directory):
        return 'локальный отказ / обработано'
    if not task.get('status'):
        return 'ошибка данных: статус отсутствует'
    if sent_time(task.get('dateEnd')) > now:
        return 'срок не наступил'
    account = api.get('Account', aid)
    if account.get('id') != aid or account.get('deleted'):
        return 'ошибка данных: Account'
    if account.get('doNotCall') or account.get('optOut'):
        return 'optOut / Не беспокоить'
    try:
        phone_for(task, account, row, explain=True)
    except SkipCandidate as exc:
        return str(exc)
    return ''


def plan_html(handler, directory, api_factory=espo.API, now=None):
    from control_pages import esc, link
    store = DailyPlan(directory, now)
    content = '<section id="daily-call-plan"><h2>План звонков на сегодня</h2><p>' + esc(store.day) + ' МСК</p>'
    try:
        report = store.load()
        fields = '<input type="hidden" name="day" value="' + store.day + '">'
        if not report:
            return content + handler.form('/followup/plan', 'Сформировать план', fields +
                '<input type="hidden" name="action" value="generate">', confirm=False) + '</section>'
        approved = report.get('state') == 'approved'
        content += '<p>Состояние: <strong>' + ('Утверждён' if approved else 'Предложен') + '</strong> · Организаций: ' + str(len(report['tasks'])) + '/10</p>'
        api = None
        try:
            api = api_factory()
            available = metadata_field(api)
            metadata_note = ('callPlanDate доступно' if available else
                             'callPlanDate отсутствует или недоступно для записи; план работает локально')
        except (espo.Hold, OSError, ValueError, TypeError, AttributeError):
            available = False
            metadata_note = 'Metadata не проверена; передача в CRM отключена, план работает локально'
        content += '<p>' + esc(metadata_note) + '</p>'
        fields += '<input type="hidden" name="plan_token" value="' + store.token(report) + '">'
        buttons = [('refresh', 'Обновить статусы')]
        if not approved:
            if len(report['tasks']) == 10:
                buttons.insert(0, ('approve', 'Утвердить план'))
        elif available:
            buttons.append(('transfer', 'Передать план в CRM'))
        content += ('<form method="post" action="/followup/plan"><input type="hidden" name="csrf" value="'
                    + esc(handler.server.csrf_token) + '">' + fields + ''.join(
                    '<button name="action" value="' + action + '">' + label + '</button> ' for action, label in buttons) + '</form>')
        content += '<p>Результат звонка отмечайте в Task EspoCRM (callResult). Состав плана фиксирован. Перед звонком обновите статусы и откройте Task.</p>'
        content += '<div style="overflow-x:auto"><table><thead><tr>' + ''.join('<th>' + label + '</th>' for label in
            ('Организация', 'Телефон', 'Причина выбора', 'Срок контроля', 'Актуальный статус CRM', 'Task', 'Передача')) + '</tr></thead><tbody>'
        for row in report['tasks']:
            phone, status = safe_phone(row.get('phone')) if api else None, 'Не проверено; откройте CRM перед звонком'
            try:
                if api:
                    task = checked_task(api, row)
                    reason = live_reason(api, row, task, store.directory, store.now)
                    status = str(task.get('status')) + ' · callResult: ' + str(task.get('callResult') or 'Не звонили')
                    status += ' · ' + ('Не звонить: ' + reason if reason else 'Доступно по проверке ' + store.now.strftime('%H:%M МСК'))
                    if reason:
                        phone = None
            except (espo.Hold, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                status = 'Не проверено: ' + error_category(exc) + '; откройте Task'
                phone = None
            content += '<tr>' + ''.join('<td>' + value + '</td>' for value in (
                esc(row.get('name', row['task_id'])), link('tel:' + phone, phone) if phone else esc(row.get('phone') or '—'),
                esc(row.get('reason', 'Сохранённый выбор')), esc(row.get('due', '—')), esc(status),
                link('https://crm.prostoburo.com/#Task/view/' + row['task_id'], 'Открыть Task'),
                esc(report.get('transfer', {}).get(row['task_id'], 'Не передавалось')))) + '</tr>'
        content += '</tbody></table></div>'
        if report.get('skipped'):
            content += '<details><summary>Причины пропуска кандидатов</summary><ul>' + ''.join(
                '<li>' + esc(reason) + ': ' + str(count) + '</li>' for reason, count in report['skipped'].items()) + '</ul></details>'
        if len(report['tasks']) < 10:
            content += '<p role="alert">Пригодных организаций меньше 10. Утверждение недоступно; проверьте причины пропуска. Снимок сохранён без добавления неподходящих задач.</p>'
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        content += '<p role="alert">План недоступен или повреждён; данные сохранены. Требуется ручная сверка локального файла.</p>'
    return content + '</section>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--dry-run', action='store_true', help='Без сохранения (по умолчанию)')
    modes.add_argument('--save-local', action='store_true', help='Сохранить неизменный план на сегодня МСК')
    parser.add_argument('--parser-dir', type=Path, default=DEFAULT_PARSER_DIR)
    args = parser.parse_args()
    try:
        directory = Path(__file__).resolve().parent / '.control-center-state'
        def build():
            return build_plan(espo.API(), args.parser_dir, directory)
        report = save_local(directory, build) if args.save_local else build()
    except (espo.Hold, OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(json.dumps(dict(mode='save-local' if args.save_local else 'dry-run', complete=False, error_type=type(exc).__name__,
                              error='Чтение не подтверждено; план не сформирован'), ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
