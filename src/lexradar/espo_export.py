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
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_URL = 'https://crm.example.invalid/api/v1/'
SENDER = 'sender@example.invalid'
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
                return response.read(MAX_PDF + 1) if binary else json.load(response)
        except Exception:
            # Never expose response bodies, request headers or exception text containing secrets.
            raise Hold('API unavailable or invalid response; inspect CRM before retry') from None

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
        if any(not r.get('id') or (operator == 'equals' and r.get(field) != value)
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
    for field in ('id', 'company_name', 'website', 'email', 'pdf_path'):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise Hold('Missing ' + field)
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', row['id']):
        raise Hold('Invalid stable id')
    for field in ('recipient_verified', 'export_approved'):
        if row.get(field) is not True and row.get(field) != 'true':
            raise Hold(field + ' must be true')
    email = row['email']
    if not ADDRESS.fullmatch(email) or len(email) > 254 or len(email.split('@')[0]) > 64:
        raise Hold('Invalid single email')
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


def template_at(api, path):
    if path:
        template = json.loads(path.read_text())
    else:
        source = api.get('Email', 'example-email')
        template = {k: source.get(k) for k in ('name', 'body', 'isHtml')}
        # The operator must supply an adapted template if the sample is company-specific.
    if (not isinstance(template, dict) or not isinstance(template.get('name'), str)
            or not template['name'].strip() or not isinstance(template.get('body'), str)
            or not template['body'].strip() or type(template.get('isHtml')) is not bool):
        raise Hold('Fill template JSON: name, body, isHtml; sample unavailable/empty')
    if any(x in template['name'] + template['body'] for x in ('FILL_FROM_APPROVED_SAMPLE',)):
        raise Hold('Template must be filled from approved sample')
    if len(template['name']) > 179:
        raise Hold('Approved subject too long for CRM recovery reference (maximum 179 characters)')
    return {k: template[k] for k in ('name', 'body', 'isHtml')}


class Exporter:
    def __init__(self, api, journal, state):
        self.api, self.journal, self.state = api, journal, state

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
                except Exception:
                    found = self.api.find(kind, field, payload[field])
                    if len(found) != 1:
                        raise Hold('Uncertain POST ' + step + '; manual reconciliation required') from None
                    record = self.api.get(kind, found[0]['id'])
        for key, value in payload.items():
            if key == 'file':
                continue
            if record.get(key) != value:
                raise Hold('CRM verification mismatch: ' + step + '/' + key)
        steps[step] = {'id': record['id']}
        save(self.journal, self.state)
        return record['id']

    def export(self, row, data, sha, template):
        external = row['id']
        entry = self.state.setdefault(external, {'sha256': sha, 'steps': {}})
        binding = {k: row[k] for k in ('company_name', 'website', 'email')}
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
            if (account.get('website') != entry.get('account_website', row['website'])
                    or account.get('name') != row['company_name']):
                raise Hold('Account binding changed')
        else:
            host = urlsplit(row['website']).hostname.lower().removeprefix('www.')
            candidates = self.api.find('Account', 'website', host, operator='contains')
            candidates = [c for c in candidates
                          if urlsplit(c.get('website') or '').hostname
                          and urlsplit(c['website']).hostname.lower().removeprefix('www.') == host]
            names = self.api.find('Account', 'name', row['company_name'])
            candidates = {c['id']: c for c in candidates + names}
            if len(candidates) > 1:
                raise Hold('Ambiguous Account')
            if candidates:
                account = self.api.get('Account', next(iter(candidates)))
                if (urlsplit(account.get('website') or '').hostname is None
                        or urlsplit(account['website']).hostname.lower().removeprefix('www.') != host
                        or account.get('name') != row['company_name']):
                    raise Hold('Account name/website conflict; manual identity check required')
                account_id = account['id']
                entry['account_website'] = account['website']
            else:
                account_id = self.ensure(entry, 'account', 'Account',
                                         {'name': row['company_name'], 'website': row['website'],
                                          'description': marker}, 'description')
            entry['account_id'] = account_id
            save(self.journal, self.state)
        encoded = 'data:application/pdf;base64,' + base64.b64encode(data).decode()
        # Distinct Attachment records prevent Document and Email from stealing file ownership.
        document_file = self.ensure(entry, 'document_file', 'Attachment',
                                    {'name': marker + '-document.pdf', 'type': 'application/pdf',
                                     'role': 'Attachment', 'relatedType': 'Document',
                                     'field': 'file', 'file': encoded})
        document = self.ensure(entry, 'document', 'Document',
                               {'name': marker, 'fileId': document_file,
                                'publishDate': entry['publish_date']})
        email_file = self.ensure(entry, 'email_file', 'Attachment',
                                 {'name': marker + '-email.pdf', 'type': 'application/pdf',
                                  'role': 'Attachment', 'parentType': 'Email',
                                  'field': 'attachments', 'file': encoded})
        # A visible reference survives CRM HTML sanitization and does not alter approved copy.
        email = self.ensure(entry, 'email', 'Email',
                            {**template, 'name': template['name'] + ' [' + marker + ']',
                             'status': 'Draft', 'to': row['email'], 'from': SENDER,
                             'parentType': 'Account', 'parentId': account_id,
                             'attachmentsIds': [email_file]})
        for attachment in (document_file, email_file):
            if hashlib.sha256(self.api.file(attachment)).hexdigest() != sha:
                raise Hold('Uploaded PDF SHA-256 mismatch')
        result = self.api.get('Email', email)
        if result.get('sendAt') or result.get('cc') or result.get('bcc'):
            raise Hold('Email scheduled or has extra recipients')
        return {'account_id': account_id, 'document_id': document, 'email_id': email}


def check_schema(api):
    metadata = api.request('GET', 'Metadata')
    definitions = metadata.get('entityDefs', {})
    required = {
        'Account': ('name', 'website', 'description'),
        'Attachment': ('name', 'type', 'role', 'related', 'parent', 'field'),
        'Document': ('name', 'file', 'publishDate'),
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


def inspect_sample(api, output):
    """GET only; keep real correspondence outside versioned examples."""
    email = api.get('Email', 'example-email')
    account = api.get('Account', 'example-account')
    document = api.get('Document', 'example-document')
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
                        check_schema(api)
                        template = template_at(api, template_path)
                    result.update(Exporter(api, journal, state).export(row, data, sha, template))
                    result['status'] = 'DRAFT_VERIFIED'
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
