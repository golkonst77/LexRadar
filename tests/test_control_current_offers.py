"""Local version replacement and refusal; no engine, network or working journals."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control_jobs import Jobs
from control_enrichment import observation
from control_offers import cleanup, current_offers, directory, publish, verified
from control_mvp import MVPHandler


class CurrentOffersTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.jobs = Jobs(self.root / 'state', self.root / 'parser', self.root / 'results')
        self.addCleanup(self.jobs.close)
        self.home = self.root / 'home'
        self.journals = self.home / '.local/state/lexradar'
        self.journals.mkdir(parents=True)
        for name in ('crm-prepare', 'espo-export'):
            (self.journals / (name + '.json')).write_text('{}')
            (self.journals / (name + '.lock')).touch()

    def saved(self, identifier, status='succeeded', findings=True, pdf=True, url='https://www.clinic.ru/'):
        from control_analysis import target
        job = dict(id=identifier, kind='analysis-site', status=status, created='2026-10-30',
                   ended='2026-10-30T12:00:00Z', params=json.dumps(dict(
                       lead_id=identifier, url=url, domain=target(url)[1])))
        with self.jobs.lock:
            self.jobs.db.execute('INSERT INTO jobs(id,kind,status,created,ended,params) VALUES(:id,:kind,:status,:created,:ended,:params)', job)
            self.jobs.db.commit()
        base = self.jobs.state_dir / directory(identifier)
        base.mkdir(parents=True)
        (base.parents[1] / 'results.csv').write_text('url,status,qualification,reason\n' + url + ',SUCCESS,HOLD,manual_review_required_including_unverified_constant_block4\n')
        (base / 'review.json').write_text(json.dumps(dict(qualification='HOLD', pdf_ready=True,
            reason='manual_review_required_including_unverified_constant_block4', client_release_allowed=False,
            findings=[dict(rule_id=1, confirmed=True, evidence=[dict(quote='Evidence')])] if findings else [])))
        (base / 'proposal.html').write_text('<div data-rule="1">' + identifier + '</div>')
        if pdf:
            (base / 'proposal.pdf').write_bytes(b'%PDF-1.7\n' + identifier.encode())
        (base / 'work.log').write_text('keep')
        with self.jobs.lock:
            publish(self.jobs, job)
        return job, base

    def clean(self):
        with patch('control_offers.Path.home', return_value=self.home):
            with self.jobs.lock:
                cleanup(self.jobs)

    def test_ready_replaces_pair_and_ui_has_one_dated_offer(self):
        old, base = self.saved('old')
        new, new_base = self.saved('new', url='http://CLINIC.ru/')
        self.clean()
        self.assertEqual(current_offers(self.jobs)['clinic.ru']['job_id'], 'new')
        self.assertFalse((base / 'proposal.pdf').exists())
        self.assertFalse((base / 'proposal.html').exists())
        self.assertEqual((base / 'work.log').read_text(), 'keep')
        self.assertTrue((base / 'review.json').exists())
        self.assertTrue((new_base / 'proposal.pdf').exists())
        review = json.loads((new_base / 'review.json').read_text())
        self.assertEqual(review['qualification'], 'READY')
        self.assertEqual(review['engine_qualification'], 'HOLD')
        self.assertFalse(review['client_release_allowed'])
        self.assertEqual(review['control_center']['job_id'], 'new')
        lead = dict(id='merged', name='Clinic', websites=['https://clinic.ru/'], phone='', email='')
        handler = object.__new__(MVPHandler)
        handler.server = SimpleNamespace(jobs=self.jobs)
        handler.index = lambda: {'leads': [lead, dict(lead, id='duplicate')]}
        data, _, counts = handler.workflow()
        self.assertEqual(len(data['offers']), 1)
        self.assertEqual(data['offers'][0]['job']['id'], 'new')
        pages = []
        handler.html = pages.append
        handler.proposals_page()
        self.assertIn('2026-10-30T12:00:00Z', pages[0])
        self.assertNotIn('/proposal-files/old/', pages[0])

    def test_hold_reject_error_keep_success_and_own_status(self):
        _, base = self.saved('old')
        for identifier, options, expected in (
                ('hold', dict(pdf=False), 'HOLD'),
                ('reject', dict(findings=False), 'REJECT'),
                ('error', dict(status='failed'), 'ERROR')):
            job, path = self.saved(identifier, **options)
            self.clean()
            self.assertEqual(current_offers(self.jobs)['clinic.ru']['job_id'], 'old')
            self.assertTrue((base / 'proposal.pdf').exists())
            self.assertEqual(observation(self.jobs.state_dir, job, 'clinic.ru')['qualification'], expected)
            self.assertEqual(json.loads((path / 'review.json').read_text())['qualification'], expected)

    def test_crm_references_protect_old_pair_and_journal_bytes(self):
        _, base = self.saved('old')
        journal = self.journals / 'crm-prepare.json'
        raw = json.dumps({'saved': {'sha256': hashlib.sha256((base / 'proposal.pdf').read_bytes()).hexdigest()}})
        journal.write_text(raw)
        self.saved('new')
        self.clean()
        self.assertTrue((base / 'proposal.pdf').exists())
        self.assertTrue((base / 'proposal.html').exists())
        self.assertEqual(journal.read_text(), raw)
        handler = object.__new__(MVPHandler)
        handler.server = SimpleNamespace(jobs=self.jobs)
        handler.respond_json = Mock()
        handler.analysis_file('/analysis-files/old/proposal.pdf')
        handler.respond_json.assert_called_once_with(404, {'error': 'proposal_unavailable'})

    def test_unreadable_journal_retains_files(self):
        _, base = self.saved('old')
        self.saved('new')
        (self.journals / 'crm-prepare.json').write_text('{broken')
        self.clean()
        self.assertTrue((base / 'proposal.pdf').exists())

    def test_other_run_review_cannot_be_ready(self):
        _, base = self.saved('old')
        job, path = self.saved('new', pdf=False)
        review = json.loads((base / 'review.json').read_text())
        (path / 'review.json').write_text(json.dumps(review))
        (path / 'proposal.pdf').write_bytes(b'%PDF-1.7\nnew')
        publish(self.jobs, job)
        self.assertEqual(observation(self.jobs.state_dir, job, 'clinic.ru')['reason'], 'review_run_mismatch')
        self.assertEqual(current_offers(self.jobs)['clinic.ru']['job_id'], 'old')

    def test_atomic_review_failure_keeps_current_pair(self):
        _, base = self.saved('old')
        job, path = self.saved('new', pdf=False)
        (path / 'proposal.pdf').write_bytes(b'%PDF-1.7\nnew')
        with patch('control_offers.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                publish(self.jobs, job)
        self.assertEqual(current_offers(self.jobs)['clinic.ru']['job_id'], 'old')
        self.assertTrue((base / 'proposal.pdf').exists())

    def test_modified_current_is_not_served_and_old_is_not_deleted(self):
        _, base = self.saved('old')
        _, path = self.saved('new')
        (path / 'proposal.pdf').write_bytes(b'%PDF-tampered')
        active = current_offers(self.jobs)['clinic.ru']
        self.assertFalse(verified(self.jobs, active))
        self.clean()
        self.assertTrue((base / 'proposal.pdf').exists())
        publish(self.jobs, self.jobs.get('new'))
        self.assertFalse(verified(self.jobs, current_offers(self.jobs)['clinic.ru']))

    def test_pointer_commit_failure_rolls_back(self):
        _, base = self.saved('old')
        job, path = self.saved('new', pdf=False)
        (path / 'proposal.pdf').write_bytes(b'%PDF-1.7\nnew')
        database = self.jobs.db
        proxy = Mock(wraps=database)
        proxy.commit.side_effect = sqlite3.OperationalError('disk full')
        with patch.object(self.jobs, 'db', proxy):
            with self.assertRaises(sqlite3.OperationalError):
                publish(self.jobs, job)
        self.assertEqual(current_offers(self.jobs)['clinic.ru']['job_id'], 'old')
        self.assertTrue((base / 'proposal.pdf').exists())

    def test_startup_recovers_last_ready_without_hiding_later_error(self):
        self.saved('old')
        self.saved('new')
        self.saved('error', status='failed')
        self.jobs.close()
        self._cleanups = [c for c in self._cleanups if c[0] != self.jobs.close]
        self.jobs = Jobs(self.root / 'state', self.root / 'parser', self.root / 'results')
        self.addCleanup(self.jobs.close)
        self.assertEqual(current_offers(self.jobs)['clinic.ru']['job_id'], 'new')
        error = self.jobs.get('error')
        self.assertEqual(observation(self.jobs.state_dir, error, 'clinic.ru')['qualification'], 'ERROR')


if __name__ == '__main__':
    unittest.main()
