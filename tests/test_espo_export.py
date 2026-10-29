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

from lexradar.espo_export import Hold, check_schema, inspect_sample, run


class FakeAPI:
    def __init__(self):
        self.records = {k: {} for k in ('Account', 'Attachment', 'Document', 'Email')}
        self.posts = []
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
        if method == 'GET' and route == 'Metadata':
            required = {
                'Account': ('name', 'website', 'description'),
                'Attachment': ('name', 'type', 'role', 'related', 'parent', 'field'),
                'Document': ('name', 'file', 'publishDate'),
                'Email': ('name', 'body', 'isHtml', 'status', 'to', 'from',
                          'parent', 'attachments'),
            }
            data = {'entityDefs': {k: {'fields': {f: {} for f in fields}}
                                   for k, fields in required.items()}}
            data['entityDefs']['Email']['fields']['status']['options'] = ['Draft', 'Sent']
            return data
        if method != 'POST':
            raise AssertionError('Unexpected mutation: ' + method + ' ' + route)
        if payload.get('name') == self.fail:
            raise Hold('Injected failure')
        eid = 'id' + str(len(self.posts) + 1)
        self.posts.append((route, copy.deepcopy(payload)))
        self.records[route][eid] = {**copy.deepcopy(payload), 'id': eid}
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
        self.api.records['Account']['example-account'] = {'id': 'example-account'}
        self.api.records['Email']['example-email'] = {
            'id': 'example-email', 'parentType': 'Account',
            'parentId': 'example-account', 'attachmentsIds': ['pdf'],
            'name': 'Original subject', 'body': 'Original copy', 'isHtml': False,
            'to': 'private@example.invalid'}
        self.api.records['Document']['example-document'] = {
            'id': 'example-document', 'fileId': 'pdf'}
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
        self.assertEqual(len(self.api.posts), 5)
        second = self.export()[0]
        self.assertEqual(first, second)
        self.assertEqual(len(self.api.posts), 5)
        email = self.api.get('Email', first['email_id'])
        self.assertEqual(email['status'], 'Draft')
        self.assertEqual(email['from'], 'sender@example.invalid')
        self.assertEqual(email['parentId'], first['account_id'])
        self.assertNotIn('sendAt', email)
        self.assertTrue(email['name'].startswith('Approved subject [LexRadar-'))
        self.assertEqual(email['body'], 'Approved body')
        self.assertNotIn('messageId', email)

    def test_missing_email(self):
        self.row['email'] = ''
        self.assertEqual(self.export()[0]['status'], 'HOLD')
        self.assertFalse(self.api.posts)

    def test_invalid_email_and_approval(self):
        for changes in ({'email': 'a@example.org,b@example.org'},
                        {'email': 'a@example.org\nBcc: x@example.org'},
                        {'recipient_verified': False}, {'export_approved': 'false'},
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
        for kind in ('Account', 'Attachment', 'Document', 'Email'):
            with self.subTest(kind=kind):
                self.journal.unlink(missing_ok=True)
                self.api = FakeAPI()
                self.api.lose = kind
                self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')
                self.assertEqual(len(self.api.posts), 5)
                self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')
                self.assertEqual(len(self.api.posts), 5)

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
        self.assertEqual(len(self.api.posts), 4)

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
                row['email'] = ''
            with manifest.open('w', newline='', encoding='utf-8-sig') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertEqual(json.loads(result.stdout)[0]['status'],
                             'PREVIEW' if valid else 'HOLD')
        self.assertFalse(self.journal.exists())

    def test_existing_account_reused(self):
        self.api.records['Account']['a'] = {'id': 'a', 'name': self.row['company_name'],
                                           'website': self.row['website']}
        self.assertEqual(self.export()[0]['status'], 'DRAFT_VERIFIED')
        self.assertEqual(len(self.api.posts), 4)


if __name__ == '__main__':
    unittest.main()
