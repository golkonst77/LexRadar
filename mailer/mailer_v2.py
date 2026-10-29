#!/usr/bin/env python3
"""Explicitly approved EspoCRM scheduling; stdlib only. Default is GET-only."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import csv
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
MOSCOW = ZoneInfo('Europe/Moscow')
ID = re.compile(r'[A-Za-z0-9_-]+\Z')
ADDRESS = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+\Z")


def utc(value):
    return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=UTC)


def stamp(value):
    return value.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def config_at(root):
    c = read_json(root / 'config.json')
    expected = dict(timezone='Europe/Moscow', working_days=[0, 1, 2, 3, 4],
                    start_hour=9, end_hour=18, emails_per_hour=5, interval_minutes=12,
                    sender='sender@example.invalid', espo_api='https://crm.example.invalid/api/v1/')
    if any(c.get(k) != v for k, v in expected.items()):
        raise ValueError('Неподдерживаемый график/отправитель/API; проверьте config.json')
    if type(c.get('enabled')) is not bool or type(c.get('dry_run')) is not bool:
        raise ValueError('enabled и dry_run должны быть boolean')
    return c


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Перенаправление API запрещено')


class API:
    def __init__(self, config):
        self.base = config['espo_api']
        self.key = os.environ.get('ESPO_API_KEY')
        if not self.key:
            raise ValueError('Загрузите ESPO_API_KEY из ~/.config/lexradar/espo.env')
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method, path, payload=None):
        request = urllib.request.Request(self.base + path, method=method,
            headers={'X-Api-Key': self.key, 'Content-Type': 'application/json'},
            data=None if payload is None else json.dumps(payload).encode())
        try:
            with self.opener.open(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'API HTTP {exc.code}') from None
        except (OSError, ValueError):
            raise RuntimeError('API: сеть недоступна или некорректный ответ') from None

    def get(self, kind, eid):
        if not isinstance(eid, str) or not ID.fullmatch(eid):
            raise ValueError('Некорректный ID')
        value = self.request('GET', kind + '/' + eid)
        if not isinstance(value, dict) or value.get('id') != eid:
            raise ValueError('API вернул другой ID или неверную структуру')
        return value

    def scheduled(self):
        rows, offset = [], 0
        while True:
            query = urllib.parse.urlencode({'maxSize': 200, 'offset': offset,
                'orderBy': 'id', 'order': 'asc',
                'where[0][type]': 'isNotNull', 'where[0][attribute]': 'sendAt'})
            data = self.request('GET', 'Email?' + query)
            page = data.get('list') if isinstance(data, dict) else None
            if not isinstance(page, list) or any(not isinstance(x, dict) or not x.get('id') or not x.get('sendAt') for x in page):
                raise ValueError('Не удалось прочитать полный список назначенных слотов')
            rows.extend(page)
            offset += len(page)
            total = data.get('total')
            if len(page) < 200:
                if isinstance(total, int) and total >= 0 and offset < total:
                    raise ValueError('Неполная пагинация CRM')
                break
            if offset > 100000 or len({r['id'] for r in rows}) != len(rows):
                raise ValueError('Некорректная пагинация CRM')
        return rows

    def put(self, eid, send_at):
        self.request('PUT', 'Email/' + eid, {'sendAt': send_at})


def batch_at(path):
    if path.suffix.lower() == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
    else:
        rows = read_json(path)
    if not isinstance(rows, list) or not rows:
        raise ValueError('Партия должна быть непустым списком')
    for row in rows:
        if not isinstance(row, dict) or not ID.fullmatch(str(row.get('id', ''))):
            raise ValueError('В партии отсутствует корректный id')
    if len({r['id'] for r in rows}) != len(rows):
        raise ValueError('Повторяющиеся ID в партии')
    return rows


def address(item):
    value = item.get('to')
    return value.strip().lower() if isinstance(value, str) else ''


def validate(item, row, config):
    if item.get('status') != 'Draft' or item.get('sendAt'):
        return 'SKIP', 'не Draft или уже назначено'
    email = address(item)
    if not ADDRESS.fullmatch(email) or len(email) > 254 or len(email.split('@')[0]) > 64:
        return 'HOLD', 'нужен корректный одиночный email'
    if item.get('cc') or item.get('bcc'):
        return 'HOLD', 'дополнительные получатели cc/bcc'
    if (item.get('from') or '').strip().lower() != config['sender']:
        return 'HOLD', 'неверный отправитель'
    account = item.get('accountId') or (item.get('parentId') if item.get('parentType') == 'Account' else None)
    if not account:
        return 'HOLD', 'нет связанного Account'
    names = item.get('attachmentsNames') or {}
    ids = item.get('attachmentsIds') or []
    pdfs = [name for aid, name in names.items() if aid in ids and isinstance(name, str) and name.lower().endswith('.pdf')]
    expected = row.get('expected_pdf')
    if not pdfs or (expected and expected not in pdfs):
        return 'HOLD', 'нет требуемого PDF'
    if not expected and len(pdfs) != 1:
        return 'REVIEW', 'укажите expected_pdf для нескольких PDF'
    return 'READY', 'технически готово; согласие проверяется отдельно'


def next_slot(after, used):
    local = after.astimezone(MOSCOW)
    for day in range(366):
        date = local.date() + timedelta(days=day)
        if date.weekday() >= 5:
            continue
        for minute in range(9 * 60, 18 * 60 - 20 + 1, 12):
            slot = datetime(date.year, date.month, date.day, minute // 60, minute % 60, tzinfo=MOSCOW)
            if slot <= after:
                continue
            hour = (slot.date(), slot.hour)
            if sum((v.astimezone(MOSCOW).date(), v.astimezone(MOSCOW).hour) == hour for v in used) >= 5:
                continue
            if any(abs((slot - v).total_seconds()) < 12 * 60 for v in used):
                continue
            used.append(slot)
            return slot
    raise ValueError('Нет свободных слотов в течение года')


def events(root):
    path = root / 'journal.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def log(root, eid, action, slot, result):
    with (root / 'journal.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(dict(date=stamp(datetime.now(UTC)), id=eid,
            action=action, slot=slot, result=result), ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def owned(root):
    pending = {}
    for event in events(root):
        if event['action'] == 'schedule' and event['result'] in ('INTENT', 'SCHEDULED'):
            pending[event['id']] = event['slot']
        elif event['action'] == 'cancel' and event['result'] == 'CANCELLED':
            pending.pop(event['id'], None)
    return pending


def snapshot(api, rows, config, root, now):
    used = [utc(r['sendAt']) for r in api.scheduled()]
    items = {r['id']: api.get('Email', r['id']) for r in rows}
    counts = Counter(address(i) for i in items.values() if i.get('status') in ('Draft', 'Sent'))
    pending = owned(root)
    plan = []
    for row in rows:
        item = items[row['id']]
        state, reason = validate(item, row, config)
        if state == 'READY' and counts[address(item)] > 1:
            state, reason = 'REVIEW', 'дубликат email в очереди'
        if state == 'READY' and row['id'] in pending:
            state, reason = 'REVIEW', 'есть прежнее намерение PUT; требуется сверка журнала и CRM'
        slot = None
        if state == 'READY':
            account = item.get('accountId') or item.get('parentId')
            if api.get('Account', account).get('deleted'):
                state, reason = 'HOLD', 'Account удалён'
            else:
                slot = stamp(next_slot(now + timedelta(minutes=10), used))
        plan.append(dict(id=row['id'], state=state, reason=reason, slot=slot))
    return plan, items


def approvals(root):
    value = read_json(root / 'approved.json')
    if not isinstance(value, list) or any(not isinstance(i, str) or not ID.fullmatch(i) for i in value):
        raise ValueError('approved.json должен содержать список ID')
    return set(value)


def schedule(api, rows, root, now):
    c = config_at(root)
    if not c['enabled'] or c['dry_run']:
        raise ValueError('Требуются enabled=true и dry_run=false')
    plan, before = snapshot(api, rows, c, root, now)
    allowed = approvals(root)
    for p in plan:
        eid = p['id']
        if p['state'] != 'READY' or eid not in allowed:
            continue
        c = config_at(root)
        if not c['enabled'] or c['dry_run'] or eid not in approvals(root):
            raise ValueError('Планирование остановлено настройками/одобрением')
        # Rebuild immediately before every PUT: another local run cannot reuse a slot.
        current, items = snapshot(api, rows, c, root, datetime.now(UTC))
        fresh = next(v for v in current if v['id'] == eid)
        if fresh['state'] != 'READY' or items[eid] != before[eid]:
            raise ValueError('Черновик изменился после проверки; выполните preview заново')
        slot = fresh['slot']
        if api.get('Email', eid) != items[eid]:
            raise ValueError('Черновик изменился перед PUT')
        c = config_at(root)
        if not c['enabled'] or c['dry_run'] or eid not in approvals(root):
            raise ValueError('Планирование остановлено перед PUT')
        if utc(slot) <= datetime.now(UTC) + timedelta(minutes=10):
            raise ValueError('Проверка заняла слишком долго; пересчитайте план')
        log(root, eid, 'schedule', slot, 'INTENT')
        try:
            api.put(eid, slot)
            item = api.get('Email', eid)
            if item.get('sendAt') != slot or item.get('status') not in ('Draft', 'Sent'):
                raise ValueError('PUT не подтверждён чтением CRM')
        except Exception:
            log(root, eid, 'schedule', slot, 'REVIEW')
            raise
        log(root, eid, 'schedule', slot, 'SCHEDULED')
        p.update(state='SCHEDULED', slot=slot)
    return plan


def cancel(api, root, ids):
    own = owned(root)
    for eid in ids:
        if eid not in own:
            raise ValueError('Отмена разрешена только для собственных назначений из журнала')
        item = api.get('Email', eid)
        if item.get('status') != 'Draft' or item.get('sendAt') != own[eid]:
            raise ValueError('Статус или sendAt изменились; отмена запрещена')
        if utc(own[eid]) <= datetime.now(UTC) + timedelta(minutes=10):
            raise ValueError('Слот близок/прошёл: письмо могло быть взято планировщиком')
        log(root, eid, 'cancel', own[eid], 'INTENT')
        api.put(eid, None)
        check = api.get('Email', eid)
        if check.get('status') != 'Draft' or check.get('sendAt'):
            raise ValueError('Отмена не подтверждена; требуется ручная сверка')
        log(root, eid, 'cancel', own[eid], 'CANCELLED')
        print(eid, 'CANCELLED')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='preview', choices=['preview', 'status', 'schedule', 'pause', 'cancel'])
    parser.add_argument('--batch', type=Path, default=ROOT / 'batch.json')
    parser.add_argument('--confirm-schedule', action='store_true')
    parser.add_argument('--confirm-cancel', action='store_true')
    parser.add_argument('--id', action='append', default=[])
    args = parser.parse_args()
    rows = []
    try:
        if args.command == 'pause':
            # pause does not wait for the scheduling lock; each next PUT rechecks config.
            c = config_at(ROOT)
            c['enabled'] = False
            tmp = ROOT / 'config.paused.tmp'
            tmp.write_text(json.dumps(c, ensure_ascii=False, indent=2) + '\n')
            os.replace(tmp, ROOT / 'config.json')
            print('PAUSED: дальнейшее планирование отключено; назначенные письма не отменены')
            return
        with (ROOT / '.mailer.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            c = config_at(ROOT)
            if args.command == 'schedule' and not args.confirm_schedule:
                raise ValueError('Требуется отдельный флаг --confirm-schedule')
            if args.command == 'cancel' and (not args.confirm_cancel or not args.id):
                raise ValueError('Требуются --confirm-cancel и --id ID')
            api = API(c)
            if args.command == 'cancel':
                cancel(api, ROOT, args.id)
                return
            rows = batch_at(args.batch)
            if args.command == 'schedule':
                plan = schedule(api, rows, ROOT, datetime.now(UTC))
            else:
                plan, _ = snapshot(api, rows, c, ROOT, datetime.now(UTC))
            for p in plan:
                print(p['id'], p['state'], p['slot'] or '-', p['reason'])
                log(ROOT, p['id'], args.command, p['slot'], p['state'])
            print('ИТОГО:', dict(Counter(p['state'] for p in plan)))
            print(f"enabled={c['enabled']} dry_run={c['dry_run']}; READY не означает одобрение")
    except Exception as exc:
        # Never print arbitrary exception messages that could contain credentials/server bodies.
        message = str(exc) if type(exc) in (ValueError, RuntimeError) else type(exc).__name__
        print('REVIEW: выполнение остановлено:', message, file=sys.stderr)
        try:
            if args.command in ('preview', 'status') and rows:
                for row in rows:
                    print(row['id'], 'REVIEW: обязательные GET-проверки не завершены')
                    log(ROOT, row['id'], args.command, None, 'REVIEW')
                print('ИТОГО:', {'REVIEW': len(rows)})
            else:
                log(ROOT, '-', args.command, None, 'REVIEW')
        except OSError:
            print('REVIEW: журнал недоступен', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
