"""Durable cumulative CRM queue. Legacy mailer files and its deals are read-only."""
import fcntl
import hashlib
import importlib
import json
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from src.lexradar import espo_export as espo
from control_pages import esc

LEGACY = Path('/home/golkonst/lexradar-mailer')
JOURNAL = Path.home() / '.local/state/lexradar/crm-prepare.json'
MOSCOW = ZoneInfo('Europe/Moscow')
UTC = timezone.utc


def legacy_modules():
    if str(LEGACY) not in sys.path:
        sys.path.insert(0, str(LEGACY))
    return importlib.import_module('mailer_v2'), importlib.import_module('lexradar_deals')


def next_slot(now, used):
    # Append after every existing future sendAt, including the protected old batch.
    after = max([now + timedelta(minutes=10), *used])
    local = after.astimezone(MOSCOW)
    for day in range(366):
        date = local.date() + timedelta(days=day)
        if date.weekday() < 5:
            for minute in range(9 * 60, 19 * 60, 30):
                slot = datetime(date.year, date.month, date.day, minute // 60,
                                minute % 60, tzinfo=MOSCOW)
                if slot > after:
                    return slot.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')
    raise ValueError('Нет свободного слота в течение года')


def all_rows(api, kind):
    rows = []
    for offset in range(0, 100000, 200):
        select = '&select=id,name,accountId,description,stage' if kind == 'Opportunity' else ''
        data = api.request('GET', f'{kind}?maxSize=200&offset={offset}&orderBy=id&order=asc' + select)
        page = data.get('list')
        if not isinstance(page, list) or any(not isinstance(r, dict) or not r.get('id') for r in page):
            raise ValueError('Неполный ответ CRM')
        rows.extend(page)
        if len({r['id'] for r in rows}) != len(rows):
            raise ValueError('Повтор страницы CRM')
        if len(page) < 200:
            if isinstance(data.get('total'), int) and data['total'] > len(rows):
                raise ValueError('Неполный список CRM')
            return rows
    raise ValueError('Слишком большой список CRM')


