import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lexradar import crm_prepare as crm
from test_espo_export import FakeAPI


class ReviewAPI(FakeAPI):
    def __init__(self):
        super().__init__()
        self.records['Note'] = {}
        self.fail = '__never__'
        self.reject_extra = False

    def request(self, method, route, payload=None):
        assert route.split('/')[0] != 'Email'
        assert not payload or 'sendAt' not in payload
        if route == 'Metadata':
            value = super().request(method, route, payload)
            value['entityDefs']['Note'] = {'fields': {k: {} for k in ('type', 'post', 'parent')}}
            return value
        if self.reject_extra and method == 'POST' and route == 'Attachment' and payload['type'] != 'application/pdf':
            raise crm.espo.Hold('Unsupported material')
        if method == 'GET' and route.endswith('/documents'):
            account = route.split('/')[1]
            docs = [dict(doc) for doc in self.records['Document'].values()
                    if account in doc.get('accountsIds', [])]
            return {'list': docs, 'total': len(docs)}
        return super().request(method, route, payload)

    def find(self, kind, *args, **kwargs):
        assert kind != 'Attachment', 'Attachment collection is forbidden'
        return super().find(kind, *args, **kwargs)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'dossier').mkdir()
        (self.root / 'dossier/evidence.txt').write_text('evidence')
        data = b'%PDF-1.4 fixture'
        (self.root / 'proposal.pdf').write_bytes(data)
        self.row = dict(id='review-1', company_name='Test', website='https://test.invalid/',
                        pdf_path='proposal.pdf', pdf_sha256=hashlib.sha256(data).hexdigest(),
                        dossier='dossier', letter_text='Future letter', identity_verified=True,
                        email='unverified address')
        self.api = ReviewAPI()
        self.journal = self.root / 'journal.json'

    def run_prepare(self, confirm=True):
        return crm.run([self.row], self.root, self.journal, confirm=confirm, api=self.api)[0]

    def test_preview_never_contacts_crm(self):
        with patch.object(self.api, 'request', side_effect=AssertionError('network')):
            result = self.run_prepare(False)
        self.assertEqual(result['status'], 'PREVIEW')
        self.assertEqual(len(result['materials']), 2)
        self.assertFalse(self.journal.exists())

    def test_repeat_no_duplicates_no_mail_and_unverified_email(self):
        first = self.run_prepare()
        self.assertEqual(first['status'], 'PENDING_REVIEW')
        count = len(self.api.posts)
        self.assertEqual(first, self.run_prepare())
        self.assertEqual(len(self.api.posts), count)
        self.assertFalse(self.api.records['Email'])
        self.assertNotIn('emailAddress', next(iter(self.api.records['Account'].values())))
        self.assertIn('Future letter', next(iter(self.api.records['Note'].values()))['post'])

    def test_optional_failure_retains_pdf_note_and_retry_safe(self):
        self.api.reject_extra = True
        result = self.run_prepare()
        self.assertEqual(result['status'], 'PENDING_REVIEW')
        self.assertTrue(result['note_id'])
        self.assertEqual(len(result['errors']), 0)
        self.assertEqual(len(self.api.records['Document']), 1)
        evidence = result['materials'][1]
        self.assertEqual(evidence['storage'], 'server_only')
        self.assertTrue(Path(evidence['path']).is_absolute())
        self.assertEqual(evidence['sha256'], hashlib.sha256(b'evidence').hexdigest())
        self.assertEqual(json.loads(self.journal.read_text())[self.row['id']]['result'], result)
        count = len(self.api.posts)
        self.run_prepare()
        self.assertEqual(len(self.api.posts), count)

    def test_note_403_is_warning_without_duplicate_pdf(self):
        original = self.api.find
        def find(kind, *args, **kwargs):
            if kind == 'Note':
                raise crm.espo.Hold('CRM HTTP 403: GET Note; inspect CRM before retry')
            return original(kind, *args, **kwargs)
        self.api.find = find
        result = self.run_prepare()
        self.assertEqual(result['status'], 'PENDING_REVIEW')
        self.assertIn('Note skipped:', result['warnings'][0]['error'])
        count = len(self.api.posts)
        self.assertEqual(result, self.run_prepare())
        self.assertEqual(len(self.api.posts), count)
        self.assertFalse(self.api.records['Note'])

    def test_old_optional_errors_do_not_block_verified_pdf(self):
        self.run_prepare()
        state = json.loads(self.journal.read_text())
        path = str((self.root / 'dossier/evidence.txt').resolve())
        state[self.row['id']]['skipped'] = {path: 'CRM HTTP 403: GET Attachment; inspect CRM before retry'}
        self.journal.write_text(json.dumps(state))
        result = self.run_prepare()
        self.assertEqual(result['status'], 'PENDING_REVIEW')
        self.assertEqual(result['materials'][1]['storage'], 'server_only')
        self.assertEqual(len(self.api.records['Attachment']), 1)

    def test_missing_optional_dossier_does_not_hold(self):
        self.row['dossier'] = 'missing'
        result = self.run_prepare()
        self.assertEqual(result['status'], 'PENDING_REVIEW')
        self.assertEqual(result['warnings'][0]['error'], 'Dossier directory missing')
        self.assertEqual(len(self.api.records['Document']), 1)

    def test_identity_required_and_conflicting_account_untouched(self):
        self.row['identity_verified'] = False
        self.assertEqual(self.run_prepare()['status'], 'HOLD_IDENTITY')
        self.assertFalse(self.api.posts)
        self.row['identity_verified'] = True
        self.api.records['Account']['old'] = dict(id='old', name='Other', website=self.row['website'])
        self.assertEqual(self.run_prepare()['status'], 'HOLD_IDENTITY')
        self.assertFalse(self.api.posts)

    def test_existing_document_never_reassigned(self):
        self.run_prepare()
        document = next(iter(self.api.records['Document'].values()))
        document['accountsIds'] = ['other-account']
        count = len(self.api.posts)
        result = self.run_prepare()
        self.assertEqual(result['status'], 'HOLD')
        self.assertEqual(document['accountsIds'], ['other-account'])
        self.assertFalse(any(method == 'PUT' for method, _, _ in self.api.calls))

    def test_changed_pdf_rejected_without_posts(self):
        (self.root / 'proposal.pdf').write_bytes(b'%PDF-changed')
        self.assertEqual(self.run_prepare()['status'], 'HOLD')
        self.assertFalse(self.api.posts)

    def test_lost_post_recovers_without_duplicate(self):
        self.api.lose = 'Document'
        self.assertEqual(self.run_prepare()['status'], 'PENDING_REVIEW')
        count = len(self.api.posts)
        self.run_prepare()
        self.assertEqual(len(self.api.posts), count)

    def test_lost_note_response_recovers_on_next_run(self):
        self.api.lose = 'Note'
        self.assertEqual(self.run_prepare()['status'], 'PENDING_REVIEW')
        count = len(self.api.posts)
        self.assertEqual(self.run_prepare()['status'], 'PENDING_REVIEW')
        self.assertEqual(len(self.api.posts), count)

    def test_old_pending_note_never_reposts_or_blocks_pdf(self):
        self.run_prepare()
        state = json.loads(self.journal.read_text())
        entry = state[self.row['id']]
        entry['steps'] = {k: v for k, v in entry['steps'].items() if not k.startswith('note-')}
        entry['steps']['note-old-outcome'] = {'pending': True}
        self.api.records['Note'].clear()
        self.journal.write_text(json.dumps(state))
        count = len(self.api.posts)
        for _ in range(2):
            result = self.run_prepare()
            self.assertEqual(result['status'], 'PENDING_REVIEW')
            self.assertIn('Note skipped:', result['warnings'][0]['error'])
            self.assertEqual(len(self.api.posts), count)
        self.assertEqual(json.loads(self.journal.read_text())[self.row['id']]['steps']['note-old-outcome'],
                         {'pending': True})

    def test_note_failure_does_not_retry_after_access_recovers(self):
        original = self.api.find
        def find(kind, *args, **kwargs):
            if kind == 'Note':
                raise crm.espo.Hold('Note unavailable')
            return original(kind, *args, **kwargs)
        with patch.object(self.api, 'find', side_effect=find):
            self.assertEqual(self.run_prepare()['status'], 'PENDING_REVIEW')
        self.run_prepare()
        state = json.loads(self.journal.read_text())
        self.assertTrue(state[self.row['id']].get('note_skipped'))
        self.assertFalse(self.api.records['Note'])

    def test_resume_old_403_skipped_with_saved_account(self):
        first = self.run_prepare()
        state = json.loads(self.journal.read_text())
        entry = state[self.row['id']]
        account = entry['account_id']
        known = '6ae3566554a3b0e47'
        self.api.records['Account'][known] = self.api.records['Account'].pop(account)
        self.api.records['Account'][known]['id'] = known
        self.api.records['Note'].clear()
        entry['account_id'] = known
        entry['steps']['account'] = {'id': known}
        for doc in self.api.records['Document'].values():
            doc['accountsIds'] = [known]
        entry['steps'] = {'account': {'id': known}}
        entry['skipped'] = {m['path']: 'CRM HTTP 403: GET Attachment; inspect CRM before retry'
                            for m in first['materials']}
        self.journal.write_text(json.dumps(state))
        result = self.run_prepare()
        self.assertEqual(result['account_id'], known)
        self.assertEqual(result['status'], 'PENDING_REVIEW')
        self.assertNotIn(first['materials'][0]['path'],
                         json.loads(self.journal.read_text())[self.row['id']]['skipped'])
        count = len(self.api.posts)
        self.assertEqual(result, self.run_prepare())
        self.assertEqual(count, len(self.api.posts))
        self.assertEqual(len(self.api.records['Document']), 1)

    def test_forbidden_material_does_not_create_note(self):
        original = self.api.request
        def request(method, route, payload=None):
            if method == 'GET' and route.endswith('/documents'):
                raise crm.espo.Hold('CRM HTTP 403: GET Account/documents; inspect CRM before retry')
            return original(method, route, payload)
        self.api.request = request
        self.assertEqual(self.run_prepare()['status'], 'HOLD')
        self.assertFalse(self.api.records['Note'])
        self.assertFalse(self.api.records['Email'])

    def test_recovery_checks_hash_without_new_posts(self):
        self.run_prepare()
        attachment = next(iter(self.api.records['Attachment'].values()))
        attachment['file'] = 'data:application/pdf;base64,YmFk'
        count = len(self.api.posts)
        self.assertEqual(self.run_prepare()['status'], 'HOLD')
        self.assertFalse(any(kind == 'Attachment' for kind, _ in self.api.posts[count:]))


