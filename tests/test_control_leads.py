"""Parser exports are leads; audit mentions never become leads."""

import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control_leads import CSV_HEADERS, load_leads
from control_results import audit_details, proposals
import test_control_center as fixtures


class ParserLeadsTest(unittest.TestCase):
    request = fixtures.ControlCenterTest.request
    write = fixtures.OrganizationTest.write

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def csv(self, rows, relative='parser.csv', bom=True):
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, delimiter=';', fieldnames=sorted(CSV_HEADERS) + ['ИНН', 'Статус скрининга', 'Бренд'])
        writer.writeheader()
        writer.writerows(rows)
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stream.getvalue(), encoding='utf-8-sig' if bom else 'utf-8')
        return path

    def row(self, **changes):
        return {'Организация': 'Клиника', 'Официальный сайт': 'https://clinic.test/',
                'Телефон': '+7 111 222', 'Email': 'contact@clinic.test',
                'Исходная карточка': 'https://aggregator.test/card', 'ИНН': '7701234567',
                'Статус скрининга': 'review', **changes}

    def test_csv_contacts_bom_and_plain(self):
        for bom in (True, False):
            with self.subTest(bom=bom):
                path = self.csv([self.row()], bom=bom)
                original = path.read_bytes()
                with patch('socket.create_connection', side_effect=AssertionError('No network')):
                    records, warnings = load_leads(self.root)
                    page = self.request('/organizations')[1]
                self.assertEqual((len(records), warnings), (1, 0))
                record = records[0]
                self.assertEqual(record['websites'], ['https://clinic.test/'])
                self.assertEqual(record['phone'], '+7 111 222')
                self.assertEqual(record['email'], 'contact@clinic.test')
                self.assertEqual(record['status'], 'review')
                detail = self.request('/organizations/' + record['id'])[1]
                for value in ('Клиника', '+7 111 222', 'contact@clinic.test', 'aggregator.test/card', 'review'):
                    self.assertIn(value, page)
                    self.assertIn(value, detail)
                self.assertIn('parser.csv', detail)
                self.assertEqual(path.read_bytes(), original)

    def test_json_candidates_and_original_csv_priority(self):
        candidate = dict(id='parser-id', company_name='Candidate', website='https://candidate.test',
                         phone='123', email='', source_card='https://source.test', crm_status='NOT_EXPORTED')
        path = self.write('handoff/crm-candidates.json', [candidate])
        path.write_text('\ufeff' + path.read_text())
        records, warnings = load_leads(self.root)
        self.assertEqual((len(records), warnings), (1, 0))
        self.assertEqual(records[0]['status'], 'NOT_EXPORTED')
        self.csv([self.row()], 'handoff/parser.csv')
        records, warnings = load_leads(self.root)
        self.assertEqual((len(records), warnings), (1, 0))
        self.assertEqual(records[0]['name'], 'Клиника')

    def test_search_filter_pagination(self):
        self.csv([self.row(**{'Организация': f'Clinic {i:02}', 'Телефон': f'phone{i:02}',
                             'Email': f'email{i:02}@test', 'ИНН': f'inn{i:02}',
                             'Официальный сайт': f'https://site{i:02}.test/'}) for i in range(23)])
        for query in ('clinic%2021', 'SITE21.TEST', 'inn21', 'phone21', 'EMAIL21%40test'):
            page = self.request('/organizations?q=' + query)[1]
            self.assertIn('Найдено: 1', page)
            self.assertIn('Clinic 21', page)
        page = self.request('/organizations?q=clinic&status=review')[1]
        self.assertIn('Найдено: 23', page)
        self.assertNotIn('Clinic 20', page)
        self.assertIn('q=clinic&amp;status=review&amp;page=2', page)
        self.assertIn('Clinic 22', self.request('/organizations?page=2')[1])
        self.assertIn('По запросу ничего не найдено', self.request('/organizations?status=absent')[1])

    def test_empty_missing_and_no_demo_fallback(self):
        self.assertEqual(load_leads(self.root / 'missing'), ([], 0))
        self.csv([])
        self.assertEqual(load_leads(self.root), ([], 0))
        self.assertIn('Организаций пока нет', self.request('/organizations')[1])
        (self.root / 'parser.csv').unlink()
        self.write('crm-candidates.json', [])
        self.assertEqual(load_leads(self.root), ([], 0))
        self.csv([self.row()], 'examples/parser.csv')
        self.csv([self.row()], 'tests/parser.csv')
        self.assertEqual(load_leads(self.root), ([], 0))

    def test_mentions_and_unrelated_audit_pdf_never_link_to_lead(self):
        audit = fixtures.OrganizationTest.audit(self, 4)
        audit['target_url'] = 'https://kaluga-med.ru/'
        self.write('audit.json', audit)
        self.assertEqual(load_leads(self.root), ([], 0))
        page = self.request('/organizations')[1]
        self.assertNotIn('Клиника 00', page)
        self.csv([self.row(**{'Официальный сайт': ''})])
        (self.root / 'proposal.pdf').write_bytes(b'%PDF-unrelated')
        record = load_leads(self.root)[0][0]
        self.assertEqual(record['websites'], [])
        self.assertIsNone(audit_details(self.root, record))
        self.assertEqual(proposals(self.root, record), [])
        page = self.request('/organizations/' + record['id'])[1]
        self.assertIn('<dt>Сайт</dt><dd>—</dd>', page)
        self.assertNotIn('kaluga-med.ru', page)
        self.assertNotIn('Открыть PDF', page)
        # Even a shared official domain does not prove the audited legal entity.
        self.csv([self.row(**{'Официальный сайт': audit['target_url']})])
        record = load_leads(self.root)[0][0]
        self.assertEqual(proposals(self.root, record), [])
        from control_results import document_id
        self.assertIn('404 Not Found', self.request('/documents/' + record['id'] + '/' + document_id('proposal.pdf'))[0])

    def test_invalid_official_site_not_replaced_by_source_or_brand(self):
        for value in ('', 'javascript:alert(1)', 'https://user:pass@site.test', 'https://bad host/'):
            self.csv([self.row(**{'Официальный сайт': value, 'Бренд': 'https://brand.test'})])
            self.assertEqual(load_leads(self.root)[0][0]['websites'], [])

    def test_status_is_not_invented_and_html_is_escaped(self):
        candidate = dict(company_name='<script>bad</script>', website='', phone='<img>', email='', source_card='source')
        self.write('crm-candidates.json', [candidate])
        record = load_leads(self.root)[0][0]
        self.assertEqual(record['status'], '')
        page = self.request('/organizations/' + record['id'])[1]
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<dt>Статус обработки</dt>', page)
        self.assertNotIn('<th>Статус обработки</th>', self.request('/organizations')[1])

    def test_malformed_files_links_and_size_limit(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / 'crm-candidates.json'
            external.write_text('[]')
            (self.root / 'crm-candidates.json').symlink_to(external)
            self.assertEqual(load_leads(self.root), ([], 1))
            (self.root / 'crm-candidates.json').unlink()
            (self.root / 'linked').symlink_to(outside, target_is_directory=True)
            self.assertEqual(load_leads(self.root / 'linked'), ([], 1))
        path = self.csv([self.row()])
        path.write_text('Организация;Организация\nx;x')
        self.assertEqual(load_leads(self.root), ([], 1))
        self.csv([self.row()])
        with path.open('a') as stream:
            stream.write('bad;columns\n')
        self.assertEqual(load_leads(self.root), ([], 1))
        with path.open('wb') as stream:
            stream.truncate(8_000_001)
        self.assertEqual(load_leads(self.root), ([], 1))
        path.unlink()
        os.mkfifo(path)
        self.assertEqual(load_leads(self.root), ([], 1))

    def test_record_links_do_not_change_owner_after_reordering(self):
        first = self.row(**{'Организация': 'First'})
        second = self.row(**{'Организация': 'Second'})
        self.csv([first, second])
        original = {r['name']: r['id'] for r in load_leads(self.root)[0]}
        self.csv([second, first])
        self.assertEqual(original, {r['name']: r['id'] for r in load_leads(self.root)[0]})
        self.csv([second])
        self.assertIn('404 Not Found', self.request('/organizations/' + original['First'])[0])

    def test_invalid_json_and_known_non_lead_csv(self):
        path = self.write('crm-candidates.json', {})
        self.assertEqual(load_leads(self.root), ([], 1))
        path.write_text('{')
        self.assertEqual(load_leads(self.root), ([], 1))
        self.write('crm-candidates.json', [None, {'company_name': 'not a candidate'}])
        self.assertEqual(load_leads(self.root), ([], 2))
        path.unlink()
        (self.root / 'lexradar.csv').write_text('url,company_name,email\nhttps://site.test,Audit,\n')
        (self.root / 'results.csv').write_text('not Parser data')
        self.assertEqual(load_leads(self.root), ([], 0))

    def test_mentions_discovery_has_a_directory_limit(self):
        from control_organizations import load_organizations
        for i in range(130):
            (self.root / f'run{i:03}').mkdir()
        records, warnings = load_organizations(self.root)
        self.assertEqual(records, [])
        self.assertGreater(warnings, 0)

    def test_depth_and_directory_limit_and_refresh(self):
        self.csv([self.row()], 'one/two/parser.csv')
        self.csv([self.row()], 'one/two/three/parser.csv')
        self.assertEqual(len(load_leads(self.root)[0]), 1)
        self.csv([self.row(), self.row(**{'Организация': 'New'})], 'one/two/parser.csv')
        self.assertEqual(len(load_leads(self.root)[0]), 2)
        for i in range(70):
            (self.root / f'dir{i:02}').mkdir()
        self.assertGreater(load_leads(self.root)[1], 0)


if __name__ == '__main__':
    unittest.main()
