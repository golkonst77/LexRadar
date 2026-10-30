"""Bounded cached index of local exports and saved results. No network or writes."""
import json
import os
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from control_leads import load_leads, website
from control_organizations import EXCLUDED, load_organizations
from control_results import document_id, read_file


def domain(value):
    value = website(value)
    if not value:
        return ''
    host = urlsplit(value).hostname.rstrip('.').lower()
    return host.removeprefix('www.')


def discover(root):
    """Names and metadata only, with a hard budget even in very wide trees."""
    root = Path(root).absolute()
    found, warnings, examined = [], 0, 0
    if any(p.is_symlink() for p in (root, *root.parents)):
        return [], 1
    if not root.exists():
        return [], 0
    pending = [(Path('.'), 0)]
    visited = 0
    while pending:
        visited += 1
        if visited > 512:
            return found, warnings + 1
        directory, depth = pending.pop(0)
        try:
            with os.scandir(root / directory) as entries:
                for entry in entries:
                    examined += 1
                    if examined > 20000:
                        return found, warnings + 1
                    if entry.name.startswith('.') or entry.name in EXCLUDED:
                        continue
                    relative = directory / entry.name
                    if entry.is_dir(follow_symlinks=False):
                        if depth < 8:
                            pending.append((relative, depth + 1))
                        else:
                            warnings += 1
                    elif entry.is_file(follow_symlinks=False) and (
                        entry.name in {'audit.json', 'organizations.json', 'crm-candidates.json'}
                        or entry.name.endswith('.csv') or entry.name.endswith('crm-result.json')
                        or entry.name == 'proposal.pdf'
                        or (entry.name.startswith('proposal-') and entry.name.endswith('.pdf'))
                    ):
                        info = entry.stat(follow_symlinks=False)
                        found.append((str(relative), info.st_mtime_ns, info.st_size))
                        if len(found) >= 1024:
                            return sorted(found), warnings + 1
        except OSError:
            warnings += 1
    return sorted(found), warnings


class Catalog:
    def __init__(self, results_dir, parser_dir):
        self.results_dir, self.parser_dir = Path(results_dir), Path(parser_dir)
        self.lock = threading.Lock()
        self.checked = 0
        self.signature = None
        self.data = None

    def snapshot(self, force=False):
        with self.lock:
            if not force and self.data is not None and time.monotonic() - self.checked < 5:
                return self.data
            files, warnings = discover(self.results_dir)
            parser_files, parser_warnings = discover(self.parser_dir)
            signature = (files, parser_files, warnings, parser_warnings)
            if force or signature != self.signature:
                leads, lw = load_leads(self.parser_dir)
                mentions, mw = load_organizations(self.results_dir)
                import_dates = {source: datetime.fromtimestamp(modified / 1e9, timezone.utc).isoformat(timespec='seconds')
                                for source, modified, _ in parser_files}
                for lead in leads:
                    lead['import_file_date'] = import_dates.get(lead['source'], '')
                reports, crm, budget = [], [], 64_000_000
                for source, modified, size in files:
                    name = Path(source).name
                    kind = ('audit' if name == 'audit.json' else 'pdf' if name.endswith('.pdf')
                            else 'crm' if name.endswith('crm-result.json') else '')
                    if not kind:
                        continue
                    if size > (50_000_000 if kind == 'pdf' else 8_000_000):
                        warnings += 1
                        continue
                    # Indexing reads only the PDF signature; full bytes are read when opened.
                    data = {}
                    if kind == 'pdf':
                        try:
                            if not read_file(self.results_dir, source, 50_000_000, prefix=5).startswith(b'%PDF-'):
                                raise ValueError('Invalid PDF')
                        except (OSError, ValueError):
                            warnings += 1
                            continue
                    else:
                        if size > budget:
                            warnings += 1
                            continue
                        budget -= size
                        try:
                            data = json.loads(read_file(self.results_dir, source))
                        except (OSError, ValueError, RecursionError):
                            warnings += 1
                            continue
                        if kind == 'audit' and (not isinstance(data, dict) or data.get('schema_version') != 'primary-audit-0.1'):
                            warnings += 1
                            continue
                    date = datetime.fromtimestamp(modified / 1e9, timezone.utc).isoformat(timespec='seconds')
                    if kind == 'crm':
                        if not isinstance(data, list):
                            warnings += 1
                            continue
                        for row in data:
                            if isinstance(row, dict) and isinstance(row.get('status'), str):
                                # Explicit fields only: never expose credentials or arbitrary result bodies.
                                identity = row.get('identity') if isinstance(row.get('identity'), dict) else {}
                                materials = row.get('materials') if isinstance(row.get('materials'), list) else []
                                pdf = next((m for m in materials if isinstance(m, dict) and m.get('document_id')), {})
                                crm.append({'source': source, 'date': date, 'id': row.get('id', ''),
                                            'company_name': row.get('company_name') or identity.get('company_name', ''),
                                            'website': row.get('website', ''), 'status': row['status'],
                                            'account_id': row.get('account_id', ''),
                                            'document_id': row.get('document_id') or pdf.get('document_id', '')})
                        continue
                    reports.append(dict(id=document_id(source), source=source, kind=kind, date=date,
                                        target=website(data.get('target_url', '')), size=size))
                counts = Counter(domain(url) for lead in leads for url in lead['websites'])
                audit_targets = {str(Path(r['source']).parent): r['target'] for r in reports if r['kind'] == 'audit'}
                names = {domain(url): lead['name'] for lead in leads for url in lead['websites']
                         if counts[domain(url)] == 1}
                for report in reports:
                    if report['kind'] == 'pdf':
                        report['target'] = audit_targets.get(str(Path(report['source']).parent), '')
                    report['lead_name'] = names.get(domain(report['target']), '')
                self.data = dict(leads=leads, mentions=mentions, reports=reports, crm=crm,
                                 domain_counts=counts, warnings=warnings + parser_warnings + lw + mw,
                                 indexed_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
                self.signature = signature
            self.checked = time.monotonic()
            return self.data

    def linked(self, lead, data=None):
        data = self.snapshot() if data is None else data
        host = domain(next(iter(lead['websites']), ''))
        if not host or data['domain_counts'][host] != 1:
            return []
        audits = [r for r in data['reports'] if r['kind'] == 'audit' and domain(r['target']) == host]
        directories = {Path(r['source']).parent for r in audits}
        return audits + [r for r in data['reports'] if r['kind'] == 'pdf' and Path(r['source']).parent in directories]
