"""Saved contacts: no network and no engine execution."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from control_enrichment import COLLECTION, emails, observation
import test_control_analysis as analysis


class EnrichmentTest(unittest.TestCase):
    setUp = analysis.AnalysisTest.setUp
    request = analysis.AnalysisTest.request
    seed = analysis.AnalysisTest.seed
    candidate = analysis.AnalysisTest.candidate
    audit = analysis.AnalysisTest.audit
    # Reuse only the fixture and HTTP helpers, not the analyzer.
    def saved(self, lead, status='succeeded'):
        job = dict(id='saved-site', kind='analysis-site', status=status, created='2026-10-30',
                   ended='2026-10-30', params=json.dumps(dict(lead_id=lead['id'],
                   url='https://aclinic-krd.ru/', domain='aclinic-krd.ru')))
        with self.jobs.lock:
            self.jobs.db.execute('INSERT INTO jobs(id,kind,status,created,ended,params) VALUES(:id,:kind,:status,:created,:ended,:params)', job)
            self.jobs.db.commit()
        base = self.jobs.state_dir / job['id'] / 'analysis'
        (base / COLLECTION).parent.mkdir(parents=True)
        (base / 'results.csv').write_text('url,status,qualification,reason\nhttps://aclinic-krd.ru/,SUCCESS,HOLD,manual_review_required_including_unverified_constant_block4\n')
        self.collection = base / COLLECTION
        self.collection.write_text(json.dumps(dict(schema_version='0.2', target_url='https://aclinic-krd.ru/', pages=[
            dict(final_url='https://aclinic-krd.ru/contacts/', http_status=200,
                 text='klinika.aa@yandex.ru KLINIKA.AA@YANDEX.RU second@clinic.ru'),
            dict(final_url='https://other.ru/', http_status=200, text='foreign@other.ru')
        ])))
        return job

    def test_contacts_hold_and_duplicate_domains(self):
        data = self.seed([self.candidate(website='https://aclinic-krd.ru/'),
                          self.candidate(company_name='Duplicate', website='https://aclinic-krd.ru/')])
        lead = next(r for r in data['leads'] if r['name'] != 'Duplicate')
        other = next(r for r in data['leads'] if r['name'] == 'Duplicate')
        job = self.saved(lead)
        found = observation(self.jobs.state_dir, job, 'aclinic-krd.ru')
        self.assertEqual([c['email'] for c in found['contacts']], ['klinika.aa@yandex.ru', 'second@clinic.ru'])
        page = self.request('/organizations/' + lead['id'])[1].decode()
        for value in ('klinika.aa@yandex.ru', 'https://aclinic-krd.ru/contacts/', 'Требует ручной проверки', 'Статус движка: SUCCESS', '2026-10-30', 'saved_proposal_requires_review'):
            self.assertIn(value, page)
        self.assertNotIn('foreign@other.ru', page)
        self.assertNotIn('klinika.aa@yandex.ru', self.request('/organizations/' + other['id'])[1].decode())
        self.assertIn('klinika.aa@yandex.ru', self.request('/organizations')[1].decode())
        self.assertIn('200', self.request('/analysis-files/saved-site/results.csv')[0])
        for path in ('../../etc/passwd', 'collection.json', '%2e%2e', '/etc/passwd'):
            self.assertIn('404', self.request('/analysis-files/saved-site/' + path)[0])
        (self.collection.parents[3] / 'report.md').symlink_to('/etc/passwd')
        self.assertIn('404', self.request('/analysis-files/saved-site/report.md')[0])

    def test_original_preserved_failed_and_missing_data(self):
        lead = self.seed([self.candidate(website='https://aclinic-krd.ru/', email='original@clinic.ru')])['leads'][0]
        self.saved(lead, status='failed')
        before = (self.parser / 'crm-candidates.json').read_bytes()
        page = self.request('/organizations/' + lead['id'])[1].decode()
        self.assertIn('исходный email сохранён: original@clinic.ru', page)
        self.assertIn('Ошибка / прервано', page)
        for body in ('{broken', 'null', '{}', '{"schema_version":"0.2","target_url":[]}'):
            self.collection.write_text(body)
            page = self.request('/organizations/' + lead['id'])[1].decode()
            self.assertIn('контакты не извлечены', page)
            self.assertNotIn('klinika.aa@yandex.ru', page)
        self.collection.unlink()
        self.assertIn('контакты не извлечены', self.request('/organizations/' + lead['id'])[1].decode())
        self.assertEqual(before, (self.parser / 'crm-candidates.json').read_bytes())

    def test_restart_reads_existing_artifacts(self):
        from control_jobs import Jobs
        lead = self.seed([self.candidate(website='https://aclinic-krd.ru/')])['leads'][0]
        self.saved(lead)
        self._cleanups = [c for c in self._cleanups if c[0] != self.jobs.close]
        self.jobs.close()
        with patch('control_jobs.subprocess.Popen') as process:
            self.jobs = Jobs(self.root / 'state', self.parser, self.results)
            self.server.jobs = self.jobs
            self.addCleanup(self.jobs.close)
            self.assertIn('klinika.aa@yandex.ru', self.request('/organizations/' + lead['id'])[1].decode())
            process.assert_not_called()

    def test_email_validation(self):
        self.assertEqual(list(emails('a..b@mail.ru .a@mail.ru a.@mail.ru a@example.com user@mail.ru icon@2x.png a@-mail.ru a@mail..ru test@localhost')), [])
        self.assertEqual(list(emails('Contact: <Clinic.AA@Yandex.RU>.')), ['clinic.aa@yandex.ru'])

    def test_commercial_decision_saved_findings_and_pdf(self):
        from control_enrichment import commercial_decision
        lead = self.seed([self.candidate(website='https://aclinic-krd.ru/')])['leads'][0]
        job = self.saved(lead)
        base = Path(job['id']) / 'analysis'
        directory = self.jobs.state_dir / base / 'sites/row-000002'
        review_path = directory / 'review.json'
        engine = dict(status='SUCCESS', reason='manual_review_required_including_unverified_constant_block4')
        finding = dict(rule_id=1, confirmed=True, evidence=[dict(quote='Сохранённая цитата')])
        review = dict(findings=[finding], pdf_ready=True, block4_without_evidence=True)
        review_path.write_text(json.dumps(review))
        (directory / 'proposal.html').write_text('<div data-rule="1">Политика</div>')
        (directory / 'proposal.pdf').write_bytes(b'%PDF-1.7')
        self.assertEqual(commercial_decision(self.jobs.state_dir, base, engine)[0], 'READY')
        finding['manual_review'] = True
        review_path.write_text(json.dumps(review))
        self.assertEqual(commercial_decision(self.jobs.state_dir, base, engine)[0], 'HOLD')
        review['findings'] = []
        review_path.write_text(json.dumps(review))
        self.assertEqual(commercial_decision(self.jobs.state_dir, base, engine)[0], 'REJECT')
        review['findings'] = [dict(rule_id=1, confirmed=True, evidence=[dict(quote='Цитата')])]
        review_path.write_text(json.dumps(review))
        (directory / 'proposal.pdf').unlink()
        self.assertEqual(commercial_decision(self.jobs.state_dir, base, engine)[0], 'HOLD')

    def test_proposal_recipient(self):
        from control_analysis_runner import recipient
        for name in ('Клиника «А»', 'ООО Клиника "А"', 'Клиника А'):
            self.assertEqual(recipient(name), 'Руководителю клиники „А“')
        self.assertEqual(recipient('ООО «Вектор»'), 'Руководителю организации „Вектор“')
        self.assertEqual(recipient('Центр «А»'), 'Руководителю организации „Центр «А»“')
        self.assertEqual(recipient(''), 'Руководителю организации')

    def test_personalization_preserves_other_sections(self):
        import re
        import sys
        from types import SimpleNamespace
        from control_analysis_runner import personalize, INTRODUCTION
        fields = re.compile(r'(<span contenteditable=true>)(.*?)(</span>)', re.S)
        page = ('<span contenteditable=true>30.10.2026</span>'
                '<span contenteditable=true>Руководителю клиники</span>'
                '<span contenteditable=true>https://aclinic-krd.ru/</span>'
                '<p class="intro"><span style="font-size:11.5pt">В рамках программы '
                'лояльности для всех коммерческих организаций РФ мы провели '
                'выборочный бесплатный экспресс-аудит вашего сайта.</span></p>'
                '<div data-rule="1">Санкции и признаки</div><p>Стоимость: 100</p>')
        with patch.dict(sys.modules, {'lexradar.client_proposal': SimpleNamespace(PERSONALIZATION=fields)}):
            result = personalize(page, 'Клиника «А»')
            self.assertIn('Руководителю клиники „А“', result)
            self.assertIn(INTRODUCTION, result)
            self.assertEqual(result[result.index('<div data-rule='):], page[page.index('<div data-rule='):])
            self.assertNotIn('программы лояльности', result)
            with self.assertRaises(ValueError):
                personalize(page.replace('В рамках программы', 'Другой текст'), 'А')
