"""Automatic Sent observations and explicitly selected CRM call tasks."""
import fcntl
import json
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from src.lexradar import espo_export as espo
from control_pages import esc, link

MOSCOW = ZoneInfo('Europe/Moscow')
UTC = timezone.utc
ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
STATES = {'pending', 'deferred', 'no_answer', 'completed', 'do_not_contact'}
GROUPS = ('Ожидают срока', 'Пора проверить', 'Отложены', 'Завершены')
GROUP_ORDER = (1, 0, 2, 3)
LABELS = {'pending': 'Ожидает обработки', 'deferred': 'Отложено', 'no_answer': 'Не дозвонился',
          'completed': 'Обработано', 'do_not_contact': 'Не связываться'}
PDF_WARNING = 'Связь части КП не подтверждена; ссылки на них скрыты.'
JOURNALS = (Path.home() / '.local/state/lexradar/crm-prepare.json',
            Path.home() / '.local/state/lexradar/espo-export.json')


def sent_time(value):
    """Espo's timezone-less dateSent is UTC; other input must carry an offset."""
    if not isinstance(value, str) or not value:
        raise ValueError('Нет корректной даты отправки')
    if re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d', value):
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=UTC)
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Требуется часовой пояс')
    return result.astimezone(UTC)


def control_date(sent):
    if sent.tzinfo is None or sent.utcoffset() is None:
        raise ValueError('Требуется часовой пояс')
    result = sent.astimezone(MOSCOW)
    days = 0
    while days < 3:
        result += timedelta(days=1)
        days += result.weekday() < 5
    return result


def check_local_call_action(directory, eid, aid):
    """Do not turn a local refusal or an already handled call into a new CRM task."""
    path = Path(directory) / 'followup.sqlite3'
    if not path.exists():
        return
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        if db.execute('SELECT 1 FROM blocked WHERE account_id=?', (aid,)).fetchone():
            raise ValueError('Task: для Account сохранён отказ от контактов')
        action = db.execute('SELECT account_id,status FROM actions WHERE email_id=?', (eid,)).fetchone()
        if action and (action[0] != aid or action[1] != 'pending'):
            raise ValueError('Task: сохранено действие оператора; требуется ручная сверка контроля')


def task_create_acl(api):
    data = api.request('GET', 'App/user')
    acl = data.get('acl') if isinstance(data, dict) else None
    table = acl.get('table') if isinstance(acl, dict) else None
    task = table.get('Task') if isinstance(table, dict) else None
    if (not isinstance(task, dict) or task.get('create') != 'yes'
            or acl.get('assignmentPermission') != 'all'):
        raise ValueError('Task: App/user не подтвердил create=yes и assignmentPermission=all; POST запрещён')


# The original POST outcome is unresolved; recovery is GET-only even if its intent is damaged.
TASK_POST_BLOCKED = frozenset({'6ae97b4789921f041'})


