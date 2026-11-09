#!/usr/bin/env python3
"""Reschedule journal-bound Drafts. Default: local preview; --check-api: GET only."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from control_mailing import LEGACY, JOURNAL, MOSCOW, UTC, legacy_modules
from src.lexradar import espo_export as espo


def read(path):
    return json.loads(path.read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def slot_after(now, used):
    after = now + timedelta(minutes=10)
    local = after.astimezone(MOSCOW)
    for day in range(366):
        date = local.date() + timedelta(days=day)
        if date.weekday() == 6:
            continue
        for minute in range(540, 1080, 15):
            slot = datetime(date.year, date.month, date.day, minute // 60, minute % 60, tzinfo=MOSCOW).astimezone(UTC)
            if slot > after and all(abs((slot - busy).total_seconds()) >= 900 for busy in used):
                return slot.strftime('%Y-%m-%d %H:%M:%S')
    raise ValueError('Нет свободного слота')


def binding(eid, row, journal, protected):
    entry = journal.get(str(row.get('source_id')) + ':draft', {})
    parent = journal.get(row.get('source_id'), {})
    steps = entry.get('steps', {})
    action = steps.get('email_attachment', {})
    if (eid in protected or row.get('id') != eid or row.get('binding_hold')
            or row.get('recipient_verified') is not True
            or row.get('schedule_intent') not in (None, row.get('slot'))
            or entry.get('draft_error') or entry.get('draft_failure')
            or parent.get('draft_error') or parent.get('draft_failure')
            or any(s.get('pending') and not s.get('id') for s in steps.values() if isinstance(s, dict))
            or steps.get('email', {}).get('id') != eid
            or entry.get('account_id') != row.get('account_id')
            or entry.get('binding', {}).get('email') != row.get('email')
            or not row.get('pdf_sha256') or entry.get('sha256') != row['pdf_sha256']
            or action.get('document_id') != row.get('document_id')
            or action.get('sha256') != row['pdf_sha256'] or not action.get('id') or not action.get('sourceId')
            or (steps.get('document', {}).get('id') or action.get('document_id')) != row.get('document_id')):
        raise ValueError('HOLD: неподтверждённая привязка/операция')
    matches = [key for key, item in journal.items()
               if item.get('steps', {}).get('email', {}).get('id') == eid]
    if matches != [row['source_id'] + ':draft']:
        raise ValueError('HOLD: конфликт привязок Email')
    return entry


def verify(api, mailer, config, eid, row, entry, email, now):
    if not isinstance(email, dict) or email.get('id') != eid or not all(
            key in email for key in ('status', 'sendAt', 'to', 'from', 'parentType', 'parentId', 'attachmentsIds', 'body', 'name')):
        raise RuntimeError('STOP: неполный GET Email')
    if email['status'] != 'Draft' or email.get('dateSent') or not email.get('sendAt'):
        raise ValueError('SKIP: не будущий Draft')
    if mailer.utc(email['sendAt']) <= now or email['sendAt'] != row.get('slot'):
        raise ValueError('SKIP: прошлый/изменённый sendAt')
    ready, _ = mailer.validate({**email, 'sendAt': None}, {}, config)
    if (ready != 'READY' or email.get('parentType') != 'Account'
            or email.get('parentId') != row['account_id'] or email.get('to') != row['email']
            or email.get('accountId') not in (None, row['account_id'])):
        raise ValueError('HOLD: Email binding')
    action = entry['steps']['email_attachment']
    document = api.get('Document', row['document_id'])
    links = api.request('GET', 'Document/' + row['document_id'] + '/accounts')
    if (not isinstance(links.get('list'), list) or links.get('total', -1) != len(links['list'])
            or {x.get('id') for x in links['list']} != {row['account_id']}
            or document.get('fileId') != action['sourceId']
            or email['attachmentsIds'] != [action['id']]
            or email.get('attachmentsNames', {}).get(action['id']) != action.get('name')):
        raise ValueError('HOLD: Document/PDF binding')
    account = api.get('Account', row['account_id'])
    saved = entry['binding']
    if (account.get('deleted') or account.get('name') != saved.get('company_name')
            or espo.website_domain(account.get('website')) != espo.website_domain(saved.get('website'))):
        raise ValueError('HOLD: Account binding')
    for fid in (action['sourceId'], action['id']):
        if hashlib.sha256(api.file(fid)).hexdigest() != row['pdf_sha256']:
            raise ValueError('HOLD: PDF SHA-256')


def occupied(api, mailer):
    rows = mailer.API.scheduled(api)
    if len({r['id'] for r in rows}) != len(rows):
        raise RuntimeError('STOP: неполная пагинация')
    return [mailer.utc(r['sendAt']) for r in rows]


def execute(args, api=None):
    mailer, _ = legacy_modules()
    directory = args.directory.resolve()
    path = directory / 'mailing.json'
    operation_path = directory / 'mailing-reschedule.json'
    online = args.check_api or args.apply
    with ExitStack() as stack:
        for lockpath, mode in ((directory / 'mailing.lock', fcntl.LOCK_EX),
                               (LEGACY / '.mailer.lock', fcntl.LOCK_EX),
                               (args.journal.with_suffix('.lock'), fcntl.LOCK_SH)):
            lock = stack.enter_context(lockpath.open('r'))
            fcntl.flock(lock, mode | fcntl.LOCK_NB)
        state, journal = read(path), read(args.journal)
        operation = read(operation_path) if operation_path.exists() else {'id': str(uuid.uuid4()), 'items': {}}
        if any(item.get('status') != 'CONFIRMED' for item in operation['items'].values()):
            raise RuntimeError('STOP UNKNOWN: прежний intent; PUT запрещён, нужна ручная сверка')
        protected = {r['id'] for r in read(LEGACY / 'batch.json')}
        protected.update(read(LEGACY / 'deals-state.json'))
        archive = directory / 'mailing-archive.json'
        if archive.exists():
            protected.update(read(archive))
        now = datetime.now(UTC)
        candidates = sorted(((eid, row) for eid, row in state.items() if row.get('status') == 'SCHEDULED'),
                            key=lambda pair: (pair[1].get('slot') or '', pair[0]))
        config = mailer.config_at(LEGACY) if online else None
        api = (api or espo.API()) if online else None
        report = {'mode': 'apply' if args.apply else 'api-dry-run' if online else 'local-dry-run',
                  'at': now.isoformat(), 'queue_sha256': digest(state), 'rows': []}
        used = [mailer.utc(r[key]) for r in state.values() for key in ('slot', 'schedule_intent') if r.get(key)]
        legacy_events = LEGACY / 'journal.jsonl'
        if legacy_events.exists():
            used.extend(mailer.utc(slot) for slot in mailer.owned(LEGACY).values())
        used.extend(mailer.utc(item['new']) for item in operation['items'].values())
        plan = []
        for eid, row in candidates[:args.limit]:
            result = {'id': eid, 'old': row.get('slot'), 'status': 'UNVERIFIED'}
            report['rows'].append(result)
            if eid in operation['items']:
                result['status'] = 'ALREADY_CONFIRMED'
                continue
            # The first API operation for a candidate is always its full Email GET.
            email = api.get('Email', eid) if online else None
            if online:
                result.update(snapshot=email, snapshot_sha256=digest(email), checked_at=datetime.now(UTC).isoformat())
            try:
                entry = binding(eid, row, journal, protected)
                if online:
                    verify(api, mailer, config, eid, row, entry, email, datetime.now(UTC))
                elif not row.get('slot') or mailer.utc(row['slot']) <= now:
                    raise ValueError('SKIP: локальный слот прошёл')
            except ValueError as exc:
                result.update(status='SKIP', reason=str(exc))
                continue
            plan.append((eid, row, entry, email, result))
        if online:
            used.extend(occupied(api, mailer))
        for eid, row, entry, email, result in plan:
            slot = slot_after(now, used)
            used.append(mailer.utc(slot))
            result.update(new=slot, new_moscow=mailer.utc(slot).astimezone(MOSCOW).isoformat(),
                          old_moscow=mailer.utc(row['slot']).astimezone(MOSCOW).isoformat(),
                          status='VERIFIED' if online else 'UNVERIFIED')
        report['unexamined'] = max(0, len(candidates) - args.limit)
        espo.save(args.output, report)
        if not args.apply:
            print(json.dumps({'mode': report['mode'], 'candidates': len(candidates), 'planned': len(plan),
                              'unexamined': report['unexamined'], 'report': str(args.output)}))
            return
        if report['unexamined']:
            raise RuntimeError('STOP: apply требует полного прохода (--limit)')
        backup = directory / ('reschedule-' + operation['id'])
        backup.mkdir(mode=0o700, exist_ok=True)
        if not (backup / 'mailing.json').exists():
            espo.save(backup / 'mailing.json', state)
            espo.save(backup / 'crm-prepare.json', journal)
            espo.save(backup / 'sha256.json', {'queue': digest(state), 'journal': digest(journal)})
        for eid, row, entry, email, result in plan:
            slot = result['new']
            current = api.get('Email', eid)
            if current != email:
                raise RuntimeError('STOP: Email изменён; нужен новый dry-run')
            verify(api, mailer, config, eid, row, entry, current, datetime.now(UTC))
            if mailer.utc(slot) <= datetime.now(UTC) + timedelta(minutes=10):
                raise RuntimeError('STOP: новый слот слишком близко')
            if any(abs((mailer.utc(slot) - busy).total_seconds()) < 900 for busy in occupied(api, mailer)):
                raise RuntimeError('STOP: слот занят')
            intent = {'status': 'UNKNOWN', 'old_row': dict(row), 'new': slot, 'snapshot': current,
                      'snapshot_sha256': digest(current), 'binding': entry, 'at': datetime.now(UTC).isoformat()}
            operation['items'][eid] = intent
            try:
                espo.save(operation_path, operation)
                reply = api.request('PUT', 'Email/' + eid, {'sendAt': slot})
                if not isinstance(reply, dict) or reply.get('id') != eid or reply.get('sendAt') != slot:
                    raise RuntimeError('unexpected PUT response')
                check = api.get('Email', eid)
                ignored = {'sendAt', 'modifiedAt', 'modifiedById', 'modifiedByName'}
                if (check.get('status') != 'Draft' or check.get('sendAt') != slot
                        or {k: v for k, v in check.items() if k not in ignored}
                        != {k: v for k, v in current.items() if k not in ignored}):
                    raise RuntimeError('GET verification failed')
                row.update(slot=slot, schedule_intent=slot, checked_at=mailer.stamp(datetime.now(UTC)))
                espo.save(path, state)
                intent.update(status='CONFIRMED', verified_at=datetime.now(UTC).isoformat(), after=check)
                espo.save(operation_path, operation)
            except BaseException:
                raise RuntimeError('STOP UNKNOWN: intent сохранён; PUT не повторять') from None
        print('CONFIRMED:', len(plan))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path(__file__).resolve().parent / '.control-center-state')
    parser.add_argument('--journal', type=Path, default=JOURNAL)
    parser.add_argument('--output', type=Path, default=Path('/tmp/lexradar-reschedule-dry-run.json'))
    parser.add_argument('--limit', type=int, default=1, help='maximum candidates; apply requires full coverage')
    parser.add_argument('--check-api', action='store_true', help='GET-only live reconciliation')
    parser.add_argument('--apply', action='store_true', help='explicitly authorize sendAt PUTs')
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('--limit must be positive')
    try:
        execute(args)
    except Exception as exc:
        print(str(exc) if isinstance(exc, (ValueError, RuntimeError, BlockingIOError)) else 'STOP: API/IO failure', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
