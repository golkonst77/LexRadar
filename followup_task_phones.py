"""Preview existing LexRadar Task descriptions; write only with explicit --apply."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlencode

from control_followup import JOURNALS, ID, campaign_bindings, local_phones, safe_phone
from control_leads import DEFAULT_PARSER_DIR, load_leads, merge_leads
from control_jobs import Jobs
from src.lexradar import espo_export as espo

NAME = 'Контрольный звонок LexRadar'
PHONE_LINE = re.compile(r'^Телефон: не указан(?=\r?$)', re.M)
OLD_LINK = re.compile(r'https?://192\.168\.233\.105/\#Email/view/([A-Za-z0-9_-]{1,100})(?=$|[\s)\]>])')
MARKER = re.compile(r'\ALexRadar followup Email/([A-Za-z0-9_-]{1,100})\r?\n')


def proposal(task, bindings, leads, journals, source_warnings=()):
    """Return only a description proposal, leaving the input record untouched."""
    description = task.get('description')
    result = {'task_id': task['id'], 'phone': 'skip', 'links': 'skip'}
    if (task.get('deleted') or not isinstance(task.get('name'), str)
            or not (task['name'] == NAME or task['name'].startswith(NAME + ' · '))
            or not isinstance(description, str)):
        return dict(result, reason='Не подтверждены название/описание задачи')
    marker = MARKER.match(description)
    eid = marker[1] if marker else None
    aid = bindings.get(eid, {}).get('account_id')
    if (not aid or not (task.get('parentType') == 'Account' and task.get('parentId') == aid
            and task.get('accountId') in (None, '', aid)
            or task.get('accountId') == aid and not task.get('parentId') and not task.get('parentType'))):
        return dict(result, reason='Не подтверждена точная привязка Task/Email/Account')
    changed, link_count = OLD_LINK.subn(
        lambda m: 'https://crm.prostoburo.com/#Email/view/' + m[1]
        if m[1] == eid else m[0], description)
    link_count = int(changed != description)
    result.update(links='ready' if link_count else 'unchanged', link_count=link_count)
    if len(PHONE_LINE.findall(description)) != 1:
        result['reason'] = 'Нет единственной точной строки «Телефон: не указан»'
    else:
        row = {'email_id': eid, 'account_id': aid}
        warnings = local_phones([row], leads, journals)
        number = safe_phone(row.get('phone'))
        if source_warnings or warnings:
            result['reason'] = 'Источники неполны; однозначность не подтверждена'
        elif not number:
            result['reason'] = row.get('phone_reason', 'Нет пригодного номера')
        else:
            changed = PHONE_LINE.sub(lambda m: 'Телефон: ' + row['phone'], changed, count=1)
            result.update(phone='ready', phone_value=row['phone'], source=row['phone_source'])
    if changed != description:
        result['description'] = changed
    return result


def dry_run(api, leads, journals=JOURNALS, source_warnings=(), *, apply=False):
    bindings, warnings = campaign_bindings(journals)
    warnings = [*source_warnings, *warnings]
    tasks, seen, offset = [], set(), 0
    while True:
        page = api.request('GET', 'Task?' + urlencode({'maxSize': 200, 'offset': offset,
            'orderBy': 'id', 'order': 'asc', 'where[0][type]': 'startsWith',
            'where[0][attribute]': 'name', 'where[0][value]': NAME}))
        items = page.get('list')
        if not isinstance(items, list):
            raise ValueError('Не подтверждена страница Task; итог неизвестен')
        for item in items:
            tid = item.get('id') if isinstance(item, dict) else None
            if not isinstance(tid, str) or not ID.fullmatch(tid) or tid in seen:
                raise ValueError('Повтор/некорректный ID Task; итог неполный')
            seen.add(tid)
            task = api.get('Task', tid)
            if task.get('id') != tid:
                raise ValueError('Не подтверждён ID полной Task')
            tasks.append(task)
        offset += len(items)
        if not items:
            break
    rows = [proposal(task, bindings, leads, journals, warnings) for task in tasks]
    ready = sum(row['phone'] == 'ready' for row in rows)
    report = dict(mode='dry-run', complete=True, found=len(rows), phone_ready=ready,
        phone_skipped=len(rows) - ready,
        skip_reasons=dict(Counter(row['reason'] for row in rows if row['phone'] == 'skip')),
        links_ready=sum(row['links'] == 'ready' for row in rows),
        links_skipped=sum(row['links'] == 'skip' for row in rows),
        warnings=warnings, tasks=rows)
    if apply:
        apply_descriptions(api, report, tasks)
    return report


def apply_descriptions(api, report, snapshots):
    """One PUT at most per Task; unknown writes are verified, never retried."""
    report.update(mode='apply', updated=0, skipped=0, unverified=0)
    for row, snapshot in zip(report['tasks'], snapshots):
        row['result'] = 'skipped'
        if 'description' not in row:
            row['apply_reason'] = 'Нет изменений'
            report['skipped'] += 1
            continue
        attempted = False
        try:
            current = api.get('Task', row['task_id'])
            if current != snapshot:
                row['apply_reason'] = 'Task изменилась после снимка'
                report['skipped'] += 1
                continue
            attempted = True
            try:
                api.request('PUT', 'Task/' + row['task_id'], {'description': row['description']})
            except (espo.Hold, OSError, ValueError, TypeError):
                pass  # A lost response can still mean success; only GET can confirm it.
            checked = api.get('Task', row['task_id'])
            audit = {'modifiedAt', 'modifiedById', 'modifiedByName'}
            if (checked.get('description') != row['description'] or
                    {k: v for k, v in checked.items() if k not in audit | {'description'}} !=
                    {k: v for k, v in current.items() if k not in audit | {'description'}}):
                raise ValueError('Проверка после записи не пройдена')
            row['result'] = 'updated'
            report['updated'] += 1
        except (espo.Hold, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            row.update(result='unverified' if attempted else 'skipped',
                       apply_reason='Исход записи не подтверждён; сверить вручную' if attempted
                       else 'Повторное чтение не подтверждено', error_type=type(exc).__name__)
            report['unverified' if attempted else 'skipped'] += 1
    report['complete'] = report['unverified'] == 0


def phone_leads(parser_dir, state_dir):
    """Use the table's import/run identities without starting Jobs or its worker."""
    leads, errors = load_leads(parser_dir)
    database = Path(state_dir) / 'jobs.sqlite3'
    if database.exists():
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            # collected/list/get only read; __init__ would start the worker.
            jobs = Jobs.__new__(Jobs)
            jobs.state_dir, jobs.db, jobs.lock = Path(state_dir), db, threading.RLock()
            collected, count = jobs.collected()
            leads += collected
            errors += count
    return merge_leads(leads), errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parser-dir', type=Path, default=DEFAULT_PARSER_DIR)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true', help='GET-only (default)')
    mode.add_argument('--apply', action='store_true', help='Update description only, with fresh GET and verification')
    args = parser.parse_args()
    try:
        leads, errors = phone_leads(args.parser_dir, Path(__file__).resolve().parent / '.control-center-state')
        warnings = ['Индекс локальных организаций/CSV неполон'] if errors or not leads else []
        report = dry_run(espo.API(), leads, source_warnings=warnings, apply=args.apply)
    except (espo.Hold, OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(json.dumps(dict(mode='apply' if args.apply else 'dry-run', complete=False, found=None,
            phone_ready=None, phone_skipped=None, error_type=type(exc).__name__,
            error=str(exc) if isinstance(exc, espo.Hold) and (str(exc).startswith('CRM connection failed')
                or str(exc) == 'ESPO_API_KEY missing') else 'Чтение не подтверждено; итог неизвестен'), ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