def ensure_call_task(api, row, email, save, *, apply=True):
    """Both mailing locks must be held; apply=False performs GET checks without saving."""
    if 'task_id' in row:
        if not isinstance(row['task_id'], str) or not ID.fullmatch(row['task_id']):
            raise ValueError('Task: повреждён task_id; требуется сверка')
        return 'CONFIRMED'
    eid, aid = row['id'], row.get('account_id')
    if (not isinstance(eid, str) or not ID.fullmatch(eid)
            or not isinstance(aid, str) or not ID.fullmatch(aid) or row.get('binding_hold')
            or email.get('id') != eid or email.get('deleted') or email.get('sendAt')
            or email.get('status') != 'Sent' or email.get('parentType') != 'Account'
            or email.get('parentId') != aid):
        raise ValueError('Task: не подтверждена привязка отправленного Email к Account')
    sent = sent_time(email.get('dateSent'))
    if sent > datetime.now(UTC):
        raise ValueError('Task: дата отправки находится в будущем')
    marker = 'LexRadar followup Email/' + eid
    matches = api.request('GET', 'Task?' + urlencode({'maxSize': 2,
        'where[0][type]': 'startsWith', 'where[0][attribute]': 'description', 'where[0][value]': marker + '\n'}))
    tasks = matches.get('list')
    if (not isinstance(tasks, list) or len(tasks) > 1
            or matches.get('total', len(tasks)) != len(tasks)
            or any(not isinstance(t, dict) or not isinstance(t.get('id'), str)
                   or not ID.fullmatch(t['id']) for t in tasks)):
        raise ValueError('Task: неоднозначный результат сверки задач')
    if tasks:
        task = api.get('Task', tasks[0]['id'])
        if (task.get('id') != tasks[0]['id'] or task.get('deleted')
                or not isinstance(task.get('description'), str)
                or not task['description'].startswith(marker + '\n')
                or not (task.get('parentType') == 'Account' and task.get('parentId') == aid
                        or task.get('accountId') == aid and not task.get('parentId')
                        and not task.get('parentType'))):
            raise ValueError('Task: изменена привязка найденной задачи')
        if not apply:
            return 'FOUND'
    else:
        if 'task_intent' in row or 'task_post_id' in row or eid in TASK_POST_BLOCKED:
            raise ValueError('Task: неизвестный исход POST; повтор запрещён, требуется сверка')
        task_create_acl(api)
        try:
            metadata = api.request('GET', 'Metadata')
        except (espo.Hold, OSError) as exc:
            raise ValueError('Task: GET Metadata недоступен; схема не проверена: ' + str(exc)) from exc
        definitions = metadata.get('entityDefs') if isinstance(metadata, dict) else None
        definition = definitions.get('Task') if isinstance(definitions, dict) else None
        fields = definition.get('fields') if isinstance(definition, dict) else None
        if not isinstance(fields, dict):
            keys = ', '.join(sorted(metadata)) if isinstance(metadata, dict) else type(metadata).__name__
            raise ValueError('Task: структура GET Metadata не подтверждена '
                             '(ожидался entityDefs.Task.fields; корневые ключи: ' + keys +
                             '). Это не означает отсутствия Task; нужна сверка ответа API')
        required = {'parent', 'name', 'description', 'assignedUser', 'dateEnd', 'status'}
        missing = sorted(required - fields.keys())
        invalid = sorted(k for k in required & fields.keys() if not isinstance(fields[k], dict))
        if missing or invalid:
            raise ValueError('Task: Metadata: отсутствуют поля: ' + ', '.join(missing) +
                             '; некорректные определения: ' + ', '.join(invalid))
        readonly = sorted(k for k in required if fields[k].get('readOnly'))
        if readonly:
            raise ValueError('Task: Metadata: readOnly у полей ' + ', '.join(readonly) +
                             '; наличие поля не подтверждает запись; POST запрещён')
        parent = fields['parent']
        if (parent.get('type') != 'linkParent'
                or not isinstance(parent.get('entityList'), list)
                or 'Account' not in parent['entityList']):
            raise ValueError('Task: Metadata: parent не подтверждает linkParent с Account; POST запрещён')
        for attribute in ('parentType', 'parentId'):
            if attribute in fields and (not isinstance(fields[attribute], dict)
                                        or fields[attribute].get('readOnly')):
                raise ValueError('Task: Metadata: запись ' + attribute + ' не подтверждена; POST запрещён')
        options = fields['status'].get('options')
        if not isinstance(options, list) or 'Not Started' not in options:
            raise ValueError('Task: Metadata: status.options не подтверждает Not Started; POST запрещён')
        users = api.request('GET', 'User?' + urlencode({'maxSize': 2,
            'where[0][type]': 'equals', 'where[0][attribute]': 'userName', 'where[0][value]': 'admin'}))
        admins = users.get('list')
        if (not isinstance(admins, list) or len(admins) != 1 or users.get('total', 1) != 1
                or not isinstance(admins[0], dict) or admins[0].get('userName') != 'admin'
                or admins[0].get('isActive') is not True or admins[0].get('deleted')
                or not isinstance(admins[0].get('id'), str) or not ID.fullmatch(admins[0]['id'])):
            raise ValueError('Task: единственный активный пользователь admin не подтверждён')
        account = api.get('Account', aid)
        if account.get('id') != aid or account.get('deleted') or not isinstance(account.get('name'), str) or not account['name']:
            raise ValueError('Task: организация недоступна или не имеет названия')
        due = control_date(sent).astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')
        url = espo.API_URL.split('/api/')[0] + '/#Email/view/' + eid
        payload = dict(name='Контрольный звонок LexRadar · ' + account['name'],
            parentType='Account', parentId=aid, assignedUserId=admins[0]['id'], status='Not Started', dateEnd=due,
            description=marker + '\nОрганизация: ' + account['name'] +
                '\nТелефон: ' + (account_phone(account) or 'не указан') +
                '\nКП отправлено: ' + sent.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M МСК') +
                '\nПисьмо: ' + url)
        if not apply:
            return 'READY'
        if api.get('Email', eid) != email:
            raise ValueError('Task: Email изменён перед созданием задачи')
        row['task_intent'] = payload
        save()
        created = api.request('POST', 'Task', payload)
        if not isinstance(created.get('id'), str) or not ID.fullmatch(created['id']):
            raise ValueError('Task: CRM не вернула корректный ID; повтор POST запрещён')
        row['task_post_id'] = created['id']
        save()
        task = api.get('Task', created['id'])
        if (task.get('id') != created['id'] or task.get('deleted')
                or any(task.get(key) != value for key, value in payload.items())):
            raise ValueError('Task: создание не подтверждено контрольным GET')
    if row.get('task_post_id') and row['task_post_id'] != task['id']:
        raise ValueError('Task: найденная задача не совпадает с сохранённым ответом POST')
    row['task_id'] = task['id']
    row.pop('task_error', None)
    save()
    return 'CONFIRMED'


def campaign_bindings(journals=JOURNALS):
    """Exact saved export IDs, never a subject substring or a shared Account."""
    bindings, conflicts, warnings = {}, set(), []
    for path in journals:
        if not path.exists():
            continue
        try:
            with path.with_suffix('.lock').open('r') as lock:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                if path.stat().st_size > 8_000_000:
                    raise ValueError()
                journal = json.loads(path.read_text())
            if not isinstance(journal, dict):
                raise ValueError()
            validated = []
            for entry in journal.values():
                if not isinstance(entry, dict):
                    raise ValueError()
                steps = entry.get('steps', {})
                eid = steps.get('email', {}).get('id')
                aid = entry.get('account_id')
                if not eid:
                    continue
                if not all(isinstance(x, str) and ID.fullmatch(x) for x in (eid, aid)):
                    raise ValueError()
                doc = steps.get('document', {}).get('id')
                action = steps.get('email_attachment', {})
                # Only the exact Email attachment binding can identify its proposal.
                doc = doc if (isinstance(doc, str) and ID.fullmatch(doc)
                              and action.get('document_id') == doc and action.get('id')
                              and isinstance(action.get('sourceId'), str) and ID.fullmatch(action['sourceId'])
                              and re.fullmatch(r'[a-f0-9]{64}', str(entry.get('sha256', '')))
                              and action.get('sha256') == entry.get('sha256')) else None
                binding = {'account_id': aid, 'document_id': doc,
                           'attachment_id': action.get('id') if doc else None,
                           'source_id': action.get('sourceId') if doc else None}
                validated.append((eid, binding))
            for eid, binding in validated:
                if eid in bindings and bindings[eid] != binding:
                    conflicts.add(eid)
                bindings[eid] = binding
        except (OSError, ValueError, TypeError, AttributeError):
            warnings.append('История экспорта недоступна или повреждена; журналов: 1; число непроверенных записей в нём неизвестно.')
    for eid in conflicts:
        bindings.pop(eid, None)
    if conflicts:
        warnings.append('Непроверенных записей: ' + str(len(conflicts)) + '; категория: противоречивая привязка экспорта.')
    return bindings, [message + (' Случаев: ' + str(count) + '.' if count > 1 else '')
                      for message, count in Counter(warnings).items()]


def task_state(row):
    if 'task_id' in row:
        return 'CONFIRMED' if isinstance(row['task_id'], str) and ID.fullmatch(row['task_id']) else 'RECONCILE_ONLY'
    if row.get('id') in TASK_POST_BLOCKED or any(k in row for k in ('task_intent', 'task_post_id')):
        return 'RECONCILE_ONLY'
    return 'AVAILABLE'


