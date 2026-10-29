"""Read saved site observations and adjacent proposal PDFs; never run LexRadar."""

import hashlib
import json
import os
import stat
from pathlib import Path

from control_organizations import MAX_BYTES

def read_file(root, relative, limit=MAX_BYTES, *, prefix=None):
    """Open every path component without following links, including the root."""
    relative = Path(relative)
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise ValueError('Invalid relative path')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = (*Path(root).absolute().parts[1:], *relative.parts)
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=descriptor)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError('Unsupported file')
            raw = stream.read(min(prefix, limit + 1) if prefix is not None else limit + 1)
            if len(raw) > limit:
                raise ValueError('File too large')
            return raw
    finally:
        os.close(descriptor)


def rows(data, key):
    value = data.get(key, [])
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def document_id(relative):
    return hashlib.sha256(str(relative).encode()).hexdigest()


def proposals(root, record):
    """Only known proposal names beside this result, never collected site PDFs."""
    if record.get('kind') == 'parser':
        return []
    directory = Path(record['source']).parent
    documents = []
    try:
        # shortcut: list at most 2048 entries / 64 PDFs; use a focused results directory for larger archives.
        with os.scandir(Path(root) / directory) as entries:
            for index, entry in enumerate(entries):
                if index >= 2048 or len(documents) >= 64:
                    break
                if entry.name != 'proposal.pdf' and not (
                    entry.name.startswith('proposal-') and entry.name.endswith('.pdf')
                ):
                    continue
                relative = directory / entry.name
                try:
                    raw = read_file(root, relative, 50_000_000, prefix=5)
                    if raw.startswith(b'%PDF-'):
                        documents.append({'id': document_id(relative), 'name': entry.name,
                                          'relative': relative})
                except (OSError, ValueError):
                    continue
    except OSError:
        pass
    return sorted(documents, key=lambda d: d['name'])


def audit_details(root, record):
    if record.get('kind') == 'parser' or Path(record['source']).name != 'audit.json':
        return None
    try:
        data = json.loads(read_file(root, record['source']))
        if isinstance(data, dict) and data.get('schema_version') == 'primary-audit-0.1':
            return data
    except (OSError, ValueError, RecursionError):
        pass
    return None