class Mailing:
    def __init__(self, directory, journal=JOURNAL, legacy=LEGACY):
        self.directory = Path(directory)
        self.path = self.directory / 'mailing.json'
        self.journal, self.legacy = Path(journal), Path(legacy)
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.error = ''

    @contextmanager
    def locked(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.directory / 'mailing.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(self.path.read_text()) if self.path.exists() else {}
            if not isinstance(state, dict):
                raise ValueError('Повреждён журнал очереди')
            yield state

    def select(self, selected, included):
        with self.locked() as state:
            path = self.directory / 'mailing-selection.json'
            choices = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(choices, dict):
                raise ValueError('Повреждён выбор рассылки')
            choices.update({'web-account-' + lead: lead in included for lead in selected})
            espo.save(path, choices)
            for row in state.values():
                if row.get('source_id') in choices:
                    row['mailing_enabled'] = choices[row['source_id']] is True
            espo.save(self.path, state)

    def add(self, state, result, row):
        if result.get('status') != 'DRAFT_VERIFIED' or not result.get('email_id'):
            return
        eid = result['email_id']
        path = self.directory / 'mailing-selection.json'
        choices = json.loads(path.read_text()) if path.exists() else {}
        included = choices.get(row.get('id'), True) is True
        if eid in state:
            state[eid]['source_id'] = row.get('id')
            state[eid]['mailing_enabled'] = included
        if not included:
            return
        archive_path = self.path.with_name('mailing-archive.json')
        if archive_path.exists() and eid in json.loads(archive_path.read_text()):
            return
        if not all(isinstance(result.get(k), str) and espo.re.fullmatch(r'[A-Za-z0-9_-]+', result[k]) for k in ('account_id', 'document_id', 'email_id')):
            raise ValueError('Нет подтверждённых Account/Document')
        record = dict(id=eid, source_id=row.get('id'), mailing_enabled=included, name=row.get('company_name', ''), email=row.get('email', '').lower(),
                      account_id=result['account_id'], document_id=result['document_id'],
                      recipient_verified=row.get('recipient_verified') is True,
                      pdf_sha256=row.get('pdf_sha256', ''), observed_draft=False, status='QUEUED',
                      reason='', slot=None)
        if eid not in state:
            state[eid] = record
        elif any(state[eid].get(k) != record[k] for k in ('account_id', 'document_id', 'email')):
            state[eid].update(status='HOLD', reason='Изменена привязка очереди', binding_hold=True)

    def enqueue(self, result, row):
        with self.locked() as state:
            self.add(state, result, row)
            espo.save(self.path, state)
        self.wake.set()

    def recover(self, state):
        if not self.journal.exists():
            return
        with self.journal.with_suffix('.lock').open('r') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            journal = json.loads(self.journal.read_text())
        for key, entry in journal.items():
            if key.startswith('web-account-') and not key.endswith(':draft'):
                result = entry.get('result', {})
                draft = journal.get(key + ':draft', {})
                binding = draft.get('binding', {})
                self.add(state, result, dict(id=key, company_name=binding.get('company_name', ''),
                    email=binding.get('email', ''), recipient_verified=bool(entry.get('draft_contact')),
                    pdf_sha256=draft.get('sha256', '')))

    def deal(self, api, row, stages, deals):
        if row.get('opportunity_id'):
            deal = api.get('Opportunity', row['opportunity_id'])
            if deal.get('accountId') != row['account_id'] or not deal.get('name', '').startswith('LexRadar ·'):
                raise ValueError('Изменена привязка сделки')
            if deal.get('stage') not in (stages.PREPARED, stages.SENT):
                raise ValueError('Сделка вручную переведена на другую стадию')
            if row['status'] == 'SENT' and deal.get('stage') == stages.PREPARED:
                api.request('PUT', 'Opportunity/' + deal['id'], {'stage': stages.SENT})
                if api.get('Opportunity', deal['id']).get('stage') != stages.SENT:
                    raise ValueError('Стадия сделки не подтверждена')
            return
        marker = 'LexRadar queue Email/' + row['id']
        matches = [d for d in deals if d.get('description') == marker and d.get('accountId') == row['account_id']]
        if len(matches) == 1:
            row['opportunity_id'] = matches[0]['id']
            espo.save(self.path, self.state)
            return self.deal(api, row, stages, deals)
        if matches or row.get('deal_intent') or any(d.get('accountId') == row['account_id']
                and d.get('name', '').startswith('LexRadar ·') for d in deals):
            raise ValueError('Требуется сверка существующей сделки/неизвестного POST')
        row['deal_intent'] = True
        espo.save(self.path, self.state)
        created = api.request('POST', 'Opportunity', dict(name='LexRadar · ' + row['name'],
            accountId=row['account_id'], stage=stages.PREPARED, amount=0,
            closeDate=(datetime.now(MOSCOW).date() + timedelta(days=30)).isoformat(), description=marker))
        row['opportunity_id'] = created['id']
        espo.save(self.path, self.state)
        deals.append(created)
        self.deal(api, row, stages, deals)

    def cycle(self, api=None, apply=True):
        mailer, stages = legacy_modules()
        with self.locked() as state:
            self.state = state
            archive_path = self.path.with_name('mailing-archive.json')
            archived = set(json.loads(archive_path.read_text())) if archive_path.exists() else set()
            self.recover(state)
            for eid in archived:
                state.pop(eid, None)
            old = {r['id']: r for r in json.loads((self.legacy / 'batch.json').read_text())}
            old_relations = json.loads((self.legacy / 'deals-state.json').read_text())
            for eid, item in old.items():
                if eid in archived:
                    continue
                state.setdefault(eid, dict(id=eid, name=item.get('name', ''), email='',
                                          status='HOLD', reason='Состояние CRM ещё не подтверждено', slot=None))
            espo.save(self.path, state)
            api = api or espo.API()
            config = mailer.config_at(self.legacy)
            # Share the existing mailer's lock, without writing its files.
            with (self.legacy / '.mailer.lock').open('r') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                emails = all_rows(api, 'Email')
                items = {e['id']: api.get('Email', e['id']) for e in emails
                         if e['id'] in state}
                missing = {}
                for eid in state.keys() - items.keys():
                    try:
                        items[eid] = api.get('Email', eid)
                        emails.append(items[eid])
                    except Exception:
                        missing[eid] = 'Письмо отсутствует или недоступно в CRM: GET Email/' + eid + ' не подтвердил запись; требуется сверка'
                used = [mailer.utc(e['sendAt']) for e in emails if e.get('sendAt')]
                deals = all_rows(api, 'Opportunity') if apply else []
                if apply and any(eid not in old and eid not in old_relations for eid in state):
                    options = api.request('GET', 'Metadata').get('entityDefs', {}).get('Opportunity', {}).get('fields', {}).get('stage', {}).get('options', [])
                    if stages.PREPARED not in options or stages.SENT not in options:
                        raise ValueError('Стадии сделки не подтверждены Metadata')
                counts = Counter(mailer.address(e) for e in items.values() if e.get('status') in ('Draft', 'Sent'))
                for eid, row in state.items():
                    try:
                        if eid in missing:
                            row.update(status='HOLD', reason=missing[eid],
                                       checked_at=mailer.stamp(datetime.now(UTC)))
                            continue
                        email = items[eid]
                        row.update(email=mailer.address(email) if eid in old else row['email'],
                                   slot=email.get('sendAt'), checked_at=mailer.stamp(datetime.now(UTC)))
                        row['status'] = ('SENT' if email.get('status') == 'Sent' else
                                         'SCHEDULED' if email.get('status') == 'Draft' and email.get('sendAt') else 'QUEUED')
                        row['reason'] = ''
                        if row['status'] == 'SCHEDULED' and mailer.utc(row['slot']) < datetime.now(UTC) - timedelta(minutes=20):
                            row['reason'] = 'Слот прошёл; CRM ещё не подтвердила Sent'
                        if eid in old or eid in old_relations:
                            if row['status'] == 'QUEUED':
                                row.update(status='HOLD', reason='Старая партия: автоматическое изменение запрещено')
                            continue
                        if email.get('status') == 'Sent' and not row.get('observed_draft'):
                            # Historical Sent records are read-only, including their deals.
                            continue
                        if email.get('status') == 'Draft':
                            row['observed_draft'] = True
                        if row.get('binding_hold'):
                            raise ValueError('Изменена привязка очереди')
                        if email.get('parentType') != 'Account' or email.get('parentId') != row['account_id'] or mailer.address(email) != row['email']:
                            raise ValueError('Изменён Account/получатель')
                        if email.get('status') not in ('Draft', 'Sent'):
                            raise ValueError('Неожиданный статус Email')
                        if row.get('mailing_enabled') is False and row['status'] not in ('SENT', 'SCHEDULED'):
                            row.update(status='EXCLUDED', reason='Оператор исключил из автоматической рассылки')
                            continue
                        if apply:
                            self.deal(api, row, stages, deals)
                        if row['status'] in ('SENT', 'SCHEDULED'):
                            continue
                        if row.get('schedule_intent'):
                            raise ValueError('Неизвестный исход PUT; требуется сверка, повтор запрещён')
                        if not row.get('recipient_verified'):
                            raise ValueError('Адресат не подтверждён')
                        validation, reason = mailer.validate(email, {}, config)
                        if validation != 'READY' or counts[row['email']] > 1:
                            raise ValueError(reason if validation != 'READY' else 'Повтор адресата в очереди')
                        if not apply:
                            continue
                        if not config['enabled'] or config['dry_run']:
                            row['reason'] = 'Планирование приостановлено настройками mailer'
                            continue
                        document = api.get('Document', row['document_id'])
                        files = email.get('attachmentsIds') or []
                        sha = row.get('pdf_sha256')
                        if not sha or not document.get('fileId') or len(files) != 1 or any(
                                hashlib.sha256(api.file(fid)).hexdigest() != sha for fid in [document['fileId'], *files]):
                            raise ValueError('PDF не подтверждён по SHA-256')
                        if api.get('Account', row['account_id']).get('deleted'):
                            raise ValueError('Account удалён')
                        used = [mailer.utc(e['sendAt']) for e in mailer.API.scheduled(api)]
                        used.extend(mailer.utc(r['schedule_intent']) for r in state.values() if r.get('schedule_intent'))
                        slot = next_slot(datetime.now(UTC), used)
                        current_config = mailer.config_at(self.legacy)
                        if not current_config['enabled'] or current_config['dry_run']:
                            raise ValueError('Настройки изменились')
                        if api.get('Email', eid) != email:
                            raise ValueError('Email изменён перед планированием')
                        row['schedule_intent'] = slot
                        espo.save(self.path, state)
                        api.request('PUT', 'Email/' + eid, {'sendAt': slot})
                        check = api.get('Email', eid)
                        if check.get('sendAt') != slot or check.get('status') not in ('Draft', 'Sent'):
                            raise ValueError('Планирование не подтверждено')
                        row.update(status='SENT' if check['status'] == 'Sent' else 'SCHEDULED', slot=slot)
                        used.append(mailer.utc(slot))
                    except ValueError as exc:
                        row.update(status=row['status'] if row['status'] in ('SENT', 'SCHEDULED') else 'HOLD', reason=str(exc))
                    except Exception:
                        row.update(status=row['status'] if row['status'] in ('SENT', 'SCHEDULED') else 'HOLD', reason='Проверка CRM/журнала не пройдена; требуется сверка')
                    finally:
                        espo.save(self.path, state)
            self.error = ''

    def start(self):
        def work():
            while not self.stop.is_set():
                self.wake.clear()
                try:
                    self.cycle()
                except Exception:
                    self.error = 'Сверка CRM не завершена; показаны последние сохранённые данные. Планирование заблокировано.'
                self.wake.wait(60)
        self.thread = threading.Thread(target=work, name='lexradar-mailing', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()

    def html(self, details=False, status=''):
        def timestamp(value):
            if not value:
                return None
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

        def local(value):
            return value.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M МСК') if value else '—'

        content = '<article><h2>Рассылка LexRadar</h2>'
        try:
            rows = list(json.loads(self.path.read_text()).values())
        except (OSError, ValueError, AttributeError):
            return content + '<p>Не определено: журнал очереди недоступен.</p><a href="/mailing">Подробнее</a></article>'
        now = datetime.now(UTC)
        today = now.astimezone(MOSCOW).date()
        c = Counter(r['status'] for r in rows)
        sent, scheduled, finish, nearest = 'Не определено', 'Не определено', None, None
        slots = []
        slots_valid = False
        reason = 'Нет подтверждённых данных CRM.'
        try:
            api = espo.API()
            emails = all_rows(api, 'Email')
            indexed = {e['id']: e for e in emails}
            current = {r['id']: api.get('Email', r['id']) for r in rows}
            sent = 0
            slots = []
            unknown_sent = False
            for row in rows:
                email = current[row['id']]
                if email.get('status') == 'Sent':
                    date = timestamp(email.get('dateSent'))
                    if date is None or date > now:
                        unknown_sent = True
                    elif date.astimezone(MOSCOW).date() == today:
                        sent += 1
                if email.get('status') == 'Draft' and email.get('sendAt'):
                    slot = timestamp(email['sendAt'])
                    if slot > now:
                        slots.append(slot)
            if unknown_sent:
                sent = 'Не определено (dateSent отсутствует или некорректен)'
            scheduled = sum(s.astimezone(MOSCOW).date() == today and s.astimezone(MOSCOW).weekday() < 5
                            and 9 <= s.astimezone(MOSCOW).hour < 19
                            and s.minute in (0, 30) and s.second == 0 for s in slots)
            nearest = min(slots) if slots else None
            pending = [r for r in rows if current[r['id']].get('status') != 'Sent']
            blocked = [r for r in pending if r['status'] in ('WAITING_APPROVAL', 'HOLD', 'ERROR')]
            unscheduled = [r for r in pending if not current[r['id']].get('sendAt')]
            overdue = any(current[r['id']].get('sendAt') and timestamp(current[r['id']]['sendAt']) <= now for r in pending)
            slots_valid = bool(slots) and len(set(slots)) == len(slots) and all(
                s.astimezone(MOSCOW).weekday() < 5 and 9 <= s.astimezone(MOSCOW).hour < 19
                and s.minute in (0, 30) and s.second == 0 and s.microsecond == 0 for s in slots)
            # Unscheduled rows lack the final PDF/config/binding checks; never assume admission.
            if slots and not slots_valid:
                reason = 'Будущие слоты совпадают или не соответствуют графику; требуется сверка.'
            elif blocked:
                reason = 'Есть заблокированные письма WAITING_APPROVAL / HOLD / ERROR.'
            elif unscheduled:
                reason = 'Допуск неназначенных писем к планированию ещё не подтверждён.'
            elif overdue:
                reason = 'Есть прошедшие слоты без подтверждения Sent.'
            elif slots:
                finish = max(slots)
                reason = 'По уже назначенным sendAt; пн–пт 09:00–19:00 МСК, шаг 30 минут.'
            else:
                reason = 'Нет ожидающих отправлений.'
            if len(indexed) != len(emails):
                raise ValueError('Неполная сверка')
        except Exception:
            sent, scheduled, finish, nearest = 'Не определено', 'Не определено', None, None
            slots_valid = False
            reason = 'Сверка CRM недоступна или неполна; прогноз неизвестен.'
        # Forecast only the confirmed future CRM slots.
        # Blocked/unscheduled records remain visible as separate warnings.
        valid = slots_valid and isinstance(scheduled, int)
        if valid:
            finish = max(slots)
        else:
            finish = None
        days = len(slots) / 20 if valid else 0
        color = '#16803c' if days >= 5 else '#b77900' if days >= 1 else '#c62828'
        status_text = 'Запас достаточный' if days >= 5 else 'Пора пополнять очередь' if days >= 1 else 'Срочно пополнить очередь'
        reserve = f'{days:.1f}'.replace('.', ',') if valid else '—'
        if not valid:
            color, status_text = '#64748b', 'Прогноз недоступен'

        def metric(title, number, subtitle, accent=None):
            style = ('color:' + accent + ';') if accent else ''
            return ('<div style="background:#f5f7fb;border:1px solid #e6eaf0;'
                    'border-radius:12px;padding:24px;min-width:0">'
                    '<div style="font-size:22px;color:#64748b;margin-bottom:10px">' + esc(title) + '</div>'
                    '<div style="font-size:52px;font-weight:750;line-height:1.2;' + style + '">' + esc(str(number)) + '</div>'
                    '<div style="font-size:20px;color:#64748b;margin-top:8px">' + esc(subtitle) + '</div></div>')

        content += '<div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:18px 0">'
        content += metric('В очереди', c['QUEUED'] + c['SCHEDULED'] + c['WAITING_APPROVAL'], 'писем к отправке')
        content += metric('Отправится сегодня', scheduled, 'по графику')
        content += metric('Отправлено сегодня', sent, 'по данным CRM')
        content += metric('Запас рассылки', reserve, 'дня по 20 слотов · ' + status_text, color)
        content += '</div>'
        content += '<div style="border-top:1px solid #e6eaf0;padding-top:14px;margin-top:8px">'
        content += '<p style="font-weight:600;margin:0 0 6px">Последний подтверждённый будущий слот: ' + esc(local(finish) if finish else 'Не определено') + '</p>'
        content += '<p style="color:#64748b;font-size:20px;margin:0">Ближайшая отправка: ' + esc(local(nearest)) + '</p>'
        content += '</div>'
        content += '<p>Запас учитывает только назначенные будущие слоты; это не срок завершения всей очереди и не подтверждение доставки.</p>'
        if details:
            content += '<p style="color:#64748b;font-size:12px">' + esc(reason) + '</p>'
        content += '<p style="color:#64748b;font-size:12px">Данные очереди — последняя сохранённая сверка; сегодня — по Москве.</p>'
        if not details:
            return content + '<a href="/mailing">Подробнее</a></article>'
        content += '<form method="get" action="/mailing"><label>Статус <select name="status"><option value="">Все</option>'
        for value in sorted(c):
            content += '<option value="' + esc(value) + '"' + (' selected' if status == value else '') + '>' + esc(value) + '</option>'
        content += '</select></label> <button>Показать</button></form><table><thead><tr><th>Организация</th><th>Email</th><th>Статус</th><th>Слот</th><th>Причина</th></tr></thead><tbody>'
        for row in rows:
            if status and row['status'] != status:
                continue
            try:
                slot = local(timestamp(row.get('slot')))
            except (ValueError, TypeError):
                slot = 'Не определено'
            content += '<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in (row.get('name', ''), row.get('email', ''), row['status'], slot, row.get('reason', ''))) + '</tr>'
        return content + '</tbody></table></article>'


if __name__ == '__main__':
    # GET-only check uses a disposable journal; it cannot schedule or create deals.
    import tempfile
    import shutil
    with tempfile.TemporaryDirectory(prefix='lexradar-mailing-check-') as temporary:
        current = Path(__file__).resolve().parent / '.control-center-state/mailing.json'
        if current.exists():
            shutil.copy2(current, Path(temporary) / 'mailing.json')
        queue = Mailing(temporary)
        try:
            queue.cycle(apply=False)
            state = json.loads(queue.path.read_text())
            print('GET-only:', dict(Counter(r['status'] for r in state.values())))
        except Exception:
            print('HOLD: GET-проверка CRM не завершена; рабочие журналы и CRM не изменены')
            raise SystemExit(1)
