"""Offline receipt checks; no engine, PDF renderer or CRM."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from control_enrichment import commercial_decision


class RequalificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = Path('saved/analysis')
        self.directory = self.root / self.base / 'sites/row-000002'
        (self.directory / 'dossier').mkdir(parents=True)
        self.engine = dict(status='SKIPPED', qualification='HOLD', reason='unavailable')
        self.packet = dict(payload='saved packet', sha256=hashlib.sha256(b'saved packet').hexdigest())
        self.review = dict(qualification='READY', pdf_ready=True,
                           packet_sha256=self.packet['sha256'],
                           findings=[dict(rule_id=1, confirmed=True, evidence=[dict(quote='saved')])])
        self.write('packet.json', self.packet)
        self.write('review.json', self.review)
        self.write('dossier/collection.json', dict(schema_version='0.2'))
        (self.directory / 'proposal.html').write_text('<div data-rule="1">saved</div>')
        (self.directory / 'proposal.pdf').write_bytes(b'%PDF-1.7')

    def write(self, name, value):
        (self.directory / name).write_text(json.dumps(value))

    def receipt(self):
        files = dict(dossier='dossier/collection.json', packet='packet.json', review='review.json',
                     html='proposal.html', pdf='proposal.pdf')
        self.write('requalification.json', dict(status='SUCCESS', source_dossier='dossier',
                   hashes={key: hashlib.sha256((self.directory / name).read_bytes()).hexdigest()
                           for key, name in files.items()}))

    def decision(self):
        return commercial_decision(self.root, self.base, self.engine)[0]

    def test_confirmed_preparation(self):
        self.receipt()
        self.assertEqual(self.decision(), 'READY')
        self.review['pdf_error'] = 'failed'
        self.write('review.json', self.review)
        self.receipt()
        self.assertEqual(self.decision(), 'HOLD')

    def test_skipped_without_receipt(self):
        self.assertEqual(self.decision(), 'HOLD')
        self.engine['status'] = 'SUCCESS'
        self.assertEqual(self.decision(), 'READY')

    def test_hash_mismatch(self):
        self.review['packet_sha256'] = '0' * 64
        self.write('review.json', self.review)
        self.receipt()
        self.assertEqual(self.decision(), 'HOLD')
        self.review['packet_sha256'] = self.packet['sha256']
        self.write('review.json', self.review)
        self.assertEqual(self.decision(), 'HOLD')
