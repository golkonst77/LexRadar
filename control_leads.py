"""Bounded, read-only access to existing Parser v3 exports."""

import csv
import hashlib
import io
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from control_results import read_file
from control_organizations import EXCLUDED

DEFAULT_PARSER_DIR = Path('/home/golkonst/lexradar-data')
CSV_HEADERS = {'Организация', 'Официальный сайт', 'Телефон', 'Email', 'Исходная карточка'}


def text(row, key):
    value = row.get(key)
    return value.strip() if isinstance(value, str) else ''


def website(value):
    try:
        url = urlsplit(value)
        if (url.scheme in {'http', 'https'} and url.hostname and not url.username
                and not url.password and not any(c.isspace() for c in value)):
            return value
    except ValueError:
        pass
    return ''


def load_leads(root):
    root = Path(root).absolute()
    records, warnings = [], 0
    if any(p.is_symlink() for p in (root, *root.parents)):
        return [], 1
    if not root.exists():
        return [], 0
    pending = [(Path('.'), 0)]
    visited, exports, total, examined = 0, 0, 0, 0
    # shortcut: depth 2 / 64 directories / 128 exports / 32 MB per request; point at a smaller export directory for larger archives.
    while pending:
        if visited >= 64:
            warnings += 1
            break
        directory, depth = pending.pop(0)
        visited += 1
        try:
            with os.scandir(root / directory) as entries:
                names = []
                for index, entry in enumerate(entries):
                    examined += 1
                    if examined > 4096:
                        warnings += 1
                        pending.clear()
                        break
                    if index >= 2048:
                        warnings += 1
                        break
                    if entry.name.startswith('.') or entry.name in EXCLUDED:
                        continue
                    if entry.is_dir(follow_symlinks=False) and depth < 2:
                        pending.append((directory / entry.name, depth + 1))
                    elif (entry.name.endswith('.csv') and entry.name not in {'lexradar.csv', 'results.csv'}) or entry.name == 'crm-candidates.json':
                        names.append(entry.name)
            # The handoff includes the original CSV and a derived JSON: show it once.
            if 'parser.csv' in names:
                names = ['parser.csv']
            for name in sorted(names):
                exports += 1
                if exports > 128 or total >= 32_000_000:
                    return sorted(records, key=lambda r: (r['name'].casefold(), r['id'])), warnings + 1
                source = str(directory / name)
                try:
                    raw = read_file(root, source, min(8_000_000, 32_000_000 - total))
                    total += len(raw)
                    if name.endswith('.csv'):
                        reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')), delimiter=';', strict=True)
                        headers = reader.fieldnames or []
                        if not CSV_HEADERS <= set(headers) or len(headers) != len(set(headers)):
                            raise ValueError('Unsupported Parser CSV')
                        data = list(reader)
                        if any(None in row or any(v is None for v in row.values()) for row in data):
                            raise ValueError('Column count mismatch')
                        keys = ('Организация', 'Официальный сайт', 'Телефон', 'Email', 'Исходная карточка', 'Статус скрининга')
                    else:
                        data = json.loads(raw.decode('utf-8-sig'))
                        if not isinstance(data, list):
                            raise ValueError('Expected candidate list')
                        keys = ('company_name', 'website', 'phone', 'email', 'source_card', 'crm_status')
                    for number, row in enumerate(data, 1):
                        if not isinstance(row, dict) or not all(k in row for k in keys[:5]) or not text(row, keys[0]):
                            warnings += 1
                            continue
                        company, url, phone, email, card, status = [text(row, k) for k in keys]
                        records.append({
                            'id': hashlib.sha256(('parser\0' + source + '\0' + json.dumps(row, ensure_ascii=False, sort_keys=True)).encode()).hexdigest(),
                            'name': company, 'websites': [website(url)] if website(url) else [],
                            'phone': phone, 'email': email, 'source_card': card,
                            'inn': text(row, 'ИНН') or text(row, 'inn'), 'status': status,
                            'source': source, 'record_number': str(number), 'row': row,
                            'kind': 'parser',
                        })
                except (OSError, ValueError, csv.Error, RecursionError):
                    warnings += 1
        except OSError:
            warnings += 1
    return sorted(records, key=lambda r: (r['name'].casefold(), r['id'])), warnings
