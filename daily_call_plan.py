"""Daily top ten existing LexRadar call Tasks. GET-only; never starts a worker."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime
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


def phone_for(task, account, row):
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
    return next(iter(numbers)) if len(numbers) == 1 and not numbers & blocked else None


def plan(api, leads, directory, journals=JOURNALS, now=None):
    now = now or datetime.now(MOSCOW)
    bindings, warnings = campaign_bindings(journals)
    if warnings or not bindings:
        raise ValueError('История экспорта неполна или отсутствует')
    scores, networks = evidence(journals)
    blocked = local_blocks(directory)
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
            blocked.add(aid)
    candidates, skipped = [], Counter()
    for task in tasks:
        try:
            name = task.get('name', '')
            if task.get('deleted') or not (name == NAME or name.startswith(NAME + ' · ')):
                raise ValueError('Название/удалённая Task')
            marker = MARKER.match(task.get('description') or '')
            eid = marker[1] if marker else None
            aid = bindings.get(eid, {}).get('account_id')
            if not aid or not ((task.get('parentType') == 'Account' and task.get('parentId') == aid
                    and task.get('accountId') in (None, '', aid)) or
                    (task.get('accountId') == aid and not task.get('parentId') and not task.get('parentType'))):
                raise ValueError('Неоднозначная привязка')
            if aid in blocked:
                raise ValueError('Локальный отказ/обработано')
            if task.get('status') in (None, '', 'Completed', 'Canceled') or task.get('callResult') not in (None, '', 'Не звонили'):
                raise ValueError('Статус/результат звонка')
            due = sent_time(task.get('dateEnd'))
            if due > now:
                raise ValueError('Срок ещё не наступил')
            email, account = api.get('Email', eid), api.get('Account', aid)
            if (email.get('id') != eid or email.get('deleted') or email.get('status') != 'Sent'
                    or email.get('sendAt') or email.get('parentType') != 'Account' or email.get('parentId') != aid
                    or sent_time(email.get('dateSent')) > now or account.get('id') != aid or account.get('deleted')):
                raise ValueError('Email/Account не подтверждены')
            row = dict(email_id=eid, account_id=aid)
            if local_phones([row], leads, journals) or 'несколько разных' in row.get('phone_reason', ''):
                raise ValueError('Неоднозначные локальные телефоны')
            phone = phone_for(task, account, row)
            if not phone:
                raise ValueError('Нет однозначного доступного телефона')
            priority, negative_count, reason = scores.get(eid, (1, 0, 'Нет структурированных данных КП; нейтральный балл'))
            network = any(k.startswith('name:') or len(key_accounts[k] - {None}) > 1 for k in keys.get(eid, set()))
            reason += (f'; подтверждённых нарушений: {-negative_count}' if eid in scores else '') + '; ' + ('сеть' if network else 'признаков сети в локальных данных нет')
            candidates.append(((priority, negative_count, network, due, task['id']), aid,
                dict(task_id=task['id'], name=name, phone=phone, reason=reason,
                     due=due.astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M МСК'),
                     url='https://crm.prostoburo.com/#Task/view/' + task['id'])))
        except (espo.Hold, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            # CRM errors can contain private response details; print only their category.
            skipped[type(exc).__name__] += 1
    result, accounts = [], set()
    for rank, aid, row in sorted(candidates):
        if aid not in accounts:
            result.append(row)
            accounts.add(aid)
        if len(result) == 10:
            break
    return dict(mode='dry-run', found=len(tasks), selected=len(result), tasks=result,
                skipped=dict(skipped), note='Меньше 10, если пригодных уникальных Account недостаточно')


def save_local(directory, build, now=None):
    day = (now or datetime.now(MOSCOW)).astimezone(MOSCOW).date().isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'daily-call-plan-{day}.json'
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            pass
        report = dict(build(), mode='save-local', date=day)
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
            leads, errors = phone_leads(args.parser_dir, directory)
            if errors or not leads:
                raise ValueError('Локальные источники неполны')
            return plan(espo.API(), leads, directory)
        report = save_local(directory, build) if args.save_local else build()
    except (espo.Hold, OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(json.dumps(dict(mode='save-local' if args.save_local else 'dry-run', complete=False, error_type=type(exc).__name__,
                              error='Чтение не подтверждено; план не сформирован'), ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
