#!/usr/bin/env python3
"""Offline history merge. Default: rehearsal on SQLite backup copies. Stop writers first."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT / '.control-center-state/3ed1d1201c7343d5ad846d6852bce9b9'
NEW = ROOT / '.control-center-state/medadvisor-sequential'


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def connect(path, readonly=False):
    return sqlite3.connect(path.resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True, timeout=5)


def check(db):
    require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'quick_check failed')
    require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'foreign keys failed')


def backup(source, target):
    with connect(source, True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
        check(dst)


def rows(db, table):
    return db.execute('SELECT * FROM "' + table + '" ORDER BY 1').fetchall()


def merge(old_path, new_path, old_pages, new_pages):
    old_bytes, new_bytes = old_pages.read_bytes(), new_pages.read_bytes()
    old_proofs, new_proofs = json.loads(old_bytes), json.loads(new_bytes)
    proofs = {p['requested_page']: p for p in new_proofs}
    require(set(proofs) == {1}, 'Expected only new page 1')
    for p in old_proofs:
        if 2 <= p['requested_page'] <= 9:
            require(p['verified'] and p['actual_page'] == p['requested_page'], 'Invalid old proof')
            proofs.setdefault(p['requested_page'], p)
    require(set(proofs) == set(range(1, 10)), 'Missing page proofs')
    payload = (json.dumps([proofs[k] for k in sorted(proofs)], ensure_ascii=False, indent=2) + '\n').encode()
    with connect(old_path, True) as old, connect(new_path) as new:
        new.execute('PRAGMA foreign_keys=ON')
        new.execute('BEGIN IMMEDIATE')
        installed = False
        temp = new_pages.with_name(new_pages.name + '.merge-tmp')
        try:
            check(old)
            check(new)
            tables = [r[0] for r in new.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            before = {t: rows(new, t) for t in tables}
            existing = before['collection_decisions']
            require(len(existing) == 32 and len(before['companies']) == 20, 'Unexpected destination counts; refusing repeat or changed input')
            urls = {r[2] for r in existing}
            source = rows(old, 'collection_decisions')
            require(len(source) == len({r[2] for r in source}) == 603, 'Old URL uniqueness/count failed')
            require(len(urls) == 32, 'New URL uniqueness failed')
            missing = [r for r in source if r[2] not in urls]
            require(len(missing) == 571, 'Expected 571 missing decisions')
            run_map = {}
            for run_id in sorted({r[1] for r in missing}):
                run = old.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
                require(run is not None, 'Missing source run')
                cur = new.execute('INSERT INTO runs(started_at,finished_at,status,stats_json) VALUES (?,?,?,?)', run[1:])
                run_map[run_id] = cur.lastrowid
            for r in missing:
                new.execute('INSERT INTO collection_decisions(run_id,company_url,decided_at,accepted,reasons_json,details_json) VALUES (?,?,?,?,?,?)', (run_map[r[1]], *r[2:]))
            after = rows(new, 'collection_decisions')
            reviewed = {r[2] for r in after}
            require(len(after) == len(reviewed) == 603, 'Final count/uniqueness failed')
            require(all(r in after for r in existing), 'New decisions changed')
            for t in tables:
                if t not in ('runs', 'collection_decisions'):
                    require(rows(new, t) == before[t], 'Existing data changed: ' + t)
            require(all(r in rows(new, 'runs') for r in before['runs']), 'Existing runs changed')
            next_card = next((k, i, u) for k in sorted(proofs) for i, u in enumerate(proofs[k]['cards'], 1) if u not in reviewed)
            require(next_card == (7, 10, 'https://medadvisor.ru/shchelkovo/clinic/4846'), 'Unexpected resume cursor')
            check(new)
            require(new_pages.read_bytes() == new_bytes and old_pages.read_bytes() == old_bytes, 'Snapshots changed concurrently')
            with temp.open('xb') as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, new_pages)
            installed = True
            new.commit()
        except BaseException:
            new.rollback()
            if installed:
                new_pages.write_bytes(new_bytes)
            if temp.exists():
                temp.unlink()
            raise
        check(new)
        require(json.loads(new_pages.read_bytes())[0] == new_proofs[0], 'New page 1 changed')
        return dict(added=len(missing), decisions=len(after), companies=20, added_runs=len(run_map), run_mapping=run_map, pages=list(sorted(proofs)), next_card=next_card, quick_check='ok', foreign_key_check=[], new_data_preserved=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Apply in place; all parser/server writers must be stopped')
    args = parser.parse_args()
    if args.apply:
        folder = NEW / ('history-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        folder.mkdir(mode=0o700)
        for label, directory, filename in [('old', OLD, 'parser.catalog_pages.json'), ('new', NEW, 'catalog_pages.json')]:
            backup(directory / 'parser.sqlite', folder / (label + '.sqlite'))
            (folder / (label + '.catalog_pages.json')).write_bytes((directory / filename).read_bytes())
        print('Backups:', folder, flush=True)
        result = merge(OLD / 'parser.sqlite', NEW / 'parser.sqlite', OLD / 'parser.catalog_pages.json', NEW / 'catalog_pages.json')
    else:
        with tempfile.TemporaryDirectory(prefix='medadvisor-merge-check-') as directory:
            folder = Path(directory)
            for label, source, filename in [('old', OLD, 'parser.catalog_pages.json'), ('new', NEW, 'catalog_pages.json')]:
                backup(source / 'parser.sqlite', folder / (label + '.sqlite'))
                (folder / (label + '.json')).write_bytes((source / filename).read_bytes())
            result = merge(folder / 'old.sqlite', folder / 'new.sqlite', folder / 'old.json', folder / 'new.json')
    print(json.dumps(dict(applied=args.apply, **result), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
