"""Create call tasks for explicitly selected Sent emails; never run the scheduler."""
import argparse
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import sys
import sqlite3
import time
from datetime import datetime

from control_followup import (ID, campaign_bindings,
                              check_local_call_action, ensure_call_task, task_state, control_date, sent_time, UTC)
from src.lexradar import espo_export as espo

ROOT = Path(__file__).resolve().parent
LEGACY = Path('/home/golkonst/lexradar-mailer')


def wait_lock(lock):
    deadline = time.monotonic() + 10
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise ValueError('mailing.lock занята; повторите после штатного цикла') from None
            time.sleep(0.25)


def run(directory, legacy, eids=(), *, check_api=False, apply=False, api_factory=espo.API, batch_limit=None):
    directory, legacy = Path(directory), Path(legacy)
    if len(set(eids)) != len(eids) or len(eids) > 10 or any(not ID.fullmatch(eid) for eid in eids):
        raise ValueError('Укажите до 10 уникальных корректных --email')
    if batch_limit is not None:
        raise ValueError('Автоматический отбор SENT отключён; выберите организации явно через --email или Контроль КП')
    if (check_api or apply) and not eids:
        raise ValueError('Для API нужен явный --email; автоматического отбора всех SENT нет')
    path = directory / 'mailing.json'
    # Same order as the worker, no service stop and no lock bypass.
    with (directory / 'mailing.lock').open('r') as queue_lock:
        wait_lock(queue_lock)
        with (legacy / '.mailer.lock').open('r') as mailer_lock:
            fcntl.flock(mailer_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = json.loads(path.read_text())
            if not isinstance(state, dict) or any(not isinstance(r, dict) or r.get('id') != eid
                                                  for eid, r in state.items()):
                raise ValueError('Повреждён журнал очереди')
            archive_path = directory / 'mailing-archive.json'
            archive = json.loads(archive_path.read_text()) if archive_path.exists() else {}
            if not isinstance(archive, dict) or any(not isinstance(r, dict) or r.get('id') != eid
                                                    for eid, r in archive.items()):
                raise ValueError('Повреждён архив очереди')
            records = {**archive, **state}
            selected = list(eids) or sorted(eid for eid, r in records.items() if r.get('status') == 'SENT')
            api = None
            selected_accounts = set()
            results = []
            for eid in selected:
                error = None
                try:
                    if eid not in records:
                        bindings, warnings = campaign_bindings()
                        if not apply or warnings or eid not in bindings:
                            raise ValueError('Точная связь выбранной отправки не подтверждена журналом')
                        if api is None:
                            api = api_factory()
                        email = api.get('Email', eid)
                        aid = bindings[eid]['account_id']
                        if (email.get('id') != eid or email.get('status') != 'Sent' or email.get('deleted')
                                or email.get('sendAt') or email.get('parentType') != 'Account'
                                or email.get('parentId') != aid):
                            raise ValueError('CRM не подтвердила выбранный Sent/Account')
                        # Historical Sent stays outside the active mailing queue.
                        row = dict(id=eid, account_id=aid, status='SENT', dateSent=email.get('dateSent'))
                        archive[eid] = records[eid] = row
                    row = records[eid]
                    status = task_state(row)
                    if status != 'AVAILABLE':
                        result = dict(email_id=eid, status=status)
                        results.append(result)
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                        continue
                    save = (lambda: espo.save(path, state)) if eid in state else (lambda: espo.save(archive_path, archive))
                    if row.get('status') != 'SENT' or row.get('binding_hold'):
                        raise ValueError('Выбранная строка не является подтверждённой локальной SENT')
                    if not (check_api or apply):
                        status = 'NEEDS_API_CHECK'
                    else:
                        if api is None:
                            api = api_factory()
                        # GET-only checks must not change even the in-memory journal row.
                        target = row if apply else deepcopy(row)
                        if not target.get('account_id'):
                            bindings, warnings = campaign_bindings()
                            if warnings or eid not in bindings:
                                raise ValueError('Точная связь Email/Account в истории не подтверждена')
                            target['account_id'] = bindings[eid]['account_id']
                        if not any(k in target for k in ('task_id', 'task_intent', 'task_post_id')):
                            check_local_call_action(directory, eid, target['account_id'])
                        aid = target['account_id']
                        if apply:
                            if aid in selected_accounts or any(r is not row and r.get('account_id') == aid
                                    and task_state(r) != 'AVAILABLE' for r in records.values()):
                                raise ValueError('Для организации уже есть задача или неизвестный исход; требуется сверка')
                            row['call_selected_at'] = datetime.now(UTC).isoformat()
                            save()
                            selected_accounts.add(aid)
                        email = api.get('Email', eid)
                        if apply and control_date(sent_time(email.get('dateSent'))) > datetime.now(UTC):
                            raise ValueError('Три рабочих дня после отправки ещё не прошли')
                        status = ensure_call_task(api, target, email, save, apply=apply)
                except Exception as exc:
                    if not apply:
                        raise
                    # Intent stays in memory and on disk; never retry this row here.
                    status = 'ERROR'
                    error = str(exc) if isinstance(exc, (ValueError, espo.Hold)) else 'Ошибка CRM/журнала; проверьте состояние перед повтором'
                result = dict(email_id=eid, status=status)
                if error is not None:
                    result['error'] = error
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
            return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=ROOT / '.control-center-state')
    parser.add_argument('--legacy-dir', type=Path, default=LEGACY)
    parser.add_argument('--email', action='append', default=[], help='Exact saved Email ID; repeat up to 10 times')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check-api', action='store_true', help='GET only; no journal writes')
    mode.add_argument('--apply', action='store_true', help='Create Task only for selected Email IDs; unknown outcomes are skipped')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        results = run(args.state_dir, args.legacy_dir, args.email, check_api=args.check_api, apply=args.apply)
        return int(any(r['status'] == 'ERROR' for r in results))
    except (espo.Hold, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print('STOP: ' + str(exc) + '\nPOST не повторять при task_intent; нужна сверка CRM.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
