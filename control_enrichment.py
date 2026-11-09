"""Bounded observations from saved analysis only; never update source contacts."""
import csv
import hashlib
import io
import json
import re
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

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
        if engine['status'] == 'ERROR':
            return 'ERROR', engine['reason'] or 'analysis_error'
        if engine['status'] == 'SKIPPED' and engine.get('qualification') == 'HOLD':
            return 'HOLD', engine['reason'] or 'site_unavailable_for_analysis'
        review = json.loads(read_file(root, base / 'sites/row-000002/review.json', 1_000_000))
        findings = review['findings']
        if not isinstance(findings, list):
            raise ValueError('Invalid findings')
        consent_findings = [f for f in findings if isinstance(f, dict)
                            and f.get('rule_id') == 3 and f.get('confirmed') is True
                            and f.get('commercial_ready') is True
                            and isinstance(f.get('evidence'), list)
                            and any(isinstance(p, dict)
                                    and p.get('form_check') == 'name_contact_consent'
                                    and p.get('checkbox_state') in {'missing', 'preset'}
                                    and p.get('quote') and p.get('checked')
                                    and isinstance(p.get('artifacts'), list) and p['artifacts']
                                    and all(isinstance(a, dict) and isinstance(a.get('path'), str)
                                            and a['path'] and isinstance(a.get('sha256'), str)
                                            and re.fullmatch(r'[0-9a-fA-F]{64}', a['sha256'])
                                            for a in p['artifacts'])
                                    for p in f['evidence'])]
        findings = [f for f in findings if isinstance(f, dict)
                    and f.get('confirmed') is True and f.get('evidence')
                    and f.get('rule_id') in {1, 2, 3, 5, 6, 7, 8}
                    and (not f.get('manual_review') or f in consent_findings)]
        # Internal opportunities survive HOLD in review.json; they are not client claims.
        findings = [f for f in findings if f['rule_id'] in {1, 2, 3, 5}
                    and f.get('legal_violation_established') is not False
                    and all(isinstance(p, dict) and p.get('legal_violation_established') is not False
                            for p in f['evidence'])]
        if not findings:
            return 'HOLD', review.get('pdf_error') or 'no_confirmed_approved_block_basis'
        if (engine['status'] != 'SUCCESS' or not review.get('pdf_ready')
                or review.get('pdf_error')):
            return 'HOLD', review.get('pdf_error') or review.get('reason') or 'proposal_generation_requires_review'
        directory = base / 'sites/row-000002'
        page = read_file(root, directory / 'proposal.html', 8_000_000).decode('utf-8-sig')
        pdf = read_file(root, directory / 'proposal.pdf', 8_000_000)
        from src.lexradar.client_proposal import proposal_text
        from src.lexradar.proposal_rules import BLOCKS

        rules = {f['rule_id'] for f in findings} | {4}
        rendered = {int(n) for n in re.findall(r'data-rule="(\d+)"', page)}
        text = ' '.join(proposal_text(page).split())
        if (not pdf.startswith(b'%PDF-') or b'%%EOF' not in pdf[-1024:]
                or len(text) < 500 or rendered != rules
                or 'data-commercial-opportunity=' in page
                or 'Вопрос, требующий проверки' in text
                or any(' '.join(paragraph.split()) not in text
                       for number in rules for paragraph in BLOCKS[number]['paragraphs'])):
            return 'HOLD', 'proposal_content_requires_review'
        if not PdfReader(io.BytesIO(pdf)).pages:
            return 'HOLD', 'proposal_pdf_has_no_pages'
        return 'READY', 'usable_findings_and_substantive_proposal'
    except (OSError, ValueError, TypeError, KeyError, RecursionError, PyPdfError):
        return 'HOLD', 'saved_proposal_requires_review'


def observation(root, job, host):
    base = Path(job['id']) / 'analysis'
    result = dict(contacts=[], status='', qualification='', reason='', warning='',
                  commercial_opportunity_count=0)
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
            review = json.loads(read_file(root, base / 'sites/row-000002/review.json', 1_000_000))
            result['commercial_opportunity_count'] = sum(
                isinstance(o, dict) and o.get('status') == 'READY'
                for o in review.get('commercial_opportunities', [])
            )
            binding = review.get('control_center')
            if binding and (not isinstance(binding, dict) or binding.get('job_id') != job['id'] or binding.get('domain') != host):
                result.update(qualification='HOLD', reason='review_run_mismatch')
            elif binding and binding.get('hashes') and result['qualification'] == 'READY':
                if not isinstance(binding['hashes'], dict) or set(binding['hashes']) != {'pdf', 'html'} or any(hashlib.sha256(read_file(root, base / 'sites/row-000002' / ('proposal.' + ext), 8_000_000)).hexdigest() != sha
                       for ext, sha in binding['hashes'].items()):
                    result.update(qualification='HOLD', reason='proposal_hash_mismatch')
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    if job['status'] != 'succeeded':
        result.update(qualification='ERROR' if job['status'] in {'failed', 'cancelled'} else '',
                      reason='analysis_' + job['status'])
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