def call_available(row):
    return (row['group'] == 1 and row['status'] == 'pending'
            and row['task_state'] == 'AVAILABLE' and not row['account_task_blocked'])


def deadline_label(row):
    if row['group'] == 3:
        return 'Контроль завершён'
    if row['group'] == 1 and row['status'] == 'deferred':
        return 'Отсрочка просрочена'
    if row['group'] == 1 and row['status'] == 'no_answer':
        return 'Пора повторить контроль'
    return GROUPS[row['group']]


def local_phones(rows, leads, journals):
    """Use an exact saved export identity, never a similar name or domain."""
    sources = {alias: lead for lead in leads for alias in lead.get('aliases', [lead['id']])}
    candidates, matched, failures = {}, set(), Counter()
    for path in journals:
        if not path.exists():
            continue
        try:
            with path.with_suffix('.lock').open('r') as lock:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                if path.stat().st_size > 8_000_000:
                    raise ValueError()
                journal = json.loads(path.read_text())
            for key, entry in journal.items():
                lead = sources.get(key.removesuffix(':draft').removeprefix('web-account-'))
                if not lead:
                    continue
                eid = entry.get('steps', {}).get('email', {}).get('id')
                aid = entry.get('account_id')
                matched.add((eid, aid))
                for member in lead.get('members', [lead]):
                    for phone in re.split(r'[;\n]', member.get('phone', '')):
                        if safe_phone(phone):
                            candidates.setdefault((eid, aid), {})[safe_phone(phone)] = (phone.strip(),
                                'Импортный CSV' if str(member.get('source', '')).endswith('.csv') else 'Локальная организация Parser')
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            failures[type(exc).__name__] += 1
    for row in rows:
        phones = candidates.get((row['email_id'], row['account_id']), {})
        if safe_phone(row.get('phone')):
            row['phone_source'] = row.get('phone_source') or 'EspoCRM Account; сохранённая сверка'
            continue
        if len(phones) == 1:
            row['phone'], row['phone_source'] = next(iter(phones.values()))
            row['phone_source'] += '; точная привязка экспорта'
        else:
            reason = ('найдено несколько разных локальных номеров' if len(phones) > 1 else
                      'в привязанной локальной организации/CSV нет пригодного номера'
                      if (row['email_id'], row['account_id']) in matched else
                      'нет точной связи с локальной организацией/CSV в журнале экспорта')
            row['phone_reason'] = (row.get('phone_reason') or 'В снимке Account нет пригодного телефона.') + ' ' + reason + '.'
            if failures:
                row['phone_reason'] += ' Часть журналов источников недоступна или повреждена.'
    return ['Источники локальных телефонов: ' + category + ' — журналов ' + str(count)
            for category, count in sorted(failures.items())]


def mailing_records(directory):
    records = {}
    for name in ('mailing-archive.json', 'mailing.json'):
        path = Path(directory) / name
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or any(not isinstance(r, dict) or r.get('id') != eid
                or not ID.fullmatch(eid) for eid, r in data.items()):
            raise ValueError('Повреждён журнал рассылки')
        records.update(data)
    return records


def control_bindings(directory, journals):
    bindings, warnings = campaign_bindings(journals)
    for eid, row in mailing_records(directory).items():
        aid = row.get('account_id')
        if row.get('binding_hold') or not isinstance(aid, str) or not ID.fullmatch(aid):
            bindings.pop(eid, None)
            warnings.append('Непроверенная привязка очереди: 1 запись требует сверки.')
        elif eid in bindings and bindings[eid]['account_id'] != aid:
            bindings.pop(eid)
            warnings.append('Противоречивая привязка очереди: 1 запись исключена.')
        else:
            bindings.setdefault(eid, dict(account_id=aid, document_id=None,
                                          attachment_id=None, source_id=None))
    return bindings, [message + (' Случаев: ' + str(count) + '.' if count > 1 else '')
                      for message, count in Counter(warnings).items()]


def call_priority(review):
    findings = [f for f in review.get('findings', []) if isinstance(f, dict)
                and f.get('confirmed') is True]
    rules = {f.get('rule_id') for f in findings}
    if rules & {1, 2, 3, 5, 6, 7}:
        return 0, 'Существенные замечания: политика, формы, согласия, документы или сбор данных'
    opportunities = [o for o in review.get('commercial_opportunities', [])
                     if isinstance(o, dict) and o.get('status') == 'READY']
    texts = [str(o.get('site_object', '')) + ' ' + str(o.get('observed_fact', '')) for o in opportunities]
    if not findings and texts and all(re.search(r'футер|footer|копирайт|copyright', text, re.I)
            and re.search(r'устар|прошл|неактуаль|год|20[0-9]{2}', text, re.I) for text in texts):
        return 2, 'Только устаревший футер; низкий приоритет звонка'
    return 1, 'Сверить КП: прочие замечания или недостаточно данных о существенности'


def call_priorities(journals):
    result = {}
    for path in journals:
        if not path.exists():
            continue
        try:
            with path.with_suffix('.lock').open('r') as lock:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                journal = json.loads(path.read_text())
            for key, entry in journal.items():
                try:
                    eid = entry.get('steps', {}).get('email', {}).get('id')
                    source = journal.get(key.removesuffix(':draft'), {})
                    materials = source.get('result', {}).get('materials', [])
                    if not eid or not materials:
                        continue
                    material = materials[0]
                    if not material.get('pdf') or material.get('sha256') != entry.get('sha256'):
                        continue
                    path_review = Path(material['path']).with_name('review.json')
                    if path_review.stat().st_size > 1_000_000:
                        continue
                    review = json.loads(path_review.read_text())
                    if review.get('control_center', {}).get('hashes', {}).get('pdf') == entry['sha256']:
                        result[eid] = call_priority(review)
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    continue
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return result


