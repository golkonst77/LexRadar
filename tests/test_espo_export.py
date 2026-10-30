"""No live CRM calls; exercise durable intent and real local journal files."""
import base64
import copy
import csv
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from lexradar.espo_export import APPROVED_TEMPLATE, API, Hold, check_schema, inspect_sample, run


class FakeAPI:
    def __init__(self):
        self.records = {k: {} for k in ('Account', 'Attachment', 'Document', 'Email')}
        self.posts = []
        self.calls = []
        self.lose = None
        self.fail = None
        self.hide = False

    def get(self, kind, eid):
        if eid not in self.records[kind]:
            raise Hold('Saved CRM record missing')
        return copy.deepcopy(self.records[kind][eid])

    def find(self, kind, field, value, *, operator='equals'):
        if self.hide:
            return []
        return [copy.deepcopy(r) for r in self.records[kind].values() if (value in (r.get(field) or '') if operator == 'contains'
                                              else r.get(field) == value)]

    def file(self, eid):
        return base64.b64decode(self.records['Attachment'][eid]['file'].split(',', 1)[1])

    def request(self, method, route, payload=None):
        self.calls.append((method, route, copy.deepcopy(payload)))
        if route == 'Document/action/getAttachmentList':
            assert method == 'POST'
            assert payload['field'] == 'attachments' and payload['parentType'] == 'Email'
            source = self.records['Document'][payload['id']]['fileId']
            eid = 'copy-' + source
            self.records['Attachment'][eid] = {
                **copy.deepcopy(self.records['Attachment'][source]), 'id': eid, 'sourceId': source}
            return [copy.deepcopy(self.records['Attachment'][eid])]
        if method == 'GET' and route == 'Metadata':
            required = {
                'Account': ('name', 'website', 'description', 'emailAddress', 'phoneNumber', 'cSegment'),
                'Attachment': ('name', 'type', 'role', 'related', 'parent', 'field'),
                'Document': ('name', 'file', 'publishDate', 'accounts'),
                'Email': ('name', 'body', 'isHtml', 'status', 'to', 'from',
                          'parent', 'attachments'),
            }
            data = {'entityDefs': {k: {'fields': {f: {} for f in fields}}
                                   for k, fields in required.items()}}
            data['entityDefs']['Account']['fields']['emailAddress'] = {'type': 'email'}
            data['entityDefs']['Account']['fields']['phoneNumber'] = {'type': 'phone'}
            data['entityDefs']['Account']['fields']['cSegment'] = {'type': 'enum', 'options': ['Медицинские организации', 'Не определено']}
            data['entityDefs']['Email']['fields']['status']['options'] = ['Draft', 'Sent']
            return data
        if method == 'GET' and route.endswith('/documents'):
            return {'list': [], 'total': 0}
        if method == 'GET' and route.endswith('/accounts'):
            doc = self.records['Document'][route.split('/')[1]]
            return {'list': [{'id': i} for i in doc.get('accountsIds', [])]}
        if method == 'PUT':
            kind, eid = route.split('/')
            self.records[kind][eid].update(copy.deepcopy(payload))
            if kind == 'Email' and 'attachmentsIds' in payload:
                self.records[kind][eid]['attachmentsNames'] = {
                    i: self.records['Attachment'][i]['name'] for i in payload['attachmentsIds']}
            return self.get(kind, eid)
        if method != 'POST':
            raise AssertionError('Unexpected mutation: ' + method + ' ' + route)
        if payload.get('name') == self.fail:
            raise Hold('Injected failure')
        eid = 'id' + str(len(self.posts) + 1)
        self.posts.append((route, copy.deepcopy(payload)))
        self.records[route][eid] = {**copy.deepcopy(payload), 'id': eid}
        if route == 'Document':
            self.records[route][eid]['fileName'] = self.records['Attachment'][payload['fileId']]['name']
        if route == self.lose:
            self.lose = None
            raise Hold('Lost response after commit')
        return {'id': eid}


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = self.root / 'journal.json'
        (self.root / 'proposal.pdf').write_bytes(b'%PDF-1.4\nsynthetic fixture\n%%EOF')
        self.template = self.root / 'template.json'
        self.template.write_text(json.dumps({'name': 'Approved subject', 'body': 'Approved body',
                                             'isHtml': False}))
        self.row = dict(id='approved-1', company_name='Fictional Company',
                        website='https://example.invalid', email='a@example.invalid',
                        pdf_path='proposal.pdf', recipient_verified=True, export_approved=True)
        self.api = FakeAPI()

    def export(self, rows=None, confirm=True):
        return run(rows or [self.row], self.root, self.journal, api=self.api,
                   confirm=confirm, template_path=self.template)

    def test_read_only_sample_checks_actual_pdf_and_saves_private_template(self):
        self.api.records['Account']['6ae1d3a08e4e3972a'] = {'id': '6ae1d3a08e4e3972a'}
        self.api.records['Email']['6ae1e6ebf0fd5a377'] = {
            'id': '6ae1e6ebf0fd5a377', 'parentType': 'Account',
            'parentId': '6ae1d3a08e4e3972a', 'attachmentsIds': ['pdf'],
            'name': 'Original subject', 'body': 'Original copy', 'isHtml': False,
            'to': 'private@example.invalid'}
        self.api.records['Document']['6ae1d8069a608b987'] = {
            'id': '6ae1d8069a608b987', 'fileId': 'pdf'}
        self.api.records['Attachment']['pdf'] = {
            'id': 'pdf', 'type': 'application/pdf',
            'file': 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-fixture').decode()}
        output = self.root / 'private'
        result = inspect_sample(self.api, output)
        self.assertEqual(result['status'], 'READ_ONLY_VERIFIED')
        self.assertFalse(result['upload_verified'])
        self.assertEqual(json.loads((output / 'template.json').read_text()),
                         {'name': 'Original subject', 'body': 'Original copy', 'isHtml': False})
        self.assertEqual((output / 'template.json').stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.api.posts)
        self.api.file = lambda eid: b'not a PDF'
        with self.assertRaises(Hold):
            inspect_sample(self.api, output)
        self.assertFalse(self.api.posts)

    def test_preview_no_crm_mutations(self):
        def forbidden(*args, **kwargs):
            raise AssertionError('Preview contacted CRM')

        self.api.request = self.api.get = self.api.find = forbidden
        result = self.export(confirm=False)
        self.assertEqual(result[0]['status'], 'PREVIEW')
        self.assertEqual(self.api.posts, [])
        self.assertFalse(self.journal.exists())

    def test_success_and_repeat_without_duplicates(self):
        first = self.export()[0]
        self.assertEqual(first['status'], 'DRAFT_VERIFIED')
        self.assertEqual(len(self.api.posts), 4)
        second = self.export()[0]
        self.assertEqual(first, second)
        self.assertEqual(len(self.api.posts), 4)
        email = self.api.get('Email', first['email_id'])
        self.assertEqual(email['status'], 'Draft')
        self.assertEqual(email['from'], 'golwebstudio@bk.ru')
        self.assertEqual(email['parentId'], first['account_id'])
        self.assertNotIn('sendAt', email)
        self.assertTrue(email['name'].startswith('Approved subject [LexRadar-'))
        self.assertEqual(email['body'], 'Approved body')
        self.assertNotIn('messageId', email)

    def test_export_without_attachment_metadata_permissions(self):
        get, find = self.api.get, self.api.find
        def denied_get(kind, eid):
            if kind == 'Attachment':
                raise AssertionError('Forbidden GET Attachment')
            return get(kind, eid)
        def denied_find(kind, *args, **kwargs):
            if kind == 'Attachment':
                raise AssertionError('Forbidden GET Attachment search')
            return find(kind, *args, **kwargs)
        self.api.get, self.api.find = denied_get, denied_find
        first = self.export()[0]
        self.assertEqual(first['status'], 'DRAFT_VERIFIED')
        self.assertEqual(self.export()[0], first)

    def test_pdf_download_uses_bytes_without_metadata_get(self):
        api = API.__new__(API)
        with patch.object(api, 'request', return_value=b'%PDF-fixture') as request:
            self.assertEqual(api.file('file-id'), b'%PDF-fixture')
            request.assert_called_once_with('GET', 'Attachment/file/file-id', binary=True)
        with self.assertRaises(Hold):
            api.file('../invalid')

    def test_unreadable_pdf_never_verified(self):
        def denied(eid):
            raise Hold('PDF bytes unavailable')
        self.api.file = denied
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        self.assertFalse(self.api.records['Email'])
        self.api.file = lambda eid: b'%PDF-different bytes'
        result = self.export()[0]
        self.assertEqual(result['status'], 'HOLD')
        self.assertIn('SHA-256 mismatch', result['reason'])
        self.assertFalse(self.api.records['Email'])

    def test_unknown_attachment_upload_is_not_reposted(self):
        self.api.lose = 'Attachment'
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        count = len(self.api.posts)
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        self.assertEqual(len(self.api.posts), count)

    def test_document_action_and_email_put_then_readback(self):
        result = self.export()[0]
        self.assertEqual(result['status'], 'DRAFT_VERIFIED')
        calls = self.api.calls
        action = next(i for i, c in enumerate(calls) if c[1] == 'Document/action/getAttachmentList')
        self.assertEqual(calls[action][2], {'id': result['document_id'],
                                          'field': 'attachments', 'parentType': 'Email'})
        self.assertEqual(calls[action + 1][0:2], ('PUT', 'Email/' + result['email_id']))
        self.assertFalse(any(route == 'Attachment' and payload.get('parentType') == 'Email'
                             for route, payload in self.api.posts))
        before = copy.deepcopy(self.api.records)
        count = len(calls)
        self.assertEqual(self.export()[0], result)
        self.assertEqual(self.api.records, before)
        self.assertFalse(any(method in ('POST', 'PUT') for method, _, _ in calls[count:]))

    def test_attachment_action_invalid_responses_hold_without_duplicate_email(self):
        for response in (None, {}, {'list': 'invalid'}, [None], [],
                         [{'id': 'wrong', 'sourceId': 'wrong', 'name': 'proposal.pdf'}]):
            with self.subTest(response=response):
                self.journal.unlink(missing_ok=True)
                self.api = FakeAPI()
                original = self.api.request
                def request(method, route, payload=None):
                    if route == 'Document/action/getAttachmentList':
                        return response
                    return original(method, route, payload)
                self.api.request = request
                for _ in range(2):
                    self.assertEqual(self.export()[0]['status'], 'HOLD')
                self.assertEqual(len(self.api.records['Email']), 1)

    def test_attachment_action_lost_response_is_not_reposted(self):
        original = self.api.request
        def request(method, route, payload=None):
            response = original(method, route, payload)
            if route == 'Document/action/getAttachmentList':
                raise Hold('Lost attachment action response')
            return response
        self.api.request = request
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        count = len(self.api.calls)
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        self.assertFalse(any(m in ('POST', 'PUT') for m, _, _ in self.api.calls[count:]))
        self.assertEqual(len(self.api.records['Email']), 1)

    def test_saved_action_attachment_reused_after_failed_put(self):
        original = self.api.request
        def request(method, route, payload=None):
            if method == 'PUT' and route.startswith('Email/'):
                raise Hold('PUT failed before response')
            return original(method, route, payload)
        self.api.request = request
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        count = len(self.api.calls)
        self.api.request = original
        self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')
        self.assertFalse(any(m == 'POST' for m, _, _ in self.api.calls[count:]))

    def test_lost_attachment_put_response_reuses_completed_draft(self):
        original = self.api.request
        def request(method, route, payload=None):
            result = original(method, route, payload)
            if method == 'PUT' and route.startswith('Email/'):
                raise Hold('CRM HTTP 503: PUT Email; response lost')
            return result
        self.api.request = request
        result = self.export()[0]
        self.assertIn('HTTP 503', result['reason'])
        count = len(self.api.calls)
        self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')
        self.assertEqual(len(self.api.records['Email']), 1)
        self.assertFalse(any(method in ('POST', 'PUT') for method, _, _ in self.api.calls[count:]))

    def test_email_put_requires_readback_body_and_attachment_names(self):
        for field, value in (('body', ''), ('attachmentsNames', {}), ('status', 'Sent'),
                             ('from', ''), ('to', ''), ('parentType', 'Contact'),
                             ('parentId', 'wrong'), ('sendAt', '2030-01-01 09:00:00')):
            with self.subTest(field=field):
                self.journal.unlink(missing_ok=True)
                self.api = FakeAPI()
                original = self.api.request
                def request(method, route, payload=None):
                    response = original(method, route, payload)
                    if method == 'PUT' and route.startswith('Email/'):
                        self.api.records['Email'][route.split('/')[1]][field] = value
                    return response
                self.api.request = request
                self.assertEqual(self.export()[0]['status'], 'HOLD')
                self.assertEqual(len(self.api.records['Email']), 1)

    def test_attachment_action_wrapped_list(self):
        original = self.api.request
        def request(method, route, payload=None):
            response = original(method, route, payload)
            return {'list': response} if route == 'Document/action/getAttachmentList' else response
        self.api.request = request
        self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')

    def manual_draft(self):
        self.row.update(company_name='Клиника А', website='https://aclinic-krd.ru/',
                        phones=['+78619922327', '+79189381920'])
        account = '6ae32c46426f8cdb3'
        document = '6ae32e009a0f5cc31'
        email = '6ae3374166673d4cf'
        source = '6ae32da53033facf1'
        self.api.records['Account'][account] = dict(
            id=account, name=self.row['company_name'], website='aclinic-krd.ru',
            emailAddress=self.row['email'], phoneNumber='+7 (861) 992-23-27')
        self.api.records['Document'][document] = dict(id=document, fileId=source, accountsIds=[account])
        for eid in (source, 'email-pdf'):
            self.api.records['Attachment'][eid] = dict(id=eid, name='proposal.pdf',
                file='data:application/pdf;base64,' + base64.b64encode((self.root / 'proposal.pdf').read_bytes()).decode())
        self.api.records['Email'][email] = dict(id=email, name='Manual subject', body='Approved body',
            isHtml=False, status='Draft', to=self.row['email'], **{'from': 'golwebstudio@bk.ru'},
            parentType='Account', parentId=account, attachmentsNames={'email-pdf': 'proposal.pdf'})
        return email

    def test_manual_clinic_requires_explicit_document_binding(self):
        self.manual_draft()
        self.row.pop('phones')
        before = copy.deepcopy(self.api.records)
        original_request = self.api.request

        def request(method, route, payload=None):
            if method == 'GET' and route.endswith('/documents'):
                return {'list': list(self.api.records['Document'].values()), 'total': 1}
            return original_request(method, route, payload)

        self.api.request = request
        result = self.export()[0]
        self.assertEqual(result['status'], 'HOLD')
        self.assertIn('existing_document_id', result['reason'])
        self.assertEqual(self.api.records, before)
        self.assertFalse(self.api.posts)

    def test_saved_clinic_draft_is_verified_without_body_repair(self):
        self.row['website'] = 'https://aclinic-krd.ru/'
        first = self.export()[0]
        self.assertEqual(first['status'], 'DRAFT_VERIFIED')
        before_posts = len(self.api.posts)
        self.assertEqual(self.export()[0], first)
        self.api.records['Email'][first['email_id']]['body'] = 'Changed body'
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        self.assertEqual(len(self.api.posts), before_posts)
        self.assertEqual(self.api.records['Email'][first['email_id']]['body'], 'Changed body')

    def test_api_safe_diagnostics(self):
        with patch.dict('os.environ', {'ESPO_API_KEY': 'secret-key'}):
            api = API()
        for exc, expected in ((HTTPError('secret-key', 403, 'secret-key', {}, None), 'HTTP 403'),
                              (URLError(PermissionError(1, 'secret-key')), 'PermissionError errno=1'),
                              (ValueError('secret-key'), 'invalid JSON')):
            with self.subTest(expected=expected), patch.object(api.opener, 'open', side_effect=exc):
                with self.assertRaises(Hold) as error:
                    api.request('GET', 'Email/test')
                self.assertIn(expected, str(error.exception))
                self.assertNotIn('secret-key', str(error.exception))

    def test_missing_email(self):
        self.row['email'] = ''
        self.assertEqual(self.export()[0]['status'], 'DOCUMENT_VERIFIED_NO_DRAFT')
        self.assertEqual(len(self.api.posts), 3)
        self.assertEqual(self.export()[0]['status'], 'DOCUMENT_VERIFIED_NO_DRAFT')
        self.assertEqual(len(self.api.posts), 3)

    def test_invalid_email_and_approval(self):
        for changes in ({'email': 'a@example.org,b@example.org'},
                        {'email': 'a@example.org\nBcc: x@example.org'},
                        {'export_approved': 'false'},
                        {'export_approved': 1}):
            with self.subTest(changes=changes):
                self.assertEqual(self.export([{**self.row, **changes}])[0]['status'], 'HOLD')
        self.assertFalse(self.api.posts)

    def test_missing_broken_and_oversize_pdf(self):
        for data in (None, b'not a PDF', b'%PDF-' + b'x' * (10 * 1024 * 1024)):
            with self.subTest(size=None if data is None else len(data)):
                path = self.root / 'proposal.pdf'
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(data)
                self.assertEqual(self.export()[0]['status'], 'HOLD')
        self.assertFalse(self.api.posts)

    def test_ambiguous_account_and_name_only_conflict(self):
        self.api.records['Account'] = {
            'a': {'id': 'a', 'name': self.row['company_name'], 'website': self.row['website']},
            'b': {'id': 'b', 'name': self.row['company_name'], 'website': self.row['website']}}
        self.assertIn('Ambiguous Account', self.export()[0]['reason'])
        del self.api.records['Account']['b']
        self.api.records['Account']['a']['website'] = 'https://other.invalid'
        self.assertIn('conflict', self.export()[0]['reason'])
        self.assertFalse(self.api.posts)

    def test_lost_response_recovered_at_every_step(self):
        for kind in ('Account', 'Document', 'Email'):
            with self.subTest(kind=kind):
                self.journal.unlink(missing_ok=True)
                self.api = FakeAPI()
                self.api.lose = kind
                self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')
                self.assertEqual(len(self.api.posts), 4)
                self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')
                self.assertEqual(len(self.api.posts), 4)

    def test_uncertain_post_not_repeated_when_search_cannot_recover(self):
        self.api.lose = 'Attachment'
        original = self.api.request

        def request(method, route, payload=None):
            try:
                return original(method, route, payload)
            except Hold:
                self.api.hide = True
                raise

        self.api.request = request
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        count = len(self.api.posts)
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        self.assertEqual(len(self.api.posts), count)

    def test_continue_after_row_error(self):
        self.api.fail = self.row['company_name']
        other = {**self.row, 'id': 'approved-2', 'company_name': 'Other Company',
                 'website': 'https://other.invalid'}
        result = self.export([self.row, other])
        self.assertEqual([r['status'] for r in result], ['HOLD', 'DRAFT_VERIFIED'])

    def test_changed_pdf_recipient_and_deleted_record_hold(self):
        self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')
        count = len(self.api.posts)
        self.assertEqual(self.export([{**self.row, 'email': 'other@example.invalid'}])[0]['status'],
                         'HOLD')
        (self.root / 'proposal.pdf').write_bytes(b'%PDF-new')
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        self.assertEqual(len(self.api.posts), count)

    def test_get_verification_rejects_changed_records_and_file(self):
        first = self.export()[0]
        count = len(self.api.posts)
        email = self.api.records['Email'][first['email_id']]
        email['sendAt'] = '2030-01-01 09:00:00'
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        del email['sendAt']
        original_file = self.api.file
        self.api.file = lambda eid: b'%PDF-wrong'
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        self.api.file = original_file
        del self.api.records['Document'][first['document_id']]
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        self.assertEqual(len(self.api.posts), count)

    def test_account_website_scheme_variation_reused(self):
        self.api.records['Account']['a'] = {'id': 'a', 'name': self.row['company_name'],
                                           'website': 'http://www.example.invalid/'}
        self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')
        self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')
        self.assertEqual(len(self.api.posts), 3)

    def test_bare_account_domain_reused_and_reverified(self):
        account = dict(id='a', name=self.row['company_name'],
                       website='example.invalid', emailAddress=self.row['email'])
        self.api.records['Account']['a'] = account
        first = self.export()[0]
        self.assertEqual(first['status'], 'DRAFT_VERIFIED')
        self.assertEqual(first['account_id'], 'a')
        for website in ('www.example.invalid/', 'http://example.invalid/',
                        'https://www.example.invalid/'):
            with self.subTest(website=website):
                account['website'] = website
                self.assertEqual(self.export()[0], first)
                self.assertEqual(len(self.api.posts), 3)
        account['name'] = 'Different company'
        self.assertIn('Account binding changed', self.export()[0]['reason'])
        account['name'] = self.row['company_name']
        account['website'] = 'other.invalid'
        self.assertIn('Account binding changed', self.export()[0]['reason'])
        self.assertEqual(len(self.api.posts), 3)

    def test_bare_domain_different_name_prevents_duplicate(self):
        self.api.records['Account']['a'] = dict(
            id='a', name='Different company', website='example.invalid')
        self.assertIn('conflict', self.export()[0]['reason'])
        self.assertFalse(self.api.posts)

    def test_clinic_bare_domain_uses_explicit_document_and_any_account_id(self):
        self.row.update(company_name='Клиника А', website='https://aclinic-krd.ru/',
                        email='')
        account_id = '6ae32c46426f8cdb3'
        document_id = '6ae32e009a0f5cc31'
        file_id = '6ae32da53033facf1'
        self.api.records['Account'][account_id] = dict(
            id=account_id, name=self.row['company_name'], website='aclinic-krd.ru')
        self.api.records['Document'][document_id] = dict(
            id=document_id, fileId=file_id, fileName='proposal.pdf', accountsIds=[account_id])
        self.api.records['Attachment'][file_id] = dict(
            id=file_id, name='proposal.pdf', file='data:application/pdf;base64,' + base64.b64encode(
                (self.root / 'proposal.pdf').read_bytes()).decode())
        self.row['existing_document_id'] = document_id
        original = copy.deepcopy(self.api.records)
        for _ in range(2):
            result = self.export()[0]
            self.assertEqual(result['status'], 'DOCUMENT_VERIFIED_NO_DRAFT')
            self.assertEqual(result['account_id'], account_id)
            self.assertEqual(self.api.records, original)
            self.assertFalse(self.api.posts)
        self.journal.unlink()
        account = self.api.records['Account'].pop(account_id)
        account['id'] = 'wrong'
        self.api.records['Account']['wrong'] = account
        self.api.records['Document'][document_id]['accountsIds'] = ['wrong']
        result = self.export()[0]
        self.assertEqual(result['status'], 'DOCUMENT_VERIFIED_NO_DRAFT')
        self.assertEqual(result['account_id'], 'wrong')
        self.assertFalse(self.api.posts)

    def test_schema_and_placeholder_template_block_before_post(self):
        self.template.write_text(json.dumps({'name': 'FILL_FROM_APPROVED_SAMPLE',
                                             'body': 'text', 'isHtml': False}))
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        self.assertFalse(self.api.posts)
        with self.assertRaises(Hold):
            check_schema(type('EmptySchema', (), {'request': lambda *args: {}})())

    def test_duplicate_ids(self):
        self.assertEqual([r['status'] for r in self.export([self.row, self.row])], ['HOLD', 'HOLD'])
        self.assertFalse(self.api.posts)

    def test_csv_cli_preview_and_hold_exit_codes(self):
        manifest = self.root / 'approved.csv'
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'espo-export'),
                   str(manifest), '--journal', str(self.journal)]
        for valid, expected in ((True, 0), (False, 1)):
            row = {**self.row, 'recipient_verified': 'true', 'export_approved': 'true'}
            if not valid:
                row['email'] = 'invalid' 
            with manifest.open('w', newline='', encoding='utf-8-sig') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(json.loads(result.stdout)[0]['status'],
                             'PREVIEW' if valid else 'HOLD')
        self.assertFalse(self.journal.exists())

    def test_contacts_and_document_relation(self):
        self.row.update(phones=['+78619922327', '+79189381920'],
                        segment='Медицинские организации', industry_verified=True)
        result = self.export()[0]
        self.assertEqual(result['status'], 'DRAFT_VERIFIED')
        account = self.api.get('Account', result['account_id'])
        self.assertEqual(account['emailAddress'], self.row['email'])
        self.assertEqual(len(account['phoneNumberData']), 2)
        self.assertEqual(account['cSegment'], self.row['segment'])
        document = self.api.get('Document', result['document_id'])
        self.assertTrue(document['publishDate'])
        self.assertEqual(document['accountsIds'], [result['account_id']])

    def test_existing_contacts_preserved_and_completed(self):
        self.api.records['Account']['a'] = dict(id='a', name=self.row['company_name'],
            website=self.row['website'], emailAddress='old@example.invalid', phoneNumber='+71111111111')
        self.row['phones'] = ['+72222222222']
        self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')
        account = self.api.get('Account', 'a')
        self.assertEqual(account['emailAddress'], 'old@example.invalid')
        self.assertEqual(account['phoneNumber'], '+71111111111')
        self.assertEqual(len(account['phoneNumberData']), 2)
        self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')

    def test_unverified_email_keeps_document(self):
        self.row['recipient_verified'] = False
        self.assertEqual(self.export()[0]['status'], 'DOCUMENT_VERIFIED_NO_DRAFT')
        self.assertFalse(self.api.records['Email'])

    def test_adopt_document_without_attachment_source_evidence_holds(self):
        first = self.export()[0]
        self.journal.unlink()
        self.row['existing_document_id'] = first['document_id']
        count = len(self.api.posts)
        result = self.export()[0]
        self.assertEqual(result['status'], 'HOLD')
        self.assertIn('source relation', result['reason'])
        self.assertEqual(len(self.api.posts), count)

    def test_missing_clinic_created_normally_without_duplicates(self):
        self.row['website'] = 'https://aclinic-krd.ru/'
        result = self.export()[0]
        self.assertEqual(result['status'], 'DRAFT_VERIFIED')
        count = len(self.api.posts)
        self.assertEqual(self.export()[0], result)
        self.assertEqual(len(self.api.posts), count)
        self.assertEqual(sum(kind == 'Account' for kind, _ in self.api.posts), 1)

    def test_default_approved_template_personalizes_only_subject(self):
        result = run([self.row], self.root, self.journal, api=self.api, confirm=True)[0]
        self.assertEqual(result['status'], 'DRAFT_VERIFIED')
        email = self.api.get('Email', result['email_id'])
        self.assertIn(self.row['company_name'], email['name'])
        approved = json.loads((Path(__file__).resolve().parents[1] /
                               'src/lexradar/espo_email.json').read_text())
        self.assertEqual(email['body'], approved['body'])
        self.assertFalse(result['advertising_send_authorized'])

    def test_document_relation_must_be_read_back(self):
        original = self.api.request
        for mode in ('missing', 'extra', 'incomplete'):
            with self.subTest(mode=mode):
                def request(method, route, payload=None):
                    result = original(method, route, payload)
                    if route.endswith('/accounts'):
                        if mode == 'missing':
                            return {'list': []}
                        if mode == 'extra':
                            result['list'].append({'id': 'other-account'})
                        if mode == 'incomplete':
                            result['total'] = len(result['list']) + 1
                    return result
                self.api.request = request
                self.assertEqual(self.export()[0]['status'], 'HOLD')
                self.assertFalse(self.api.records['Email'])

    def test_phone_fallback_and_neutral_segment(self):
        original = self.api.request
        def request(method, route, payload=None):
            result = original(method, route, payload)
            if route == 'Metadata':
                result['entityDefs']['Account']['fields'].pop('phoneNumber')
            return result
        self.api.request = request
        self.row['phones'] = '+71111111111;+72222222222'
        first = self.export()[0]
        self.assertEqual(first['status'], 'DRAFT_VERIFIED')
        account = self.api.get('Account', first['account_id'])
        self.assertIn('+71111111111', account['description'])
        self.assertIn('+72222222222', account['description'])
        self.assertEqual(account['cSegment'], 'Не определено')
        self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')

    def test_existing_account_reused(self):
        self.api.records['Account']['a'] = {'id': 'a', 'name': self.row['company_name'],
                                           'website': self.row['website']}
        self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')
        self.assertEqual(len(self.api.posts), 3)


if __name__ == '__main__':
    unittest.main()
