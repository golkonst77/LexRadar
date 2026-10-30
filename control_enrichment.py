"""Bounded observations from saved analysis only; never update source contacts."""
import csv
import io
import json
import re
from pathlib import Path

from control_analysis import target
from control_results import read_file

COLLECTION = 'sites/row-000002/dossier/collection.json'
EMAIL = re.compile(r"(?<![\w.!#$%&'*+/=?^`{|}~@-])([A-Za-z0-9.!#$%&'*+/=?^`{|}~-]+@[A-Za-z0-9.-]+)(?![\w@-])")


def emails(text):
    for match in EMAIL.finditer(text):
        value = match[1].rstrip('.').lower()
        local, host = value.rsplit('@', 1)
        labels = host.split('.')
        if (len(value) <= 254 and len(local) <= 64 and not local.startswith('.')
                and not local.endswith('.') and '..' not in local and len(labels) >= 2
                and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in labels)
                and re.fullmatch(r'[a-z]{2,63}', labels[-1])
                and labels[-1] not in {'test', 'example', 'invalid', 'localhost', 'local', 'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'css', 'js'}
                and host not in {'example.com', 'example.org', 'example.net', 'domain.com', 'email.com'}
                and local not in {'email', 'your-email', 'yourname', 'name', 'user', 'username'}):
            yield value


def commercial_decision(root, base, engine):
    """Commercial readiness only; saved engine/legal release gates stay untouched."""
    try:
        review = json.loads(read_file(root, base / 'sites/row-000002/review.json', 1_000_000))
        findings = review['findings']
        if not isinstance(findings, list):
            raise ValueError('Invalid findings')
        if not findings:
            return 'REJECT', 'no_usable_findings'
        if any(not isinstance(f, dict) or f.get('confirmed') is not True
               or f.get('manual_review') or not f.get('evidence')
               or f.get('rule_id') not in {1, 2, 3, 5, 6, 7, 8} for f in findings):
            return 'HOLD', 'finding_reliability_requires_review'
        if engine['reason'] != 'manual_review_required_including_unverified_constant_block4':
            return 'HOLD', engine['reason'] or 'engine_result_requires_review'
        if (engine['status'] != 'SUCCESS' or not review.get('pdf_ready')
                or review.get('pdf_error')):
            return 'HOLD', 'proposal_generation_requires_review'
        directory = base / 'sites/row-000002'
        page = read_file(root, directory / 'proposal.html', 8_000_000).decode('utf-8-sig')
        pdf = read_file(root, directory / 'proposal.pdf', 8_000_000, prefix=5)
        if not pdf.startswith(b'%PDF-') or any(
                f'data-rule="{f["rule_id"]}"' not in page for f in findings):
            return 'HOLD', 'proposal_content_requires_review'
        return 'READY', 'usable_findings_and_substantive_proposal'
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return 'HOLD', 'saved_proposal_requires_review'


def observation(root, job, host):
    base = Path(job['id']) / 'analysis'
    result = dict(contacts=[], status='', qualification='', reason='', warning='')
    try:
        raw = read_file(root, base / 'results.csv', 1_000_000).decode('utf-8-sig')
        rows = list(csv.DictReader(io.StringIO(raw), strict=True))
        if len(rows) != 1 or target(rows[0].get('url', ''))[1] != host:
            raise ValueError('Ambiguous result')
        for key in ('status', 'qualification', 'reason'):
            result[key] = (rows[0].get(key) or '')[:2000]
    except (OSError, ValueError, TypeError, csv.Error):
        result['warning'] = 'Итог отсутствует, повреждён или связь с сайтом неоднозначна.'
    if not result['warning']:
        result['qualification'], result['reason'] = commercial_decision(root, base, result)
    try:
        data = json.loads(read_file(root, base / COLLECTION, 8_000_000))
        if (not isinstance(data, dict) or data.get('schema_version') != '0.2'
                or target(data.get('target_url', ''))[1] != host
                or not isinstance(data.get('pages'), list)):
            raise ValueError('Unsupported collection')
        budget, contacts = 1_000_000, {}
        if len(data['pages']) > 64:
            result['warning'] = 'Лимит 64 страниц; список контактов может быть неполным.'
        # shortcut: 64 pages / 1M text characters / 32 candidates; expand only for larger saved collections.
        for page in data['pages'][:64]:
            if not isinstance(page, dict) or page.get('http_status') != 200:
                continue
            url, text = page.get('final_url'), page.get('text')
            if not isinstance(url, str) or len(url) > 4096 or not isinstance(text, str):
                continue
            try:
                source_host = target(url)[1]
            except ValueError:
                continue
            if source_host != host:
                result['warning'] = 'Страницы другого домена исключены из контактов.'
                continue
            part = text[:min(budget, 100_000)]
            budget -= len(part)
            for email in emails(part):
                if email not in contacts and len(contacts) >= 32:
                    result['warning'] = 'Лимит 32 кандидатов; список контактов может быть неполным.'
                    continue
                sources = contacts.setdefault(email, [])
                if url not in sources:
                    sources.append(url)
            if budget <= 0:
                result['warning'] = 'Достигнут лимит чтения контактов; список может быть неполным.'
                break
        result['contacts'] = [dict(email=e, sources=s) for e, s in sorted(contacts.items())]
    except (OSError, ValueError, TypeError, RecursionError):
        result['warning'] = 'Досье отсутствует, повреждено или не связано с этим сайтом; контакты не извлечены.'
    return result
