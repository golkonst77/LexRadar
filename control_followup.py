"""Read-only CRM observations and independent, local operator follow-up actions."""
import fcntl
import json
import os
import re
import sqlite3
import threading
import time
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
            warnings.append('Часть истории экспорта недоступна или повреждена; её письма не включены.')
    for eid in conflicts:
        bindings.pop(eid, None)
    if conflicts:
        warnings.append('Письма с противоречивой привязкой исключены.')
    return bindings, warnings


def read_sent(api, bindings):
    """Bounded GET-only pagination; no mailing imports or write-capable workflows."""
    deadline, calls = time.monotonic() + 8, 0
    rows, warnings, seen, excluded = [], [], set(), set()

    def get(route):
        nonlocal calls
        if calls >= 200 or time.monotonic() >= deadline:
            raise ValueError('limit')
        calls += 1
        return api.request('GET', route)

    try:
        candidates = []
        # shortcut: first 1000 Sent only; add a cursor if campaign history exceeds this window.
        for offset in range(0, 1000, 200):
            query = urlencode({'maxSize': 200, 'offset': offset, 'orderBy': 'id', 'order': 'asc',
                               'select': 'id,status,dateSent,parentType,parentId',
                               'where[0][type]': 'equals', 'where[0][attribute]': 'status',
                               'where[0][value]': 'Sent'})
            data = get('Email?' + query)
            page = data.get('list')
            if not isinstance(page, list) or len(page) > 200:
                raise ValueError('page')
            for row in page:
                eid = row.get('id') if isinstance(row, dict) else None
                if not isinstance(eid, str) or not ID.fullmatch(eid) or eid in seen:
                    raise ValueError('page')
                seen.add(eid)
                if eid in bindings:
                    candidates.append(eid)
            if len(page) < 200:
                if isinstance(data.get('total'), int) and data['total'] > len(seen):
                    warnings.append('CRM вернула неполный список отправок.')
                break
        else:
            warnings.append('Показана ограниченная выборка: просмотрены первые 1000 писем Sent.')
        accounts = {}
        for eid in candidates:
            try:
                email = get('Email/' + eid)
                binding = bindings[eid]
                aid = binding['account_id']
                if (email.get('id') != eid or email.get('deleted') or email.get('status') != 'Sent'
                        or email.get('parentType') != 'Account' or email.get('parentId') != aid):
                    excluded.add(eid)
                    warnings.append('Письмо с изменённой привязкой или статусом исключено.')
                    continue
                try:
                    sent = sent_time(email.get('dateSent'))
                    if sent > datetime.now(UTC):
                        raise ValueError('future')
                except ValueError:
                    excluded.add(eid)
                    raise
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
                        warnings.append('Связь части КП не подтверждена; ссылки на них скрыты.')
                rows.append(dict(email_id=eid, account_id=aid, document_id=doc,
                                 date_sent=sent.isoformat(), source_id=binding.get('source_id'), name=str(account.get('name') or 'Без названия')[:500],
                                 phone=account_phone(account),
                                 website=str(account.get('website') or '')[:2000],
                                 checked_at=datetime.now(UTC).isoformat()))
            except (espo.Hold, OSError, ValueError, TypeError, AttributeError):
                warnings.append('Часть отправок или карточек не проверена: ошибка CRM, дата или лимит чтения.')
                if time.monotonic() >= deadline or calls >= 200:
                    break
    except (espo.Hold, OSError, ValueError, TypeError, AttributeError):
        warnings.append('CRM недоступна или список неполон; полнота очереди не подтверждена.')
    return rows, list(dict.fromkeys(warnings)), excluded