def read_sent(api, bindings):
    """GET every exact campaign ID, including historical mailing records."""
    rows, warnings, excluded = [], [], set()
    failures = Counter()
    get = lambda route: api.request('GET', route)
    try:
        candidates = sorted(bindings)
        accounts = {}
        for eid in candidates:
            stage = 'Email: чтение'
            try:
                email = get('Email/' + eid)
                binding = bindings[eid]
                aid = binding['account_id']
                if (email.get('id') != eid or email.get('deleted') or email.get('status') != 'Sent' or email.get('sendAt')
                        or email.get('parentType') != 'Account' or email.get('parentId') != aid):
                    excluded.add(eid)
                    if (email.get('id') == eid and not email.get('deleted')
                            and email.get('status') in {'Draft', 'Sending', 'Failed', 'Archived'}):
                        continue
                    failures['Email: привязка или статус'] += 1
                    continue
                stage = 'Дата отправки'
                try:
                    sent = sent_time(email.get('dateSent'))
                    if sent > datetime.now(UTC):
                        raise ValueError('future')
                except ValueError:
                    excluded.add(eid)
                    raise
                stage = 'Account: чтение или привязка'
                if aid not in accounts:
                    accounts[aid] = get('Account/' + aid)
                account = accounts[aid]
                if account.get('id') != aid or account.get('deleted'):
                    raise ValueError('account')
                doc = binding['document_id'] if binding['attachment_id'] in (email.get('attachmentsIds') or []) else None
                if doc:
                    try:
                        document = get('Document/' + doc)
                        links = get('Document/' + doc + '/accounts?maxSize=200')
                        linked = links.get('list')
                        if (document.get('id') != doc or document.get('deleted')
                                or document.get('fileId') != binding['source_id']
                                or not isinstance(linked, list) or len(linked) >= 200
                                or [a.get('id') for a in linked] != [aid]
                                or (isinstance(links.get('total'), int) and links['total'] > len(linked))):
                            raise ValueError('document binding')
                    except (espo.Hold, OSError, ValueError, TypeError, AttributeError):
                        doc = None
                        warnings.append(PDF_WARNING)
                if not doc:
                    warnings.append(PDF_WARNING)
                phone_account = dict(account)
                numbers = account.get('phoneNumberData')
                if isinstance(numbers, list) and any(isinstance(n, dict)
                        and safe_phone(n.get('phoneNumber')) == safe_phone(account.get('phoneNumber'))
                        and (n.get('invalid') or n.get('optOut')) for n in numbers):
                    phone_account['phoneNumber'] = ''
                phone = account_phone(phone_account)
                rows.append(dict(email_id=eid, account_id=aid, document_id=doc,
                                 date_sent=sent.isoformat(), source_id=binding.get('source_id'), name=str(account.get('name') or 'Без названия')[:500],
                                 phone=phone,
                                 phone_source='EspoCRM Account' if phone else '',
                                 phone_reason='' if phone else
                                     'В Account нет пригодного телефона (номер отсутствует, некорректен или исключён).',
                                 website=str(account.get('website') or '')[:2000],
                                 checked_at=datetime.now(UTC).isoformat()))
            except (espo.Hold, OSError, ValueError, TypeError, AttributeError) as exc:
                # Report categories only: exception bodies can contain CRM credentials.
                http = re.search(r'CRM HTTP ([0-9]{3})', str(exc))
                category = 'HTTP ' + http[1] if http else type(exc).__name__
                failures[stage + ' / ' + category] += 1
    except (espo.Hold, OSError, ValueError, TypeError, AttributeError):
        warnings.append('CRM недоступна или список неполон; полнота очереди не подтверждена.')
    if failures:
        warnings.append('Непроверенных записей: ' + str(sum(failures.values())) + '. Категории: ' +
                        '; '.join(category + ' — ' + str(count) for category, count in sorted(failures.items())) + '.')
    return rows, list(dict.fromkeys(warnings)), excluded


