"""Operator-approved, draft-only Espo export. No scheduler or sending operations."""
import argparse
import base64
import csv
import fcntl
import hashlib
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_URL = 'http://192.168.233.105/api/v1/'
SENDER = 'golwebstudio@bk.ru'
APPROVED_TEMPLATE = Path(__file__).with_name('espo_email.json')
MAX_PDF = 10 * 1024 * 1024
ADDRESS = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+\Z")


class Hold(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Hold('API redirect refused')


class API:
    def __init__(self):
        key = os.environ.get('ESPO_API_KEY')
        path = Path.home() / '.config/lexradar/espo.env'
        if not key and path.exists():
            for line in path.read_text().splitlines():
                if line.strip().startswith('ESPO_API_KEY='):
                    key = line.split('=', 1)[1].strip().strip('\"\'')
        if not key:
            raise Hold('ESPO_API_KEY missing')
        self.key = key
        self.opener = build_opener(NoRedirect)

    def request(self, method, route, payload=None, *, binary=False):
        try:
            if method == 'POST' and route == 'Account' and payload and payload.get('website'):
                website = payload['website']
                parsed = urlsplit(website)
                host = parsed.hostname
                if host and not host.isascii():
                    authority_start = website.index('//') + 2
                    host_start = authority_start + parsed.netloc.rfind('@') + 1
                    host_end = host_start + len(host)
                    payload = dict(payload, website=website[:host_start] +
                                   host.encode('idna').decode('ascii') + website[host_end:])
            request = Request(API_URL + route, method=method,
                              headers={'X-Api-Key': self.key, 'Content-Type': 'application/json'},
                              data=None if payload is None else json.dumps(payload).encode())
            with self.opener.open(request, timeout=15) as response:
                if binary:
                    return response.read(MAX_PDF + 1)
                result = json.load(response)
                if route != 'Document/action/getAttachmentList' and not isinstance(result, dict):
                    raise Hold(f'CRM invalid JSON object: {method} {route.split("?")[0]}')
                return result
        except Hold:
            raise
        except HTTPError as exc:
            detail = ''
            if exc.code == 400:
                try:
                    body = exc.read(8192).decode('utf-8', errors='replace')
                    body = body.replace(self.key, '[REDACTED]')
                    # Suppress the whole response if credentials may span multiple lines.
                    if re.search(r'(?i)token|secret|password|authorization|cookie|api[-_ ]?key', body):
                        body = '[REDACTED credential-bearing response]'
                    body = re.sub(r'(?i)bearer\s+\S+', '[REDACTED]', body)
                    detail = '; response=' + ' '.join(body.split())[:500]
                except (OSError, ValueError):
                    detail = '; response unavailable'
            raise Hold(f'CRM HTTP {exc.code}: {method} {route.split("?")[0]}; inspect CRM before retry' + detail) from None
        except (URLError, OSError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            detail = type(reason).__name__
            if isinstance(reason, OSError) and reason.errno is not None:
                detail += f' errno={reason.errno}'
            raise Hold(f'CRM connection failed ({detail}): {method} {route.split("?")[0]}') from None
        except (ValueError, UnicodeError):
            raise Hold(f'CRM invalid JSON: {method} {route.split("?")[0]}') from None

    def get(self, kind, eid):
        if not isinstance(eid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', eid):
            raise Hold('Invalid CRM ID')
        result = self.request('GET', kind + '/' + eid)
        if result.get('id') != eid or result.get('deleted'):
            raise Hold('Saved CRM record missing or changed')
        return result

    def file(self, eid):
        if not isinstance(eid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', eid):
            raise Hold('Invalid CRM ID')
        return self.request('GET', 'Attachment/file/' + eid, binary=True)

    def find(self, kind, field, value, *, operator='equals'):
        query = urlencode({'where[0][type]': operator, 'where[0][attribute]': field,
                           'where[0][value]': value, 'maxSize': 200})
        result = self.request('GET', kind + '?' + query)
        rows = result.get('list')
        if not isinstance(rows, list) or len(rows) >= 200 or result.get('total', len(rows)) > len(rows):
            raise Hold('Incomplete CRM search')
        if any(not isinstance(r, dict) or not r.get('id') or (operator == 'equals' and r.get(field) != value)
               for r in rows):
            raise Hold('CRM search did not respect exact filter')
        return rows


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.espo-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_manifest(path):
    if path.suffix.lower() == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
    else:
        rows = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(rows, list) or not rows:
        raise Hold('Manifest must be a nonempty list')
    return rows


def validate(row, root):
    if not isinstance(row, dict):
        raise Hold('Row must be an object')
    for field in ('id', 'company_name', 'website', 'pdf_path'):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise Hold('Missing ' + field)
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', row['id']):
        raise Hold('Invalid stable id')
    for field in ('export_approved',):
        if row.get(field) is not True and row.get(field) != 'true':
            raise Hold(field + ' must be true')
    email = row.get('email', '')
    if not isinstance(email, str) or (email and (not ADDRESS.fullmatch(email) or len(email) > 254 or len(email.split('@')[0]) > 64)):
        raise Hold('Invalid single email')
    phones(row)
    website = urlsplit(row['website'])
    if website.scheme not in ('http', 'https') or not website.hostname or website.username:
        raise Hold('Invalid website URL')
    path = root / row['pdf_path']
    try:
        with path.open('rb') as stream:
            data = stream.read(MAX_PDF + 1)
    except OSError:
        raise Hold('PDF missing or unreadable') from None
    if not data.startswith(b'%PDF-') or len(data) > MAX_PDF:
        raise Hold('Invalid PDF signature or size (limit 10 MiB)')
    return data, hashlib.sha256(data).hexdigest()


def phones(row):
    values = row.get('phones') or []
    if isinstance(values, str):
        values = [v.strip() for v in values.split(';') if v.strip()]
    if not isinstance(values, list) or any(not isinstance(v, str) or not re.fullmatch(r'\+?[0-9 ()-]{5,30}', v) for v in values):
        raise Hold('Invalid phones: use a list or semicolon-separated numbers')
    return list(dict.fromkeys(values))


def template_at(api, path):
    template = json.loads((path or APPROVED_TEMPLATE).read_text())
    if (not isinstance(template, dict) or not isinstance(template.get('name'), str)
            or not template['name'].strip() or not isinstance(template.get('body'), str)
            or not template['body'].strip() or type(template.get('isHtml')) is not bool):
        raise Hold('Fill template JSON: name, body, isHtml; sample unavailable/empty')
    if any(x in template['name'] + template['body'] for x in ('FILL_FROM_APPROVED_SAMPLE',)):
        raise Hold('Template must be filled from approved sample')
    if len(template['name']) > 179:
        raise Hold('Approved subject too long for CRM recovery reference (maximum 179 characters)')
    return {k: template[k] for k in ('name', 'body', 'isHtml')}


def proposal_filename(company_name):
    if (not isinstance(company_name, str) or not company_name.strip()
            or any(ord(c) < 32 or c in '/\\' for c in company_name)):
        raise Hold('Clinic name unsafe for PDF filename')
    name = 'Коммерческое предложение — ' + company_name + '.pdf'
    if len(name) > 255:
        raise Hold('PDF filename too long')
    return name


def website_domain(website):
    website = (website or '').strip()
    parsed = urlsplit(website if '://' in website else '//' + website)
    return (parsed.hostname or '').lower().removeprefix('www.')


class Exporter:
    def __init__(self, api, journal, state, definitions):
        self.api, self.journal, self.state = api, journal, state
        self.fields = definitions['Account']['fields']

    def contacts(self, row, account):
        patch = {}
        email = row.get('email', '')
        if email and row.get('recipient_verified') in (True, 'true') and not account.get('emailAddress'):
            if self.fields.get('emailAddress', {}).get('type') != 'email':
                raise Hold('CRM emailAddress schema unsupported')
            patch['emailAddress'] = email
        numbers = phones(row)
        if numbers:
            if self.fields.get('phoneNumber', {}).get('type') == 'phone':
                existing = account.get('phoneNumberData') or []
                if account.get('phoneNumber') and not existing:
                    existing = [{'phoneNumber': account['phoneNumber'], 'primary': True}]
                missing = [n for n in numbers if n not in [v['phoneNumber'] for v in existing]]
                if missing:
                    patch['phoneNumberData'] = existing + [{'phoneNumber': n, 'primary': not existing and i == 0} for i, n in enumerate(missing)]
                    if not account.get('phoneNumber'):
                        patch['phoneNumber'] = numbers[0]
            else:
                description = account.get('description') or ''
                missing = [n for n in numbers if n not in description and n != account.get('phoneNumber')]
                if missing:
                    patch['description'] = description + '\nТелефоны: ' + '; '.join(missing)
        if not account and 'cSegment' in self.fields:
            options = self.fields['cSegment'].get('options', [])
            neutral = next((v for v in ('', 'Не определено') if v in options), None)
            if neutral is None and not (row.get('segment') and row.get('industry_verified') in (True, 'true')):
                raise Hold('No safe neutral cSegment in Metadata; confirm industry first')
            if neutral is not None:
                patch['cSegment'] = neutral
        segment = row.get('segment')
        if segment and row.get('industry_verified') in (True, 'true') and not account.get('cSegment'):
            field = self.fields.get('cSegment', {})
            if field.get('type') != 'enum' or segment not in field.get('options', []):
                raise Hold('Confirmed segment unavailable in Metadata')
            patch['cSegment'] = segment
        return patch

    def ensure(self, entry, step, kind, payload, field='name'):
        steps = entry.setdefault('steps', {})
        saved = steps.get(step)
        if kind == 'Attachment':
            # Attachment metadata/search is forbidden; unknown uploads cannot be recovered blindly.
            if saved:
                if not saved.get('id'):
                    raise Hold('Uncertain Attachment POST; reconcile before retry')
                return saved['id']
            steps[step] = {'pending': True}
            save(self.journal, self.state)
            response = self.api.request('POST', kind, payload)
            eid = response.get('id')
            if not isinstance(eid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', eid):
                raise Hold('Invalid uploaded Attachment ID; reconcile before retry')
            steps[step]['id'] = eid
            save(self.journal, self.state)
            return eid
        if saved and saved.get('id'):
            record = self.api.get(kind, saved['id'])
        else:
            if kind == 'Email' and entry.get('presentation_version') == 2 and saved:
                raise Hold('Uncertain Email POST; reconcile saved intent before retry')
            found = self.api.find(kind, field, payload[field])
            if kind == 'Email' and entry.get('presentation_version') == 2 and found:
                raise Hold('Existing human-readable Email subject; reconcile before creating')
            if len(found) > 1:
                raise Hold('Ambiguous recovery marker: ' + step)
            if found:
                record = self.api.get(kind, found[0]['id'])
            elif saved:
                raise Hold('Uncertain POST ' + step + ': marker not found; manual reconciliation required')
            else:
                steps[step] = {'pending': True}
                save(self.journal, self.state)  # Durable intent before any POST.
                try:
                    response = self.api.request('POST', kind, payload)
                    eid = response['id']
                    steps[step]['id'] = eid
                    save(self.journal, self.state)
                    record = self.api.get(kind, eid)
                except Exception as exc:
                    if kind == 'Email' and entry.get('presentation_version') == 2:
                        raise Hold('Uncertain Email POST; reconcile saved intent before retry') from None
                    found = self.api.find(kind, field, payload[field])
                    if len(found) != 1:
                        raise Hold('Uncertain POST ' + step + '; manual reconciliation required; ' +
                                   (str(exc) if isinstance(exc, Hold) else type(exc).__name__)) from None
                    record = self.api.get(kind, found[0]['id'])
        for key, value in payload.items():
            if key in ('file', 'accountsIds'):
                continue
            if record.get(key) != value:
                raise Hold('CRM verification mismatch: ' + step + '/' + key)
        steps[step] = {'id': record['id']}
        save(self.journal, self.state)
        return record['id']

    def export(self, row, data, sha, template):
        if len(template['name'].replace('{company_name}', row.get('brand_name') or row['company_name'])) > 179:
            raise Hold('Personalized subject too long')
        external = row['id']
        entry = self.state.setdefault(external, {'sha256': sha, 'steps': {}})
        binding = {k: row.get(k, '') for k in ('company_name', 'website', 'email')}
        if entry['sha256'] != sha or entry.get('binding', binding) != binding:
            raise Hold('Stable ID already bound to different PDF or recipient/company')
        if entry.get('presentation_update') and not entry['presentation_update'].get('completed'):
            raise Hold('Resume saved presentation update before export')
        if entry.get('template', template) != template:
            raise Hold('Template changed for existing export')
        if 'template' not in entry:
            entry['presentation_version'] = 2
        entry.update(binding=binding, template=template)
        display_name = row.get('brand_name') or entry.get('presentation_name') or row['company_name']
        if entry.get('presentation_name', display_name) != display_name:
            raise Hold('Clinic display name changed')
        entry['presentation_name'] = display_name
        pdf_filename = (proposal_filename(display_name) if entry.get('presentation_version') == 2
                        else None)
        entry.setdefault('publish_date', date.today().isoformat())
        marker = 'LexRadar-' + hashlib.sha256((external + ':' + sha).encode()).hexdigest()
        account_id = entry.get('account_id')
        if account_id:
            account = self.api.get('Account', account_id)
            if (website_domain(account.get('website'))
                    != website_domain(entry.get('account_website', row['website']))
                    or account.get('name') != row['company_name']):
                raise Hold('Account binding changed')
        else:
            host = website_domain(row['website'])
            candidates = self.api.find('Account', 'website', host, operator='contains')
            candidates = [c for c in candidates
                          if website_domain(c.get('website')) == host]
            names = self.api.find('Account', 'name', row['company_name'])
            candidates = {c['id']: c for c in candidates + names}
            if len(candidates) > 1:
                raise Hold('Ambiguous Account')
            if candidates:
                account = self.api.get('Account', next(iter(candidates)))
                if (website_domain(account.get('website')) != host
                        or account.get('name') != row['company_name']):
                    raise Hold('Account name/website conflict; manual identity check required')
                account_id = account['id']
                entry['account_website'] = account['website']
            else:
                account_id = self.ensure(entry, 'account', 'Account',
                                         {'name': row['company_name'], 'website': row['website'],
                                          'description': marker, **self.contacts(row, {})}, 'description')
            entry['account_id'] = account_id
            save(self.journal, self.state)
        account = self.api.get('Account', account_id)
        patch = self.contacts(row, account)
        if patch:
            self.api.request('PUT', 'Account/' + account_id, patch)
            verified = self.api.get('Account', account_id)
            if any(verified.get(k) != v for k, v in patch.items()):
                raise Hold('Account contact verification mismatch')
        encoded = 'data:application/pdf;base64,' + base64.b64encode(data).decode()
        # Email attachments are obtained from Document/action/getAttachmentList below.
        adopted = row.get('existing_document_id')
        if adopted:
            document = self.api.get('Document', adopted)['id']
            document_file = self.api.get('Document', document).get('fileId')
        else:
            existing = self.api.request('GET', 'Account/' + account_id + '/documents')
            if not isinstance(existing.get('list'), list) or existing.get('total', len(existing['list'])) > len(existing['list']):
                raise Hold('Incomplete Account documents search')
            if 'document' not in entry['steps'] and not entry['steps'].get('document_file'):
                if existing.get('total', 0) or existing.get('list'):
                    raise Hold('Existing documents: specify verified existing_document_id before export')
            document_file = self.ensure(entry, 'document_file', 'Attachment',
                                        {'name': pdf_filename or marker + '-document.pdf', 'type': 'application/pdf',
                                         'role': 'Attachment', 'relatedType': 'Document',
                                         'field': 'file', 'file': encoded})
            document = self.ensure(entry, 'document', 'Document',
                                   {'name': marker, 'fileId': document_file,
                                    'publishDate': entry['publish_date'], 'accountsIds': [account_id]})
        document_record = self.api.get('Document', document)
        if document_record.get('fileId') != document_file or not document_record.get('fileName'):
            raise Hold('Document PDF ID/name unavailable or changed')
        pdf_name = document_record['fileName']
        if not adopted and pdf_name != (pdf_filename or marker + '-document.pdf'):
            raise Hold('Document PDF name changed')
        links = self.api.request('GET', 'Document/' + document + '/accounts')
        if (not isinstance(links.get('list'), list)
                or links.get('total', len(links['list'])) > len(links['list'])
                or {r.get('id') for r in links['list']} != {account_id}):
            raise Hold('Document Account relation not verified')
        if hashlib.sha256(self.api.file(document_file)).hexdigest() != sha:
            raise Hold('Uploaded PDF SHA-256 mismatch')
        if not row.get('email') or row.get('recipient_verified') not in (True, 'true'):
            return {'account_id': account_id, 'document_id': document,
                    'status': 'DOCUMENT_VERIFIED_NO_DRAFT',
                    'reason': 'Email missing or unverified; publication is not advertising consent'}
        expected = {**template,
                    'name': template['name'].replace('{company_name}', display_name)
                    + ('' if entry.get('presentation_version') == 2 else ' [' + marker + ']'),
                    'status': 'Draft', 'to': row['email'], 'from': SENDER,
                    'parentType': 'Account', 'parentId': account_id}
        email = self.ensure(entry, 'email', 'Email', expected)
        result = self.api.get('Email', email)
        self.verify_email(result, expected)
        names = result.get('attachmentsNames') or {}
        if not isinstance(names, dict) or not pdf_name:
            raise Hold('Email PDF attachment names unavailable')
        attachment_name = pdf_filename or pdf_name
        action = entry['steps'].get('email_attachment')
        if action and action.get('id'):
            if (action.get('sourceId') != document_file or action.get('name') != attachment_name
                    or action.get('source_name', action.get('name')) != pdf_name
                    or action.get('document_id') != document or action.get('sha256') != sha):
                raise Hold('Saved Email Attachment source/name evidence unavailable or changed')
        matching = [eid for eid, name in names.items() if name == attachment_name]
        if matching and (not action or matching != [action.get('id')]
                         or action['id'] not in (result.get('attachmentsIds') or [])):
            raise Hold('Email attachment source relation not verified')
        if not matching:
            action = entry['steps'].get('email_attachment')
            if action and action.get('id'):
                email_file = action['id']
            elif action:
                raise Hold('Uncertain POST Document/action/getAttachmentList; сверить вложения Document и Email в CRM; повтор POST заблокирован')
            else:
                entry['steps']['email_attachment'] = {'pending': True}
                save(self.journal, self.state)
                response = self.api.request('POST', 'Document/action/getAttachmentList',
                                            {'id': document, 'field': 'attachments', 'parentType': 'Email'})
                attachments = response.get('list') if isinstance(response, dict) else response
                if not isinstance(attachments, list) or any(not isinstance(a, dict) for a in attachments):
                    raise Hold('Invalid Document/action/getAttachmentList response: expected attachment list')
                selected = [a for a in attachments if a.get('sourceId') == document_file]
                if (len(selected) != 1 or not isinstance(selected[0].get('id'), str)
                        or not selected[0]['id'] or selected[0].get('name') != pdf_name):
                    raise Hold('Document/action/getAttachmentList: PDF sourceId/name missing or ambiguous')
                email_file = selected[0]['id']
                entry['steps']['email_attachment'] = {'id': email_file, 'sourceId': document_file,
                                                      'name': attachment_name, 'source_name': pdf_name,
                                                      'document_id': document, 'sha256': sha}
                save(self.journal, self.state)
            if hashlib.sha256(self.api.file(email_file)).hexdigest() != sha:
                raise Hold('Email PDF SHA-256 mismatch')
            if attachment_name != pdf_name:
                self.verify_email(self.api.get('Email', email), expected)
                if email_file == document_file:
                    raise Hold('Email attachment must be an independent copy')
                self.api.request('PUT', 'Attachment/' + email_file, {'name': attachment_name})
            self.verify_email(self.api.get('Email', email), expected)
            self.api.request('PUT', 'Email/' + email,
                             {'attachmentsIds': list(dict.fromkeys([*names, email_file]))})
            result = self.api.get('Email', email)
            self.verify_email(result, expected)
            names = result.get('attachmentsNames') or {}
            if (not isinstance(names, dict) or names.get(email_file) != attachment_name
                    or email_file not in (result.get('attachmentsIds') or [])):
                raise Hold('Email PDF attachment not verified after PUT')
            matching = [email_file]
        if any(hashlib.sha256(self.api.file(eid)).hexdigest() != sha for eid in matching):
            raise Hold('Email PDF SHA-256 mismatch')
        entry['steps']['email'] = {'id': email}
        save(self.journal, self.state)
        return {'account_id': account_id, 'document_id': document, 'email_id': email,
                'status': 'DRAFT_VERIFIED', 'advertising_send_authorized': False}

    def refresh_draft(self, external, *, confirm=False):
        """Update only journal-bound objects; never create or schedule anything."""
        entry = self.state.get(external, {})
        binding, steps = entry.get('binding', {}), entry.get('steps', {})
        email_id = steps.get('email', {}).get('id')
        action = steps.get('email_attachment', {})
        document_id = steps.get('document', {}).get('id') or action.get('document_id')
        if not (email_id and document_id and entry.get('account_id') and action.get('id')
                and entry.get('template') and entry.get('sha256') and binding.get('email')):
            raise Hold('Complete saved Draft/PDF evidence required; no creation allowed')
        email = self.api.get('Email', email_id)
        if email.get('status') != 'Draft':
            return {'status': 'SKIPPED', 'reason': 'Email is not Draft', 'email_id': email_id}
        account = self.api.get('Account', entry['account_id'])
        if (account.get('name') != binding.get('company_name')
                or website_domain(account.get('website')) != website_domain(binding.get('website'))):
            raise Hold('Account binding changed')
        document = self.api.get('Document', document_id)
        links = self.api.request('GET', 'Document/' + document_id + '/accounts')
        if (not isinstance(links.get('list'), list)
                or links.get('total', len(links['list'])) > len(links['list'])
                or {r.get('id') for r in links['list']} != {entry['account_id']}):
            raise Hold('Document Account relation not verified')
        if (action.get('document_id') != document_id or action.get('sourceId') != document.get('fileId')
                or action.get('sha256') != entry['sha256']
                or action['id'] == document.get('fileId')
                or email.get('attachmentsIds') != [action['id']]):
            raise Hold('Saved PDF/Email attachment binding changed')
        for file_id in (document['fileId'], action['id']):
            if hashlib.sha256(self.api.file(file_id)).hexdigest() != entry['sha256']:
                raise Hold('PDF SHA-256 mismatch')
        template = template_at(self.api, None)
        if entry['template'] != template:
            raise Hold('Commercial PDF content refresh required: rename-only refresh cannot replace PDF bytes; '
                       'regenerate PDF and reconcile Document/Email file IDs and SHA-256 before applying')
        clinic = entry.get('presentation_name') or binding['company_name']
        filename = proposal_filename(clinic)
        target = {**template, 'name': template['name'].replace('{company_name}', clinic)}
        if len(target['name']) > 179:
            raise Hold('Personalized subject too long')
        marker = 'LexRadar-' + hashlib.sha256((external + ':' + entry['sha256']).encode()).hexdigest()
        old = {**entry['template'], 'name': entry['template']['name'].replace('{company_name}', clinic)
               + ('' if entry.get('presentation_version') == 2 else ' [' + marker + ']')}
        update = entry.get('presentation_update')
        if update:
            if update['target'] != target or update['filename'] != filename:
                raise Hold('Presentation update target changed')
            old = update['old_email']
        else:
            update = {'old_email': old, 'target': target, 'filename': filename,
                      'document_name': document.get('fileName'), 'attachment_name': action.get('name')}
        fixed = {'status': 'Draft', 'to': binding['email'], 'from': SENDER,
                 'parentType': 'Account', 'parentId': entry['account_id']}

        def check_draft():
            current = self.api.get('Email', email_id)
            if (any(current.get(k) != v for k, v in fixed.items())
                    or current.get('sendAt') or current.get('dateSent')
                    or current.get('cc') or current.get('bcc')
                    or current.get('attachmentsIds') != [action['id']]):
                raise Hold('Email is sent, scheduled or binding changed; update blocked')
            for key in target:
                allowed = ((target[key],) if update.get('completed') else
                           (old[key], target[key]) if entry.get('presentation_update') else (old[key],))
                if current.get(key) not in allowed:
                    raise Hold('Draft was edited; presentation update blocked')
            if current.get('subject') not in (None, current.get('name')):
                raise Hold('Email subject/name mismatch')
            return current

        current = check_draft()
        names = current.get('attachmentsNames') or {}
        if (document.get('fileName') not in (update['document_name'], filename)
                or names.get(action['id']) not in (update['attachment_name'], filename)):
            raise Hold('PDF name changed outside saved presentation update')
        if not entry.get('presentation_update') and names.get(action['id']) != action.get('name'):
            raise Hold('Saved attachment name mismatch')
        if update.get('completed'):
            if document.get('fileName') != filename or names.get(action['id']) != filename:
                raise Hold('Completed PDF presentation changed')
            return {'status': 'DRAFT_VERIFIED', 'email_id': email_id, 'document_id': document_id,
                    'account_id': entry['account_id'], 'advertising_send_authorized': False}
        if not confirm:
            return {'status': 'PREVIEW', 'email_id': email_id, 'name': target['name'], 'filename': filename}
        entry['presentation_update'] = update
        save(self.journal, self.state)  # Keep the original copy and names before the first PUT.
        for file_id, current_name in ((document['fileId'], document.get('fileName')),
                                      (action['id'], names.get(action['id']))):
            if current_name != filename:
                check_draft()
                self.api.request('PUT', 'Attachment/' + file_id, {'name': filename})
        check_draft()
        patch = dict(target)
        if email.get('subject') is not None:
            patch['subject'] = target['name']
        self.api.request('PUT', 'Email/' + email_id, patch)
        verified = self.api.get('Email', email_id)
        self.verify_email(verified, {**target, **fixed})
        if (verified.get('attachmentsIds') != [action['id']]
                or (verified.get('attachmentsNames') or {}).get(action['id']) != filename
                or self.api.get('Document', document_id).get('fileName') != filename):
            raise Hold('PDF rename not verified; resume saved update, do not recreate objects')
        for file_id in (document['fileId'], action['id']):
            if hashlib.sha256(self.api.file(file_id)).hexdigest() != entry['sha256']:
                raise Hold('PDF SHA-256 mismatch after rename')
        action['name'] = filename
        action['source_name'] = filename
        entry.update(template=template, presentation_version=2, presentation_name=clinic)
        update['completed'] = True
        save(self.journal, self.state)
        return {'status': 'DRAFT_VERIFIED', 'email_id': email_id, 'document_id': document_id,
                'account_id': entry['account_id'], 'advertising_send_authorized': False}

    @staticmethod
    def verify_email(result, expected):
        if any(result.get(key) != value for key, value in expected.items()):
            raise Hold('Email Draft fields/body verification mismatch')
        if not isinstance(result.get('body'), str) or not result['body'].strip():
            raise Hold('Email Draft body is empty')
        if result.get('sendAt') or result.get('cc') or result.get('bcc'):
            raise Hold('Email scheduled or has extra recipients')


def check_schema(api):
    metadata = api.request('GET', 'Metadata')
    definitions = metadata.get('entityDefs', {})
    required = {
        'Account': ('name', 'website', 'description'),
        'Attachment': ('name', 'type', 'role', 'related', 'parent', 'field'),
        'Document': ('name', 'file', 'publishDate', 'accounts'),
        'Email': ('name', 'body', 'isHtml', 'status', 'to', 'from',
                  'parent', 'attachments'),
    }
    for kind, fields in required.items():
        actual = definitions.get(kind, {}).get('fields', {})
        if any(field not in actual for field in fields):
            raise Hold('CRM schema unsupported/unavailable: ' + kind)
    statuses = definitions['Email']['fields']['status'].get('options', [])
    if 'Draft' not in statuses:
        raise Hold('CRM schema does not confirm Draft status')

    return definitions


def inspect_sample(api, output):
    """GET only; keep real correspondence outside versioned examples."""
    email = api.get('Email', '6ae1e6ebf0fd5a377')
    account = api.get('Account', '6ae1d3a08e4e3972a')
    document = api.get('Document', '6ae1d8069a608b987')
    if email.get('parentType') != 'Account' or email.get('parentId') != account['id']:
        raise Hold('Sample Email/Account binding mismatch')
    template = {k: email.get(k) for k in ('name', 'body', 'isHtml')}
    files = {}
    ids = [document.get('fileId'), *(email.get('attachmentsIds') or [])]
    if not document.get('fileId') or not email.get('attachmentsIds'):
        raise Hold('Sample Document/Email PDF attachment missing')
    for eid in ids:
        metadata = api.get('Attachment', eid)
        data = api.file(eid)
        if metadata.get('type') != 'application/pdf' or not data.startswith(b'%PDF-') or len(data) > MAX_PDF:
            raise Hold('Sample attachment is not a supported PDF')
        files[eid] = {'metadata': metadata, 'sha256': hashlib.sha256(data).hexdigest(),
                      'bytes': len(data)}
    # Private local snapshot may contain the original recipient; never print it.
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    save(output / 'sample.json', {'email': email, 'account': account,
                                 'document': document, 'files': files})
    save(output / 'template.json', template)
    template_at(api, output / 'template.json')
    check_schema(api)
    return {'status': 'READ_ONLY_VERIFIED', 'pdf_count': len(files),
            'upload_verified': False}


def run(rows, root, journal, *, api=None, confirm=False, template_path=None):
    results = []
    counts = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get('id'), str):
            counts[row['id']] = counts.get(row['id'], 0) + 1
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.with_suffix(journal.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(journal.read_text()) if journal.exists() else {}
        template = None
        for row in rows:
            result = {'id': row.get('id') if isinstance(row, dict) else None}
            try:
                data, sha = validate(row, root)
                result['sha256'] = sha
                if counts.get(row['id'], 0) != 1:
                    raise Hold('Duplicate manifest id')
                if row['id'] in state and state[row['id']]['sha256'] != sha:
                    raise Hold('Stable ID already bound to different PDF')
                if not confirm:
                    result.update(status='PREVIEW', reason='Local validation passed; CRM/template unchecked')
                else:
                    if api is None:
                        api = API()
                    if template is None:
                        definitions = check_schema(api)
                        template = template_at(api, template_path)
                    result.update(Exporter(api, journal, state, definitions).export(row, data, sha, template))

            except Hold as exc:
                result.update(status='HOLD', reason=str(exc))
            except Exception:
                result.update(status='HOLD', reason='Local/API failure; inspect journal before retry')
            results.append(result)
    return results


def refresh_drafts(journal, ids, *, confirm=False, api=None):
    results = []
    with journal.with_suffix(journal.suffix + '.lock').open('a' if confirm else 'r') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(journal.read_text())
        api = api or API()
        worker = Exporter(api, journal, state, check_schema(api))
        for external in dict.fromkeys(ids):
            try:
                result = worker.refresh_draft(external, confirm=confirm)
            except Hold as exc:
                result = {'status': 'HOLD', 'reason': str(exc)}
            except Exception:
                result = {'status': 'HOLD', 'reason': 'Local/API failure; resume saved update after reconciliation'}
            results.append({'id': external, **result})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path, nargs='?')
    parser.add_argument('--check-connection', type=Path, metavar='PRIVATE_OUTPUT_DIR',
                        help='GET sample, PDF bytes and schema; save private snapshot/template, no POST')
    parser.add_argument('--refresh-draft', action='append', default=[], metavar='JOURNAL_ID',
                        help='Preview presentation update of an existing journal-bound Draft; repeat for several IDs')
    parser.add_argument('--confirm-refresh-drafts', action='store_true',
                        help='Apply --refresh-draft using PUT only; never create or send')
    parser.add_argument('--confirm-create-drafts', action='store_true')
    parser.add_argument('--template', type=Path, help='Approved JSON: name, body, isHtml')
    parser.add_argument('--journal', type=Path,
                        default=Path.home() / '.local/state/lexradar/espo-export.json')
    args = parser.parse_args(argv)
    if args.refresh_draft or args.confirm_refresh_drafts:
        if (not args.refresh_draft or args.manifest or args.confirm_create_drafts
                or args.check_connection or args.template):
            parser.error('--refresh-draft requires journal IDs and cannot be combined with export arguments')
        try:
            results = refresh_drafts(args.journal, args.refresh_draft, confirm=args.confirm_refresh_drafts)
        except Exception:
            parser.exit(2, 'HOLD: journal/API unavailable; no blind retry\n')
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 1 if any(r['status'] == 'HOLD' for r in results) else 0
    if args.check_connection:
        if args.manifest or args.confirm_create_drafts or args.template:
            parser.error('--check-connection cannot be combined with export arguments')
        try:
            print(json.dumps(inspect_sample(API(), args.check_connection), indent=2))
            return 0
        except Hold as exc:
            print(json.dumps({'status': 'HOLD', 'reason': str(exc)}))
            return 1
        except Exception:
            print(json.dumps({'status': 'HOLD', 'reason': 'Read-only check or local save failed'}))
            return 1
    if args.manifest is None:
        parser.error('manifest required for preview/export')
    try:
        results = run(read_manifest(args.manifest), args.manifest.resolve().parent, args.journal,
                      confirm=args.confirm_create_drafts, template_path=args.template)
    except Exception:
        parser.exit(2, 'HOLD: manifest/journal unavailable or invalid; no blind retry\n')
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if any(r['status'] == 'HOLD' for r in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