class Followup:
    def __init__(self, directory, journals=JOURNALS):
        self.path = Path(directory) / 'followup.sqlite3'
        self.journals = journals
        self.lock = threading.Lock()
        self.refreshed = 0
        self.warnings = []
        self.unavailable = False

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

    def refresh(self, api_factory=None):
        with self.lock:
            if time.monotonic() - self.refreshed < 60:
                return
            bindings, warnings = campaign_bindings(self.journals)
            rows, excluded = [], set()
            if bindings:
                try:
                    rows, errors, excluded = read_sent((api_factory or espo.API)(), bindings)
                    warnings += errors
                except (espo.Hold, OSError, ValueError):
                    warnings.append('CRM недоступна; новые отправки не проверены.')
            else:
                warnings.append('Нет достоверных привязок Email к кампании LexRadar; посторонние письма исключены.')
            with closing(self.connect()) as db, db:
                if not warnings:
                    db.execute('DELETE FROM snapshot')
                db.executemany('DELETE FROM snapshot WHERE email_id=?', [(eid,) for eid in excluded])
                db.executemany('INSERT OR REPLACE INTO snapshot VALUES (?, ?)',
                               [(r['email_id'], json.dumps(r, ensure_ascii=False)) for r in rows])
            self.unavailable = bool(warnings)
            self.warnings = list(dict.fromkeys(warnings))
            self.refreshed = time.monotonic()

    def queue(self, now=None):
        now = now or datetime.now(UTC)
        bindings, warnings = campaign_bindings(self.journals)
        with closing(self.connect()) as db:
            actions = {r[0]: r for r in db.execute('SELECT * FROM actions')}
            blocked = {r[0] for r in db.execute('SELECT account_id FROM blocked')}
            records = [json.loads(r[0]) for r in db.execute('SELECT record FROM snapshot')]
        rows = []
        for row in records:
            eid, aid = row['email_id'], row['account_id']
            if bindings.get(eid, {}).get('account_id') != aid:
                continue
            if (bindings[eid].get('document_id') != row.get('document_id')
                    or bindings[eid].get('source_id') != row.get('source_id')):
                row['document_id'] = None
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
            rows.append({**row, 'status': status, 'due': due, 'comment': comment,
                         'revision': revision, 'group': group})
        return sorted(rows, key=lambda r: (GROUP_ORDER.index(r['group']), r['due'], r['email_id'])), warnings

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
        bindings, errors = campaign_bindings(self.journals)
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
            self.refresh()
            rows, warnings = self.queue()
            params = parse_qs(urlsplit(getattr(handler, 'path', '/followup')).query)
            selected = next((r for r in rows if r['email_id'] == params.get('email', [''])[0]), None)
            history = self.history(selected) if selected else []
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return '<h1>Контроль после отправки</h1><p role="alert">Локальное хранилище недоступно или повреждено. Действия отключены; данные не сброшены.</p>'
        content = FOLLOWUP_STYLE + '<section id="followup"><h1>Контроль после отправки</h1>'
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
        if self.unavailable and rows:
            content += '<p role="status" class="fu-warning">Данные неполные. Показан последний подтверждённый снимок; часть записей может устареть. Сверьте CRM перед контактом.</p>'
        content += '<details class="fu-rules"><summary>Правила контроля и источник данных</summary><p>Срок — три рабочих дня по Москве (пн–пт; праздничные дни отдельно не учитываются). Отправка не доказывает доставку. Ответы/ошибки доставки автоматически не проверяются. Перед контактом вручную сверьте переписку, отказы и основание контакта. Наличие задачи не разрешает рекламный звонок. Решение о контакте принимает оператор; автоматических звонков и повторных писем нет. Результаты сохраняются локально: CRM и очередь рассылки не изменяются.</p><p>Источник: полные карточки CRM Email (Sent и dateSent), только точные ID из штатных журналов экспорта LexRadar. Письма без привязки не включены. Телефон — связанный Account; «—» означает, что достоверный номер не получен.</p></details>'
        content += '<div class="fu-counts" aria-label="Категории контроля">'
        for index in GROUP_ORDER:
            content += '<div' + (' class="fu-priority"' if index == 1 else '') + '><span>' + GROUPS[index] + '</span><strong>' + str(sum(r['group'] == index for r in rows)) + '</strong></div>'
        content += '</div>'
        if selected:
            content += self.detail(handler, selected, history, params)
        elif params.get('email'):
            content += '<p role="alert">Выбранная отправка отсутствует в подтверждённой очереди. Действие для неё недоступно.</p>'
        if not rows:
            return content + '<p>Подтверждённых отправок в доступной выборке нет.</p></section>'
        content += '''<div class="fu-filters" hidden>
<label>Категория <select id="fu-group"><option value="">Все категории</option>'''
        for index in GROUP_ORDER:
            content += '<option value="' + str(index) + '">' + GROUPS[index] + '</option>'
        content += '''</select></label><label class="fu-search">Поиск по названию, домену или телефону
<input type="search" id="fu-search" placeholder="Название, сайт или номер" autocomplete="off"></label>
<button type="button" id="fu-reset">Сбросить</button></div>
<p id="fu-count" role="status">Всего записей: ''' + str(len(rows)) + '</p>'
        content += '<noscript><p>Для быстрого поиска и фильтра включите JavaScript. Все записи и действия доступны ниже.</p></noscript>'
        content += '<div class="table-scroll" tabindex="0" role="region" aria-label="Очередь контроля: прокрутка таблицы"><table class="fu-table"><caption class="fu-note">Сначала просроченные, затем ближайшие сроки. Даты и время — МСК.</caption><thead><tr>'
        content += ''.join('<th scope="col">' + label + '</th>' for label in ('Организация', 'Телефон', 'КП отправлено', 'Контроль', 'Статус', 'Действия')) + '</tr></thead><tbody>'
        for row in rows:
            eid = row['email_id']
            search = ' '.join(str(row.get(k) or '') for k in ('name', 'website', 'phone'))
            content += '<tr id="fu-row-' + esc(eid) + '" data-group="' + str(row['group']) + '" data-search="' + esc(search) + '"' + (' class="fu-overdue"' if row['group'] == 1 else '') + '>'
            content += '<th scope="row"><span class="fu-name">' + esc(row['name']) + '</span><div class="fu-links">' + record_links(row) + '</div></th>'
            content += '<td class="fu-phone">' + phone_html(row.get('phone')) + '</td>'
            content += '<td class="fu-date">' + display_time(sent_time(row['date_sent'])) + '</td><td class="fu-date">' + display_time(row['due']) + '</td>'
            content += '<td><span class="fu-badge">' + GROUPS[row['group']] + '</span><small class="fu-status">' + LABELS[row['status']] + '</small></td>'
            content += '<td><a class="cta fu-open" href="/followup?' + esc(urlencode({'email': eid})) + '#followup-detail" aria-label="' + esc('Открыть: ' + row['name']) + '">Открыть</a></td></tr>'
        content += '</tbody></table></div><p id="fu-empty" hidden>По этому фильтру ничего не найдено.</p>'
        nonce = getattr(handler, 'followup_nonce', '')
        if nonce:
            content += '<script nonce="' + esc(nonce) + '">' + FOLLOWUP_SCRIPT + '</script>'
        return content + '</section>'

    def detail(self, handler, row, history, params):
        content = '<article id="followup-detail" tabindex="-1" aria-labelledby="fu-detail-title"><h2 id="fu-detail-title">' + esc(row['name']) + '</h2>'
        if row['revision'] and params.get('saved') == [str(row['revision'])]:
            content += '<p role="status" class="fu-success">Результат сохранён. Версия записи: ' + str(row['revision']) + '.</p>'
        content += '<p><strong>' + LABELS[row['status']] + '</strong> · ' + GROUPS[row['group']] + '</p>'
        content += '<p>КП отправлено: ' + display_time(sent_time(row['date_sent'])) + ' · Контроль: ' + display_time(row['due']) + '</p>'
        content += '<p>Телефон: ' + phone_html(row.get('phone')) + '</p><p class="fu-links">' + record_links(row) + '</p>'
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
#followup .fu-table { min-width:920px; table-layout:fixed; }
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
