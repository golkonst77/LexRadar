"""Read existing primary-audit results without importing or running LexRadar."""

import hashlib
import json
import os
import stat
from pathlib import Path

MAX_BYTES = 8_000_000
EXCLUDED = {"examples", "tests", "docs", "node_modules", "__pycache__"}


def load_organizations(root):
    root = Path(root).absolute()
    records, warnings = [], 0
    if any(p.is_symlink() for p in (root, *root.parents)):
        return [], 1
    if not root.exists():
        return [], 0

    def walk_error(error):
        nonlocal warnings
        warnings += 1

    visited = 0
    try:
        # fwalk + dir_fd keep reads tied to the visited directory; never follow links.
        for directory, dirs, files, descriptor in os.fwalk(
            root, follow_symlinks=False, onerror=walk_error
        ):
            visited += 1
            if visited > 128:
                warnings += 1
                break
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in EXCLUDED)
            if len(Path(directory).relative_to(root).parts) >= 8 and dirs:
                warnings += 1
                dirs[:] = []
            name = 'audit.json' if 'audit.json' in files else 'organizations.json'
            if name not in files:
                continue
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=descriptor)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                        raise ValueError('Unsupported result file')
                    raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError('Result too large')
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError('Expected object')
                if name == 'audit.json':
                    if data.get('schema_version') != 'primary-audit-0.1':
                        raise ValueError('Unsupported audit schema')
                    rows = data.get('organizations')
                else:
                    if data.get('audience') != 'internal_only':
                        raise ValueError('Unsupported organization matrix')
                    rows = data.get('rows')
                if not isinstance(rows, list):
                    raise ValueError('Expected rows')
                source = str((Path(directory) / name).relative_to(root))
                seen = set()
                for row in rows:
                    if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                            or not isinstance(row.get('name'), str) or row['id'] in seen):
                        warnings += 1
                        continue
                    seen.add(row['id'])
                    target = data.get('target_url')
                    # Citation URLs locate mentions, not the organization's own website.
                    urls = [target] if len(rows) == 1 and isinstance(target, str) else []
                    records.append({
                        'id': hashlib.sha256((source + '\0' + row['id']).encode()).hexdigest(),
                        'name': row['name'],
                        'inn': row.get('inn') if isinstance(row.get('inn'), str) else '',
                        'websites': urls,
                        'status': data.get('legal_status')
                        if isinstance(data.get('legal_status'), str) else 'unknown',
                        'source': source,
                        'row': row,
                    })
            except (OSError, ValueError, RecursionError):
                warnings += 1
    except (OSError, RecursionError):
        warnings += 1
    return sorted(records, key=lambda r: (r['name'].casefold(), r['id'])), warnings
