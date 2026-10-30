"""Publish one verified proposal pair per domain; never write CRM journals."""
import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path

from control_analysis import target
from control_enrichment import observation
from control_results import read_file


def directory(identifier):
    if not isinstance(identifier, str) or not identifier or Path(identifier).name != identifier or identifier in {'.', '..'}:
        raise ValueError('Invalid job ID')
    return Path(identifier) / 'analysis/sites/row-000002'


def publish(jobs, job, *, legacy=False):
    """Called under the queue lock after completion, and at startup for legacy runs."""
    params = json.loads(job['params'])
    host = target(params['url'])[1]
    if host != params['domain']:
        raise ValueError('Job domain mismatch')
    base = directory(job['id'])
    saved = observation(jobs.state_dir, job, host)
    try:
        review = json.loads(read_file(jobs.state_dir, base / 'review.json', 1_000_000))
    except (OSError, ValueError):
        return
    if not isinstance(review, dict):
        return
    binding = review.get('control_center', {})
    if not isinstance(binding, dict) or binding and (binding.get('job_id') != job['id'] or binding.get('domain') != host):
        return
    if binding and review.get('qualification') == 'READY' and saved['qualification'] != 'READY':
        return
    review.setdefault('engine_qualification', review.get('qualification'))
    review.setdefault('engine_reason', review.get('reason'))
    review.update(qualification=saved['qualification'] or 'ERROR', reason=saved['reason'])
    review['control_center'] = dict(job_id=job['id'], domain=host,
                                    date=job['ended'] or job['created'],
                                    cleanup_allowed=binding.get('cleanup_allowed', not legacy))
    hashes = {}
    if saved['qualification'] == 'READY' and job['status'] == 'succeeded':
        hashes = {ext: hashlib.sha256(read_file(jobs.state_dir, base / ('proposal.' + ext), 8_000_000)).hexdigest()
                  for ext in ('pdf', 'html')}
        review['control_center']['hashes'] = hashes
    # A complete review is durable before the single SQLite pointer is switched.
    path = jobs.state_dir / base / 'review.json'
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(review, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
    if saved['qualification'] != 'READY' or job['status'] != 'succeeded':
        return
    current = jobs.db.execute('SELECT job_id FROM current_offers WHERE domain=?', (host,)).fetchone()
    if current:
        old_order = jobs.db.execute('SELECT rowid FROM jobs WHERE id=?', (current['job_id'],)).fetchone()[0]
        new_order = jobs.db.execute('SELECT rowid FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
        if old_order > new_order:
            return
    try:
        jobs.db.execute('INSERT OR REPLACE INTO current_offers(domain,job_id,hashes) VALUES(?,?,?)',
                        (host, job['id'], json.dumps(hashes)))
        jobs.db.commit()
    except Exception:
        jobs.db.rollback()
        raise


def current_offers(jobs):
    with jobs.lock:
        return {r['domain']: dict(r) for r in jobs.db.execute('SELECT * FROM current_offers')}


def cleanup(jobs):
    """Only obsolete PDF/HTML, with the same journal locks used by CRM writers."""
    # shortcut: custom/external journals cannot be discovered; retain legacy unbound files and require operator reconciliation for external exports.
    journal_dir = Path.home() / '.local/state/lexradar'
    try:
        with ExitStack() as stack:
            journals = []
            for name in ('crm-prepare.json', 'espo-export.json'):
                path = journal_dir / name
                # Missing/unreadable journals do not establish that deletion is safe.
                if not path.is_file() or path.is_symlink() or path.stat().st_size > 16_000_000:
                    return
                lock = stack.enter_context(path.with_suffix('.lock').open('r'))
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raw = path.read_text()
                if not isinstance(json.loads(raw), dict):
                    return
                journals.append(raw)
            references = '\n'.join(journals)
            current = current_offers(jobs)
            for job in jobs.analysis_history().values():
                host = json.loads(job['params'])['domain']
                active = current.get(host)
                if not active or active['job_id'] == job['id'] or job['status'] != 'succeeded':
                    continue
                if jobs.db.execute('SELECT rowid FROM jobs WHERE id=?', (job['id'],)).fetchone()[0] > jobs.db.execute(
                        'SELECT rowid FROM jobs WHERE id=?', (active['job_id'],)).fetchone()[0]:
                    continue
                base = directory(job['id'])
                try:
                    review = json.loads(read_file(jobs.state_dir, base / 'review.json', 1_000_000))
                    if not isinstance(review, dict):
                        continue
                    binding = review.get('control_center', {})
                    if (not isinstance(binding, dict) or binding.get('job_id') != job['id']
                            or not binding.get('cleanup_allowed')
                            or review.get('qualification') != 'READY'):
                        continue
                    files = [base / ('proposal.' + ext) for ext in ('pdf', 'html')]
                    data = [read_file(jobs.state_dir, p, 8_000_000) for p in files]
                    # Domain references also protect bindings whose digest hides the PDF path.
                    if host in references or job['id'] in references or any(
                            hashlib.sha256(raw).hexdigest() in references for raw in data):
                        continue
                    if not verified(jobs, active):
                        continue
                    for path in files:
                        (jobs.state_dir / path).unlink()
                except (OSError, ValueError, TypeError):
                    continue
    except (OSError, ValueError, TypeError):
        return


def verified(jobs, offer):
    try:
        job = jobs.get(offer['job_id'])
        if not job or job['status'] != 'succeeded':
            return False
        host = offer['domain']
        params = json.loads(job['params'])
        if params.get('domain') != host or target(params.get('url'))[1] != host:
            return False
        if observation(jobs.state_dir, job, host)['qualification'] != 'READY':
            return False
        base = directory(job['id'])
        hashes = json.loads(offer['hashes'])
        if set(hashes) != {'pdf', 'html'}:
            return False
        return all(hashlib.sha256(read_file(jobs.state_dir, base / ('proposal.' + ext), 8_000_000)).hexdigest() == sha
                   for ext, sha in hashes.items())
    except (OSError, ValueError, KeyError, TypeError):
        return False
