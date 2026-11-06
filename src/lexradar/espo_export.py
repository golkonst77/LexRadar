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
            raise Hold(f'CRM HTTP {exc.code}: {method} {route.split("?")[0]}; inspect CRM before retry') from None
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
        self.get('Attachment', eid)
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
        if saved and saved.get('id'):
            record = self.api.get(kind, saved['id'])
        else:
            found = self.api.find(kind, field, payload[field])
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
        if len(template['name'].replace('{company_name}', row['company_name'])) > 179:
            raise Hold('Personalized subject too long')
        external = row['id']
        entry = self.state.setdefault(external, {'sha256': sha, 'steps': {}})
        binding = {k: row.get(k, '') for k in ('company_name', 'website', 'email')}
        if entry['sha256'] != sha or entry.get('binding', binding) != binding:
            raise Hold('Stable ID already bound to different PDF or recipient/company')
        if entry.get('template', template) != template:
            raise Hold('Template changed for existing export')
        entry.update(binding=binding, template=template)
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
                if host == 'aclinic-krd.ru':
                    raise Hold('Known manual clinic Account not found; no duplicate will be created')
                account_id = self.ensure(entry, 'account', 'Account',
                                         {'name': row['company_name'], 'website': row['website'],
                                          'description': marker, **self.contacts(row, {})}, 'description')
            entry['account_id'] = account_id
            save(self.journal, self.state)
        if website_domain(row['website']) == 'aclinic-krd.ru' and account_id != '6ae32c46426f8cdb3':
            raise Hold('Manual clinic Account ID mismatch')
        account = self.api.get('Account', account_id)
        patch = self.contacts(row, account)
        # The proven manual clinic result is read-only for this exporter.
        if account_id == '6ae32c46426f8cdb3':
            numbers = phones(row)
            if ((row.get('email') and account.get('emailAddress') != row['email'])
                    or (numbers and re.sub(r'\D', '', account.get('phoneNumber') or '')
                        != re.sub(r'\D', '', numbers[0]))):
                raise Hold('Manual clinic primary contacts differ; reconcile explicitly, no update performed')
            patch = {}
        if patch:
            self.api.request('PUT', 'Account/' + account_id, patch)
            verified = self.api.get('Account', account_id)
            if any(verified.get(k) != v for k, v in patch.items()):
                raise Hold('Account contact verification mismatch')
        encoded = 'data:application/pdf;base64,' + base64.b64encode(data).decode()
        # Email attachments are obtained from Document/action/getAttachmentList below.
        manual = website_domain(row['website']) == 'aclinic-krd.ru'
        adopted = row.get('existing_document_id') or ('6ae32e009a0f5cc31' if manual else None)
        if adopted:
            document = self.api.get('Document', adopted)['id']
            document_file = self.api.get('Document', document).get('fileId')
            if manual and (account_id != '6ae32c46426f8cdb3' or document_file != '6ae32da53033facf1'):
                raise Hold('Manual clinic identity mismatch')
        else:
            existing = self.api.request('GET', 'Account/' + account_id + '/documents')
            if not isinstance(existing.get('list'), list) or existing.get('total', len(existing['list'])) > len(existing['list']):
                raise Hold('Incomplete Account documents search')
            if 'document' not in entry['steps'] and not entry['steps'].get('document_file'):
                if existing.get('total', 0) or existing.get('list'):
                    raise Hold('Existing documents: specify verified existing_document_id before export')
            document_file = self.ensure(entry, 'document_file', 'Attachment',
                                        {'name': marker + '-document.pdf', 'type': 'application/pdf',
                                         'role': 'Attachment', 'relatedType': 'Document',
                                         'field': 'file', 'file': encoded})
            document = self.ensure(entry, 'document', 'Document',
                                   {'name': marker, 'fileId': document_file,
                                    'publishDate': entry['publish_date'], 'accountsIds': [account_id]})
        links = self.api.request('GET', 'Document/' + document + '/accounts')
        if account_id not in [r.get('id') for r in links.get('list', [])]:
            raise Hold('Document Account relation not verified')
        if hashlib.sha256(self.api.file(document_file)).hexdigest() != sha:
            raise Hold('Uploaded PDF SHA-256 mismatch')
        if not row.get('email') or row.get('recipient_verified') not in (True, 'true'):
            return {'account_id': account_id, 'document_id': document,
                    'status': 'DOCUMENT_VERIFIED_NO_DRAFT',
                    'reason': 'Email missing or unverified; publication is not advertising consent'}
        expected = {**template,
                    'name': template['name'].replace('{company_name}', row['company_name']) + ' [' + marker + ']',
                    'status': 'Draft', 'to': row['email'], 'from': SENDER,
                    'parentType': 'Account', 'parentId': account_id}
        if manual:
            # The control draft was created manually; adopt it without changing its subject.
            email = '6ae3374166673d4cf'
            result = self.api.get('Email', email)
            expected['name'] = result.get('name')
        else:
            email = self.ensure(entry, 'email', 'Email', expected)
            result = self.api.get('Email', email)
        # Only the known manual draft may have its body reconciled after PDF verification.
        self.verify_email({**result, 'body': expected['body']} if manual else result, expected)
        pdf_name = self.api.get('Attachment', document_file).get('name')
        names = result.get('attachmentsNames') or {}
        if not isinstance(names, dict) or not pdf_name:
            raise Hold('Email PDF attachment names unavailable')
        matching = [eid for eid, name in names.items() if name == pdf_name]
        if not matching:
            if manual:
                raise Hold('Manual clinic draft PDF missing; no update performed')
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
            if hashlib.sha256(self.api.file(email_file)).hexdigest() != sha:
                raise Hold('Email PDF SHA-256 mismatch')
            self.api.request('PUT', 'Email/' + email,
                             {'attachmentsIds': list(dict.fromkeys([*names, email_file]))})
            result = self.api.get('Email', email)
            self.verify_email(result, expected)
            names = result.get('attachmentsNames') or {}
            if not isinstance(names, dict) or names.get(email_file) != pdf_name:
                raise Hold('Email PDF attachment not verified after PUT')
            matching = [email_file]
        if any(hashlib.sha256(self.api.file(eid)).hexdigest() != sha for eid in matching):
            raise Hold('Email PDF SHA-256 mismatch')
        if manual and result.get('body') != expected['body']:
            current = self.api.get('Email', email)
            self.verify_email({**current, 'body': expected['body']}, expected)
            if current.get('attachmentsNames') != names or current.get('attachmentsIds') != result.get('attachmentsIds'):
                raise Hold('Manual clinic draft attachments changed; no update performed')
            self.api.request('PUT', 'Email/' + email, {'body': expected['body']})
            result = self.api.get('Email', email)
            self.verify_email(result, expected)
            if result.get('attachmentsNames') != names or result.get('attachmentsIds') != current.get('attachmentsIds'):
                raise Hold('Manual clinic draft attachments changed after body PUT')
            if any(hashlib.sha256(self.api.file(eid)).hexdigest() != sha for eid in matching):
                raise Hold('Email PDF SHA-256 mismatch')
        entry['steps']['email'] = {'id': email}
        save(self.journal, self.state)
        return {'account_id': account_id, 'document_id': document, 'email_id': email,
                'status': 'DRAFT_VERIFIED', 'advertising_send_authorized': False}

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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path, nargs='?')
    parser.add_argument('--check-connection', type=Path, metavar='PRIVATE_OUTPUT_DIR',
                        help='GET sample, PDF bytes and schema; save private snapshot/template, no POST')
    parser.add_argument('--confirm-create-drafts', action='store_true')
    parser.add_argument('--template', type=Path, help='Approved JSON: name, body, isHtml')
    parser.add_argument('--journal', type=Path,
                        default=Path.home() / '.local/state/lexradar/espo-export.json')
    args = parser.parse_args(argv)
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