class AutomaticIdentityTests(unittest.TestCase):
    setUp = PrepareTests.setUp
    run_prepare = PrepareTests.run_prepare
    def capture(self, text):
        self.row['identity_verified'] = False
        self.row['company_name'] = 'ООО «Тест»'
        artifacts = self.root / 'dossier/artifacts'
        artifacts.mkdir(exist_ok=True)
        raw = text.encode()
        (artifacts / 'page-0001.html').write_bytes(raw)
        (artifacts / 'page-0001.txt').write_text(text)
        (self.root / 'dossier/collection.json').write_text(json.dumps({
            'target_url': self.row['website'], 'pages': [{
                'evidence_id': 'page-0001', 'http_status': 200,
                'final_url': self.row['website'], 'text': text,
                'reading': {'source_sha256': hashlib.sha256(raw).hexdigest()}}]}))

    def test_automatic_and_repeat(self):
        self.capture('© ООО «Тест» ИНН 7701234567')
        result = self.run_prepare()
        self.assertEqual(result['identity']['method'], 'saved_site')
        self.assertEqual(result['status'], 'PENDING_REVIEW')
        count = len(self.api.posts)
        self.assertEqual(result, self.run_prepare())
        self.assertEqual(count, len(self.api.posts))

    def test_unicode_quotes_spaces_case_and_full_legal_form(self):
        self.capture('© Общество\u00a0с ограниченной ответственностью „\u200eТЕ\u200bСТ‟ '
                     'ИНН 7701234567. ООО"Тест"')
        result = self.run_prepare(False)
        self.assertEqual(result['status'], 'PREVIEW')
        self.assertEqual(len({crm.identity_name(e['name']) for e in result['identity']['evidence']}), 1)
        self.assertEqual(crm.identity_name('Публичное акционерное общество «Тест»'), 'пао тест')

    def test_different_forms_and_abbreviated_names_are_not_merged(self):
        for other in ('АО «Тест»', 'ООО «Т.»'):
            with self.subTest(other=other):
                self.capture('© ООО «Тест». ' + other)
                self.assertEqual(self.run_prepare(False)['status'], 'HOLD_IDENTITY')

    def test_third_party_blocks_and_owner_mentions(self):
        self.capture('© ООО «Тест» ИНН 7701234567. '
                     'Услуга веб-анализа предоставляется ООО «Яндекс» ИНН 7736207543. '
                     'Пиксель для взаимодействия с рекламой предоставляется ООО «ВК» ИНН 7743001840. '
                     'Рекламодатель ООО «Реклама» ИНН 7703388936.')
        result = self.run_prepare(False)
        self.assertEqual(result['status'], 'PREVIEW')
        self.assertEqual(result['identity']['identifiers']['inn'], ['7701234567'])
        self.capture('© ООО «Яндекс» ИНН 7736207543')
        self.row['company_name'] = 'ООО «Яндекс»'
        self.assertEqual(self.run_prepare(False)['status'], 'PREVIEW')

    def test_normalized_name_with_conflicting_requisites_stays_hold(self):
        self.capture('© ООО «Тест» ИНН 7701234567. ООО „ТЕСТ“ ИНН 7703388936')
        self.assertEqual(self.run_prepare(False)['status'], 'HOLD_IDENTITY')

    def test_owner_after_third_party_block_is_still_a_candidate(self):
        self.capture('Сервис веб-аналитики ООО «Яндекс». Владелец сайта ООО «Другое». '
                     '© ООО «Тест»')
        self.assertEqual(self.run_prepare(False)['status'], 'HOLD_IDENTITY')

    def test_insurers_and_event_organizers_are_third_parties(self):
        self.capture('© ООО «Тест». О страховых медицинских организациях: АО «Страховщик». '
                     'Участие в мастер-классе по лечению (АО «Производитель», Франция).')
        self.assertEqual(self.run_prepare(False)['status'], 'PREVIEW')

    def test_conflicting_entities_even_with_manual_flag(self):
        self.capture('ООО «Тест» ОГРН 1107746169981 ООО «Другое» ОГРН 1157746251904')
        self.row['identity_verified'] = True
        self.assertEqual(self.run_prepare()['status'], 'HOLD_IDENTITY')
        self.assertFalse(self.api.posts)

    def test_missing_evidence_and_independent_rows(self):
        self.row['identity_verified'] = False
        good = {**self.row, 'id': 'second', 'identity_verified': True}
        results = crm.run([self.row, good], self.root, self.journal, confirm=True, api=self.api)
        self.assertEqual([r['status'] for r in results], ['HOLD_IDENTITY', 'PENDING_REVIEW'])

    def test_tampered_capture(self):
        self.capture('ООО «Тест»')
        (self.root / 'dossier/artifacts/page-0001.html').write_text('changed')
        self.assertEqual(self.run_prepare()['status'], 'HOLD_IDENTITY')
        self.assertFalse(self.api.posts)

    def test_domain_and_branch_not_inferred(self):
        self.capture('ООО «Тест»')
        self.row['company_name'] = 'Филиал Тест на Арбате'
        self.assertEqual(self.run_prepare()['status'], 'HOLD_IDENTITY')
        self.assertFalse(self.api.posts)

    def test_preview_crm_lookup_is_read_only(self):
        self.capture('© ООО «Тест»')
        self.api.records['Account']['known'] = {
            'id': 'known', 'name': 'ООО "Тест"', 'website': 'http://www.test.invalid/'}
        result = crm.run([self.row], self.root, self.journal, api=self.api, check_crm=True)[0]
        self.assertEqual(result['status'], 'PREVIEW')
        self.assertEqual(result['account_id'], 'known')
        self.assertFalse(self.api.posts)
        self.assertFalse(self.journal.exists())

    def test_previously_verified_binding_needs_no_flag(self):
        self.run_prepare()
        self.row['identity_verified'] = False
        result = self.run_prepare()
        self.assertEqual(result['status'], 'PENDING_REVIEW')
        self.assertTrue(result['identity_verified'])

    def test_mention_without_owner_context_is_insufficient(self):
        self.capture('Партнер: ООО «Тест»')
        self.assertEqual(self.run_prepare()['status'], 'HOLD_IDENTITY')
        self.assertFalse(self.api.posts)

    def test_processors_and_their_requisites_are_excluded(self):
        self.capture('© ООО «Тест» ИНН 7701234567. '
                     'Лица, осуществляющие обработку по поручению Оператора: '
                     'ООО «Колтач», ИНН 7703388936; ООО «Телемир», ИНН 7701974131.')
        result = self.run_prepare()
        self.assertEqual(result['status'], 'PENDING_REVIEW')
        self.assertEqual(result['identity']['identifiers']['inn'], ['7701234567'])
        self.assertEqual([e['role'] for e in result['identity']['evidence']],
                         ['identity_candidate', 'contractor', 'contractor'])

    def test_analytics_service_is_not_site_operator(self):
        self.capture('© ООО «Тест». Используется сервис веб-аналитики, предоставляемый '
                     'ООО «Яндекс», ИНН 7736207543.')
        self.assertEqual(self.run_prepare()['status'], 'PENDING_REVIEW')

    def test_two_clinics_remain_ambiguous_after_excluding_service(self):
        self.capture('Общая информация о медицинской организации ООО «Тест» ОГРН 1107746169981. '
                     'ООО «Филиал» ОГРН 1157746251904. '
                     'Технический сервис предоставляется ООО «Подрядчик».')
        self.assertEqual(self.run_prepare()['status'], 'HOLD_IDENTITY')
        self.assertFalse(self.api.posts)

    def test_service_role_after_name_is_excluded(self):
        self.capture('© ООО «Тест» ИНН 7701234567. '
                     'ООО «Сервис» предоставляет технический сервис, ИНН 7703388936.')
        self.assertEqual(self.run_prepare()['status'], 'PENDING_REVIEW')

    def test_clinic_after_processor_list_is_not_excluded(self):
        self.capture('© ООО «Тест». Лица по поручению Оператора: '
                     'ООО «Подрядчик», ИНН 7703388936; '
                     'ООО «Другая клиника» Лицензия на медицинскую деятельность.')
        self.assertEqual(self.run_prepare()['status'], 'HOLD_IDENTITY')

    def test_san_brand_is_separate_from_documented_site_operator(self):
        self.capture('«Оператор» — ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ '
                     '«ДОКТОР САН» (ОГРН 1127847043048, ИНН 7840464000). '
                     '«Сайт» расположен по адресу https://test.invalid/. '
                     'Реквизиты организации ООО «Доктор САН». '
                     'Лицензия на медицинскую деятельность Л041-01148-78/00348966.')
        self.row['company_name'] = 'Клиника SAN'
        result = self.run_prepare(False)
        self.assertEqual(result['status'], 'PREVIEW')
        identity = result['identity']
        self.assertEqual(identity['brand_name'], 'Клиника SAN')
        self.assertEqual(crm.identity_name(identity['legal_name']), 'ооо доктор сан')
        self.assertEqual(identity['identifiers']['inn'], ['7840464000'])

    def test_brand_without_explicit_operator_domain_link_stays_hold(self):
        for text in ('© ООО «Доктор САН» ИНН 7840464000 ОГРН 1127847043048',
                     'Оператор ООО «Доктор САН» ИНН 7840464000 ОГРН 1127847043048. '
                     'Сайт https://other.invalid/'):
            self.capture(text)
            self.row['company_name'] = 'Клиника SAN'
            self.assertEqual(self.run_prepare(False)['status'], 'HOLD_IDENTITY')

    def test_rmc_alias_requires_explicit_document_statement(self):
        self.capture('© ООО «Региональный медицинский центр». ООО «РМЦ»')
        self.assertEqual(self.run_prepare(False)['status'], 'HOLD_IDENTITY')
        self.capture('Оператор ООО «Региональный медицинский центр» (ООО «РМЦ», Оператор): '
                     'ИНН 6316222339, ОГРН 1166313101921, сайт https://test.invalid/.')
        self.row['company_name'] = 'Центр лазерной коррекции зрения'
        result = self.run_prepare(False)
        self.assertEqual(result['status'], 'PREVIEW')
        self.assertEqual(result['identity']['legal_name'], 'ООО "Региональный медицинский центр"')

    def test_explicit_alias_does_not_override_conflicting_identifiers(self):
        self.capture('Оператор ООО «Региональный медицинский центр» (ООО «РМЦ»): '
                     'ИНН 6316222339 ОГРН 1166313101921 https://test.invalid/. '
                     'ООО «РМЦ» ИНН 7701234567')
        self.assertEqual(self.run_prepare(False)['status'], 'HOLD_IDENTITY')

    def test_girumed_two_licensees_are_not_one_brand_entity(self):
        self.capture('Клиника «ГИРУДОМЕД» объединяет несколько медицинских центров: '
                     'на ул. Гвардейская — ООО «КЛЕВЕР» (ООО «КЛЕВЕР») '
                     'ОГРН 1201600083211 ИНН 1660353565 '
                     'Лицензия на медицинскую деятельность Л041-01181-16/00328052. '
                     'На пр. Победы — ООО «ГИРУДОМЕД» (ООО «ГИРУДОМЕД») '
                     'ОГРН 1181690043589 ИНН 1655402802 '
                     'Лицензия на медицинскую деятельность Л041-01181-16/00331379.')
        self.row['company_name'] = 'Клиника «ГирудоМед»'
        result = self.run_prepare(False)
        self.assertEqual(result['status'], 'HOLD_IDENTITY')
        self.assertIn('КЛЕВЕР', result['errors'][0]['error'])
        self.assertIn('ГИРУДОМЕД', result['errors'][0]['error'])

    def test_reviewed_scan_is_bound_to_pdf_hash(self):
        self.capture('© ООО «Региональный медицинский центр». ООО «РМЦ»')
        source = self.root / 'dossier/artifacts/document-0001.pdf'
        source.write_bytes(b'%PDF-1.4 reviewed fixture')
        sha = crm.digest(source.read_bytes())
        registry = self.root / 'crm_identity_documents.json'
        registry.write_text(json.dumps({sha: {'host': 'test.invalid', 'page': 1, 'section': '1.1',
            'quote': 'Оператор ООО «Региональный медицинский центр» (ООО «РМЦ», Оператор): '
                     'ИНН 6316222339 ОГРН 1166313101921 сайт https://test.invalid/'}}))
        path = self.root / 'dossier/collection.json'
        collection = json.loads(path.read_text())
        collection['documents'] = [{'evidence_id': 'document-0001', 'http_status': 200,
            'final_url': 'https://test.invalid/policy.pdf', 'reading': {'source_sha256': sha}}]
        path.write_text(json.dumps(collection))
        self.row['company_name'] = 'Центр лазерной коррекции зрения'
        with patch.object(crm, '__file__', str(self.root / 'crm_prepare.py')):
            result = self.run_prepare(False)
            self.assertEqual(result['status'], 'PREVIEW')
            evidence = result['identity']['evidence'][-1]
            self.assertEqual(evidence['page'], 1)
            self.assertEqual(evidence['sha256'], sha)
            source.write_bytes(b'changed')
            self.assertEqual(self.run_prepare(False)['status'], 'HOLD_IDENTITY')

    def test_documented_operator_does_not_override_other_legal_name_or_branch(self):
        for requested in ('ООО «Другая клиника»', 'Филиал Тест на улице Рабочая'):
            self.capture('Оператор ООО «Тест» ИНН 7701234567 ОГРН 1107746169981 '
                         'сайт https://test.invalid/')
            self.row['company_name'] = requested
            self.assertEqual(self.run_prepare(False)['status'], 'HOLD_IDENTITY')