class Followup:
    def __init__(self, directory, journals=JOURNALS):
        self.path = Path(directory) / 'followup.sqlite3'
        self.journals = journals
        self.lock = threading.Lock()
        self.refreshed = 0
        self.warnings = []
        self.unavailable = False
        self.stop = threading.Event()

    def connect(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # SQLite commits atomically; this file is independent of mailing/CRM journals.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS snapshot (email_id TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS actions (email_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    status TEXT NOT NULL, due TEXT, comment TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, email_id TEXT NOT NULL,
                    account_id TEXT NOT NULL, status TEXT NOT NULL, due TEXT, comment TEXT NOT NULL,
                    at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS blocked (account_id TEXT PRIMARY KEY, at TEXT NOT NULL);
            ''')
        except Exception:
            db.close()
            raise
        return db

    def start(self):
        self.unavailable = True
        self.warnings = ['Автоматическая сверка контроля выполняется; показан сохранённый снимок.']
        def work():
            while not self.stop.is_set():
                try:
                    self.refresh()
                except Exception as exc:
                    self.unavailable = True
                    self.warnings = ['Автоматическая сверка контроля не завершена; число непроверенных записей неизвестно; категория: ' + type(exc).__name__ + '. Показан сохранённый снимок.']
                self.stop.wait(60)
        threading.Thread(target=work, name='lexradar-followup', daemon=True).start()

    def refresh(self, api_factory=None):
        with self.lock:
            if time.monotonic() - self.refreshed < 60:
                return
            bindings, warnings = control_bindings(self.path.parent, self.journals)
            rows, excluded = [], set()
            if bindings:
                try:
                    rows, errors, excluded = read_sent((api_factory or espo.API)(), bindings)
                    warnings += errors
                except (espo.Hold, OSError, ValueError) as exc:
                    warnings.append('Непроверенных записей: ' + str(len(bindings)) +
                                    '; категория: подключение CRM / ' + type(exc).__name__ + '.')
            else:
                warnings.append('Нет достоверных привязок Email к кампании LexRadar; посторонние письма исключены.')
            with closing(self.connect()) as db, db:
                if not any(w != PDF_WARNING for w in warnings):
                    db.execute('DELETE FROM snapshot')
                db.executemany('DELETE FROM snapshot WHERE email_id=?', [(eid,) for eid in excluded])
                db.executemany('INSERT OR REPLACE INTO snapshot VALUES (?, ?)',
                               [(r['email_id'], json.dumps(r, ensure_ascii=False)) for r in rows])
            self.unavailable = any(w != PDF_WARNING for w in warnings)
            self.warnings = list(dict.fromkeys(warnings))
            self.refreshed = time.monotonic()

    def queue(self, now=None):
        now = now or datetime.now(UTC)
        bindings, warnings = control_bindings(self.path.parent, self.journals)
        with closing(self.connect()) as db:
            actions = {r[0]: r for r in db.execute('SELECT * FROM actions')}
            blocked = {r[0] for r in db.execute('SELECT account_id FROM blocked')}
            records = [json.loads(r[0]) for r in db.execute('SELECT record FROM snapshot')]
        mailing = mailing_records(self.path.parent)
        task_accounts = {r.get('account_id') for r in mailing.values() if task_state(r) != 'AVAILABLE'}
        priorities = call_priorities(self.journals)
        rows = []
        for row in records:
            eid, aid = row['email_id'], row['account_id']
            if bindings.get(eid, {}).get('account_id') != aid:
                continue
            if (bindings[eid].get('document_id') != row.get('document_id')
                    or bindings[eid].get('source_id') != row.get('source_id')):
                row['document_id'] = None
            if sent_time(row['date_sent']) > now:
                warnings.append('Дата отправки в будущем; запись исключена.')
                continue
            action = actions.get(eid)
            if action and action[1] != aid:
                warnings.append('Сохранённое действие относится к другой организации; письмо исключено.')
                continue
            status, due, comment, revision = (action[2:] if action else ('pending', None, '', 0))
            if status not in STATES or not isinstance(comment, str) or type(revision) is not int or revision < 0:
                raise ValueError('Повреждено действие')
            due = sent_time(due) if due else control_date(sent_time(row['date_sent']))
            if aid in blocked:
                status = 'do_not_contact'
            group = (3 if status in {'completed', 'do_not_contact'} else
                     1 if due <= now else 2 if status in {'deferred', 'no_answer'} else 0)
            task = mailing.get(eid, {})
            priority, recommendation = priorities.get(eid, call_priority({}))
            rows.append({**row, 'task_state': task_state({**task, 'id': eid}),
                         'account_task_blocked': aid in task_accounts,
                         'call_selected': bool(task.get('call_selected_at')),
                         'priority': priority, 'recommendation': recommendation, 'status': status, 'due': due, 'comment': comment,
                         'revision': revision, 'group': group})
        if any(not r.get('document_id') for r in rows):
            warnings.append(PDF_WARNING)
        return sorted(rows, key=lambda r: (GROUP_ORDER.index(r['group']), r['priority'], sent_time(r['date_sent']), r['email_id'])), warnings

    def summary_html(self, now=None):
        values = ['—'] * 4
        try:
            if not self.path.is_file() or self.unavailable:
                raise ValueError('Очередь недоступна')
            rows, warnings = self.queue(now)
            if any(w != PDF_WARNING for w in warnings):
                raise ValueError('Очередь неполна')
            pending = [r for r in rows if r['status'] not in {'completed', 'do_not_contact'}]
            values = [len(pending), sum(call_available(r) and r['priority'] < 2 for r in pending),
                      sum(r['call_selected'] for r in rows),
                      sum(r['task_state'] == 'CONFIRMED' for r in rows)]
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            pass
        content = '<a href="/followup"><article><h2>Контроль КП</h2><div class="grid">'
        for label, value in zip(('Всего КП ожидают контроля', 'Рекомендовано к звонку',
                                 'Выбрано для звонка', 'Задачи CRM подтверждены'), values):
            content += '<div><span>' + label + '</span><br><strong class="metric">' + str(value) + '</strong></div>'
        return content + '</div><p>По сохранённой сверке. Выбрано — новые решения оператора о создании задач; подтверждённые задачи включают прежние. «—» — данные неполны.</p></article></a>'

    def update(self, eid, status, due, comment, revision, now=None):
        now = now or datetime.now(UTC)
        if (not isinstance(eid, str) or not ID.fullmatch(eid) or status not in STATES
                or not isinstance(comment, str) or len(comment) > 2000
                or any(ord(c) < 32 and c not in '\n\t' for c in comment)
                or not isinstance(revision, str) or not re.fullmatch(r'\d{1,9}', revision)):
            raise ValueError('Некорректное действие или комментарий (до 2000 символов).')
        next_due = None
        if status in {'deferred', 'no_answer'}:
            if not due and status == 'no_answer':
                next_due = control_date(now)
            else:
                if not re.fullmatch(r'\d{4}-\d\d-\d\d', due):
                    raise ValueError('Выберите дату повторного контроля.')
                next_due = datetime.fromisoformat(due).replace(hour=10, tzinfo=MOSCOW)
            if not now < next_due <= now + timedelta(days=730):
                raise ValueError('Дата контроля должна быть в будущем, не далее двух лет.')
        bindings, errors = control_bindings(self.path.parent, self.journals)
        if eid not in bindings:
            raise ValueError('Привязка к LexRadar недоступна; действие не сохранено.')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            saved = db.execute('SELECT record FROM snapshot WHERE email_id=?', (eid,)).fetchone()
            if not saved:
                raise ValueError('Отправка не подтверждена; обновите страницу.')
            row = json.loads(saved[0])
            aid = row['account_id']
            if bindings[eid]['account_id'] != aid:
                raise ValueError('Привязка организации изменилась.')
            previous = db.execute('SELECT revision, account_id FROM actions WHERE email_id=?', (eid,)).fetchone()
            if previous and previous[1] != aid:
                raise ValueError('Привязка прежнего действия изменилась.')
            if int(revision) != (previous[0] if previous else 0):
                raise ValueError('Действие уже сохранено или страница устарела; обновите очередь.')
            if db.execute('SELECT 1 FROM blocked WHERE account_id=?', (aid,)).fetchone():
                raise ValueError('Для организации сохранён отказ от контактов.')
            next_due = next_due.isoformat() if next_due else None
            db.execute('INSERT OR REPLACE INTO actions VALUES (?,?,?,?,?,?)',
                       (eid, aid, status, next_due, comment.strip(), int(revision) + 1))
            db.execute('INSERT INTO events(email_id,account_id,status,due,comment,at) VALUES (?,?,?,?,?,?)',
                       (eid, aid, status, next_due, comment.strip(), now.isoformat()))
            if status == 'do_not_contact':
                db.execute('INSERT INTO blocked VALUES (?,?)', (aid, now.isoformat()))

    def history(self, row):
        with closing(self.connect()) as db:
            return db.execute(
                'SELECT status,due,comment,at FROM events WHERE email_id=? AND account_id=? ORDER BY id DESC',
                (row['email_id'], row['account_id'])).fetchall()

    def html(self, handler):
        try:
            rows, warnings = self.queue()
            try:
                index = handler.index()
                warnings += local_phones(rows, index['leads'], self.journals)
                if index.get('warnings'):
                    warnings.append('Локальный индекс организаций/CSV: ошибок или ограничений — ' + str(index['warnings']) + '; полнота телефонов не подтверждена.')
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
                warnings.append('Локальные телефоны недоступны; показаны только номера снимка CRM.')
                for row in rows:
                    if not safe_phone(row.get('phone')):
                        row['phone_reason'] = 'Номер Account не получен; локальные организации/CSV недоступны.'
            params = parse_qs(urlsplit(getattr(handler, 'path', '/followup')).query)
            selected = next((r for r in rows if r['email_id'] == params.get('email', [''])[0]), None)
            history = self.history(selected) if selected else []
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return '<h1>Контроль КП</h1><p role="alert">Локальное хранилище недоступно или повреждено. Действия отключены; данные не сброшены.</p>'
        content = FOLLOWUP_STYLE + '<section id="followup"><h1>Контроль КП</h1>'
        content += '<p>Сначала — кому пора проверить ответ и позвонить. Контроль через три рабочих дня после отправки; все даты — МСК.</p>'
        if rows:
            checked = [sent_time(r['checked_at']) for r in rows]
            content += '<p class="fu-note">Последняя успешная проверка записи CRM: ' + display_time(max(checked))
            if max(checked) - min(checked) > timedelta(minutes=1):
                content += ' · Самая ранняя проверка в списке: ' + display_time(min(checked))
            content += '. Проверка кешируется на 60 секунд.</p>'
        else:
            content += '<p class="fu-note">Время успешной проверки записей CRM недоступно: снимок пуст.</p>'
        for warning in dict.fromkeys(self.warnings + warnings):
            content += '<p role="alert" class="fu-warning">' + esc(warning) + '</p>'
        if self.unavailable:
            content += '<p role="status" class="fu-warning">Данные неполные. Показан последний подтверждённый снимок; часть записей может устареть. Сверьте CRM перед контактом.</p>'
        content += '<details class="fu-rules"><summary>Правила контроля и источник данных</summary><p>Срок — три рабочих дня по Москве (пн–пт; праздничные дни отдельно не учитываются). Отправка не доказывает доставку. Ответы/ошибки доставки автоматически не проверяются. Перед контактом вручную сверьте переписку, отказы и основание контакта. Наличие задачи не разрешает рекламный звонок. Решение о контакте принимает оператор; автоматических звонков и повторных писем нет. Результаты контакта сохраняются локально. Task в CRM создаются только для явно выбранных организаций по кнопке; отправленные письма не изменяются.</p><p>Источник: полные карточки CRM Email (Sent и dateSent), точные ID из штатных журналов экспорта и очереди LexRadar. Письма без привязки не включены. Телефон — связанный Account либо локальный Parser по точной сохранённой привязке экспорта; «—» означает, что однозначный достоверный номер не получен.</p></details>'
        content += '<div class="fu-counts" aria-label="Категории контроля">'
        for index in GROUP_ORDER:
            content += '<div' + (' class="fu-priority"' if index == 1 else '') + '><span>' + GROUPS[index] + '</span><strong>' + str(sum(r['group'] == index for r in rows)) + '</strong></div>'
        content += '<div><span>Доступны для звонка</span><strong>' + str(len({r['account_id'] for r in rows if call_available(r)})) + '</strong></div>'
        content += '</div><p class="fu-note">«Пора проверить» — все КП с наступившим сроком. «Доступны для звонка» — уникальные организации, доступные для ручного выбора по сохранённой сверке.</p>'
        if selected:
            content += self.detail(handler, selected, history, params)
        elif params.get('email'):
            content += '<p role="alert">Выбранная отправка отсутствует в подтверждённой очереди. Действие для неё недоступно.</p>'
        if not rows:
            return content + '<p>Подтверждённых отправок в доступной выборке нет.</p></section>'
        content += '''<div class="fu-filters" hidden>
<label>Категория <select id="fu-group"><option value="">Все категории</option>'''
        for index in GROUP_ORDER:
            content += '<option value="' + str(index) + '"' + (' selected' if index == 1 else '') + '>' + GROUPS[index] + '</option>'
        content += '''</select></label><label class="fu-search">Поиск по названию, домену или телефону
<input type="search" id="fu-search" placeholder="Название, сайт или номер" autocomplete="off"></label>
<button type="button" id="fu-reset">Сбросить</button></div>
<p id="fu-count" role="status">Всего записей: ''' + str(len(rows)) + '</p>'
        content += '<noscript><p>Для быстрого поиска и фильтра включите JavaScript. Все записи и действия доступны ниже.</p></noscript>'
        fields = '<p>Выберите до 10 организаций. Подтверждённые задачи и неизвестные исходы POST пропускаются. Ничего не выбрано заранее.</p>'
        fields += '<p id="fu-selected" role="status">Выбрано сейчас: 0</p>'
        form = handler.form('/followup/tasks', 'Создать задачи в CRM для выбранных', fields, confirm=False)
        content += form.replace('<form ', '<form id="fu-task-selection" ', 1)
        content += '<div class="table-scroll" tabindex="0" role="region" aria-label="Очередь контроля: прокрутка таблицы"><table class="fu-table"><caption class="fu-note">Сначала готовые к контролю; выше существенные замечания, при равенстве — старые КП. Даты и время — МСК.</caption><thead><tr>'
        content += ''.join('<th scope="col">' + label + '</th>' for label in ('Организация', 'Телефон', 'КП отправлено', 'Срок контроля', 'Результат обработки', 'Действия', 'Приоритет и причина', 'Выбор')) + '</tr></thead><tbody>'
        for row in rows:
            eid = row['email_id']
            search = ' '.join(str(row.get(k) or '') for k in ('name', 'website', 'phone'))
            content += '<tr id="fu-row-' + esc(eid) + '" data-group="' + str(row['group']) + '" data-search="' + esc(search) + '"' + (' class="fu-overdue"' if row['group'] == 1 else '') + '>'
            content += '<th scope="row"><span class="fu-name">' + esc(row['name']) + '</span><div class="fu-links">' + record_links(row) + '</div></th>'
            content += '<td class="fu-phone">' + phone_html(row.get('phone'))
            if row.get('phone_source'):
                content += '<small class="fu-status">' + esc(row['phone_source']) + '</small>'
            content += '</td>'
            content += '<td class="fu-date">' + display_time(sent_time(row['date_sent'])) + '</td><td class="fu-date">' + display_time(row['due']) + '<small class="fu-status">' + deadline_label(row) + '</small></td>'
            content += '<td><span class="fu-badge">' + LABELS[row['status']] + '</span></td>'
            content += '<td><a class="cta fu-open" href="/followup?' + esc(urlencode({'email': eid})) + '#followup-detail" aria-label="' + esc('Открыть: ' + row['name']) + '">Открыть</a></td>'
            content += '<td>' + ('Высокий', 'Средний', 'Низкий')[row['priority']] + '<br>' + esc(row['recommendation']) + '</td><td>'
            if call_available(row):
                content += '<input form="fu-task-selection" type="checkbox" name="email_id" value="' + esc(eid) + '" aria-label="Выбрать ' + esc(row['name']) + '">'
            else:
                content += esc({'CONFIRMED': 'Задача подтверждена', 'RECONCILE_ONLY': 'Требуется сверка; повтор запрещён'}.get(row['task_state'], 'У Account уже есть задача или неизвестный исход' if row['account_task_blocked'] else 'Срок не наступил или контроль обработан'))
            content += '</td></tr>'
        content += '</tbody></table></div><p id="fu-empty" hidden>По этому фильтру ничего не найдено.</p>'
        nonce = getattr(handler, 'followup_nonce', '')
        if nonce:
            content += '<script nonce="' + esc(nonce) + '">' + FOLLOWUP_SCRIPT + '</script>'
        return content + '</section>'

    def detail(self, handler, row, history, params):
        content = '<article id="followup-detail" tabindex="-1" aria-labelledby="fu-detail-title"><h2 id="fu-detail-title">' + esc(row['name']) + '</h2>'
        if row['revision'] and params.get('saved') == [str(row['revision'])]:
            content += '<p role="status" class="fu-success">Результат сохранён. Версия записи: ' + str(row['revision']) + '.</p>'
        content += '<p>Результат: <strong>' + LABELS[row['status']] + '</strong> · Срок: ' + deadline_label(row) + '</p>'
        content += '<p>КП отправлено: ' + display_time(sent_time(row['date_sent'])) + ' · Контроль: ' + display_time(row['due']) + '</p>'
        content += '<p>Телефон: ' + phone_html(row.get('phone')) + '</p><p class="fu-links">' + record_links(row) + '</p>'
        if row.get('phone_source'):
            content += '<p class="fu-note">' + esc(row['phone_source']) + '</p>'
        if not safe_phone(row.get('phone')):
            content += '<p class="fu-note">' + esc(row.get('phone_reason') or 'В снимке Account нет пригодного номера; локальный источник не подтверждён.') + '</p>'
        content += '<p class="fu-note">Проверка этой записи CRM: ' + display_time(sent_time(row['checked_at'])) + '</p>'
        if not row.get('document_id'):
            content += '<p class="fu-note">Привязка отправленного КП не подтверждена; ссылка скрыта.</p>'
        content += '<p class="fu-comment">' + esc(row['comment']) + '</p>'
        if row['status'] != 'do_not_contact':
            fields = '<input type="hidden" name="email_id" value="' + esc(row['email_id']) + '"><input type="hidden" name="revision" value="' + str(row['revision']) + '">'
            fields += '<label>Результат <select name="status"><option value="completed">Обработано</option><option value="deferred">Отложить до даты</option><option value="no_answer">Не дозвонился</option><option value="do_not_contact">Не связываться</option></select></label>'
            fields += '<label>Повторный контроль (МСК, 10:00) <input type="date" name="due"></label><p class="fu-note">Для «Отложить до даты» дата обязательна. Для «Не дозвонился» без даты — через три рабочих дня; повтор назначается только этим действием.</p>'
            fields += '<label class="fu-comment-field">Комментарий <textarea name="comment" maxlength="2000" rows="3">' + esc(row['comment']) + '</textarea></label>'
            fields += '<p class="fu-warning">«Не связываться» исключает организацию из всех дальнейших задач контроля. Снять отказ этой формой нельзя. Очередь рассылки не изменяется.</p>'
            content += handler.form('/followup/action', 'Сохранить', fields)
        else:
            content += '<p class="fu-warning">Сохранён отказ от контактов для всей организации. Изменение результата заблокировано.</p>'
        content += '<p><a class="fu-return" href="/followup#fu-row-' + esc(row['email_id']) + '">Отмена / к списку</a></p>'
        content += '<details><summary>История действий (' + str(len(history)) + ')</summary>'
        if not history:
            content += '<p>Операторских действий ещё нет.</p>'
        for status, due, comment, at in history:
            content += '<div class="fu-event"><strong>' + esc(LABELS.get(status, status)) + '</strong> · ' + display_time(sent_time(at))
            if due:
                content += ' · Контроль: ' + display_time(sent_time(due))
            content += '<p class="fu-comment">' + esc(comment) + '</p></div>'
        return content + '</details></article>'


def display_time(value):
    return esc(value.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M')) + ' МСК'


def safe_phone(value):
    if not isinstance(value, str) or not re.fullmatch(r'\+?[0-9() .-]{5,100}', value.strip()):
        return None
    number = re.sub(r'[() .-]', '', value.strip())
    return number if re.fullmatch(r'\+?[0-9]{5,15}', number) else None


def account_phone(account):
    value = account.get('phoneNumber')
    if safe_phone(value):
        return value.strip()
    numbers = account.get('phoneNumberData')
    if isinstance(numbers, list):
        numbers = [n for n in numbers if isinstance(n, dict) and not n.get('invalid') and not n.get('optOut')]
        for item in sorted(numbers, key=lambda n: not n.get('primary')):
            value = item.get('phoneNumber')
            if safe_phone(value):
                return value.strip()
    return ''


def phone_html(value):
    number = safe_phone(value)
    return link('tel:' + number, value) if number else '—'


def record_links(row):
    links = []
    website = safe_website(row.get('website'))
    if website:
        links.append(link(website, urlsplit(website).hostname))
    for kind, key, title in (('Account', 'account_id', 'CRM'), ('Email', 'email_id', 'Письмо'), ('Document', 'document_id', 'КП')):
        value = row.get(key)
        if isinstance(value, str) and ID.fullmatch(value):
            links.append(link(espo.API_URL.split('/api/')[0] + '/#' + kind + '/view/' + value, title))
    return ' · '.join(links)


def safe_website(value):
    try:
        if not value or any(c.isspace() or ord(c) < 32 for c in value):
            return None
        url = value if '://' in value else 'https://' + value
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            return None
        parsed.port
        return url
    except ValueError:
        return None


FOLLOWUP_STYLE = '''<style>
#followup .fu-note { font-size:13px; color:#6B7280; }
#followup .fu-rules { font-size:13px; margin:12px 0; }
#followup .fu-counts { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:20px 0; }
#followup .fu-counts > div { border:1px solid #E5E7EB; border-radius:10px; padding:12px 16px; background:white; }
#followup .fu-counts span { display:block; font-size:14px; }
#followup .fu-counts strong { display:block; font-size:26px; line-height:1.3; }
#followup .fu-counts .fu-priority { border-color:#BFDBFE; background:#EFF6FF; }
#followup .fu-filters { display:flex; flex-wrap:wrap; align-items:end; gap:12px; margin:20px 0 8px; }
#followup [hidden] { display:none !important; }
#followup .fu-search { flex:1; min-width:200px; }
#followup .fu-table { min-width:1200px; }
#followup .fu-table th:nth-child(7) { width:24%; }
#followup .fu-table th, #followup .fu-table td { padding:10px 12px; vertical-align:middle; }
#followup .fu-table th:nth-child(1) { width:30%; }
#followup .fu-table th:nth-child(2) { width:15%; }
#followup .fu-table th:nth-child(3), #followup .fu-table th:nth-child(4) { width:15%; }
#followup .fu-table th:nth-child(5) { width:15%; }
#followup .fu-table th:nth-child(6) { width:110px; }
#followup .fu-table caption { text-align:left; padding:8px 12px; }
#followup .fu-table tbody th { background:transparent; color:#111827; font-weight:500; }
#followup .fu-name { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
#followup .fu-links { font-size:13px; font-weight:400; }
#followup .fu-overdue { background:#F0F6FF; }
#followup .fu-overdue > th { border-left:3px solid #93B4E8; }
#followup .fu-status { display:block; margin-top:4px; }
#followup .fu-badge { font-size:12px; font-weight:600; color:#374151; }
#followup .fu-date { font-size:13px; font-variant-numeric:tabular-nums; }
#followup .fu-phone { font-variant-numeric:tabular-nums; }
#followup .fu-warning { background:#FFFBEB; border-left:3px solid #D6B66A; padding:10px 12px; }
#followup .fu-success { background:#EFF6FF; padding:10px 12px; color:#1D4ED8; }
#followup #followup-detail { scroll-margin-top:24px; margin:20px 0; border-color:#BFDBFE; }
#followup #followup-detail form { align-items:start; }
#followup #followup-detail form > p, #followup .fu-comment-field { flex-basis:100%; }
#followup textarea { font:inherit; width:100%; max-width:100%; border:1px solid #D1D5DB; border-radius:8px; padding:10px; }
#followup .fu-comment { white-space:pre-wrap; overflow-wrap:anywhere; }
#followup .fu-event { padding:8px 0; border-bottom:1px solid #E5E7EB; }
#followup tr:target { outline:2px solid #2563EB; outline-offset:-2px; }
@media(max-width:700px) { #followup .fu-counts { grid-template-columns:repeat(2,minmax(0,1fr)); } }
</style>'''

FOLLOWUP_SCRIPT = '''(() => {
const root = document.getElementById('followup');
const choices = Array.from(root.querySelectorAll('input[form="fu-task-selection"]'));
const selectionCount = () => { root.querySelector('#fu-selected').textContent = 'Выбрано сейчас: ' + choices.filter(c => c.checked).length; };
choices.forEach(c => c.addEventListener('change', selectionCount));
if (root.querySelector('#fu-selected')) selectionCount();
const group = root.querySelector('#fu-group'), search = root.querySelector('#fu-search');
const rows = Array.from(root.querySelectorAll('tbody tr'));
const params = new URLSearchParams(location.search);
if (Array.from(group.options).some(o => o.value === params.get('group'))) group.value = params.get('group');
search.value = params.get('q') || '';
const digits = value => value.replace(/[^0-9]/g, '');
function filter() {
  const query = search.value.trim().toLocaleLowerCase('ru');
  const phoneQuery = /^[+0-9() .-]+$/.test(query) ? digits(query) : '';
  let count = 0;
  rows.forEach(row => {
    const text = row.dataset.search.toLocaleLowerCase('ru');
    const phone = row.querySelector('.fu-phone').textContent;
    const match = text.includes(query) || (phoneQuery && digits(phone).includes(phoneQuery));
    row.hidden = !((!group.value || row.dataset.group === group.value) && match);
    if (!row.hidden) count++;
  });
  root.querySelector('#fu-count').textContent = 'Показано: ' + count + ' из ' + rows.length;
  root.querySelector('#fu-empty').hidden = count !== 0;
  root.querySelectorAll('.fu-open, .fu-return').forEach(a => {
    const url = new URL(a.href);
    if (search.value) url.searchParams.set('q', search.value); else url.searchParams.delete('q');
    if (group.value) url.searchParams.set('group', group.value); else url.searchParams.delete('group');
    a.href = url.pathname + url.search + url.hash;
  });
}
group.addEventListener('change', filter);
search.addEventListener('input', filter);
root.querySelector('#fu-reset').addEventListener('click', () => { group.value = ''; search.value = ''; filter(); search.focus(); });
root.querySelector('.fu-filters').hidden = false;
const form = root.querySelector('form[action="/followup/action"]');
if (form) {
  const status = form.elements.status, due = form.elements.due;
  const required = () => { due.required = status.value === 'deferred'; };
  status.addEventListener('change', required);
  required();
}
filter();
})();'''
