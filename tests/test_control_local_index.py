import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control_catalog import discover
from control_followup import safe_phone
from control_leads import CSV_HEADERS, load_leads
from control_organizations import load_organizations


class LocalIndexTest(unittest.TestCase):
    def test_parser_phone_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'parser.csv'
            values = ["'+7 (999) 123-45-67", "''+79991234567", "'+7999<script>", '+79991234567']
            with path.open('w') as stream:
                writer = csv.DictWriter(stream, fieldnames=sorted(CSV_HEADERS), delimiter=';')
                writer.writeheader()
                for value in values:
                    writer.writerow({'Организация': 'Clinic', 'Телефон': value})
            original = path.read_bytes()
            records, warnings = load_leads(tmp)
            by_original = {r['row']['Телефон']: r['phone'] for r in records}
            self.assertEqual(warnings, 0)
            self.assertEqual(safe_phone(by_original[values[0]]), '+79991234567')
            self.assertIsNone(safe_phone(by_original[values[1]]))
            self.assertIsNone(safe_phone(by_original[values[2]]))
            self.assertEqual(by_original[values[3]], values[3])
            self.assertEqual(path.read_bytes(), original)

    def test_working_sources_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.git').mkdir()
            for directory in ['runs/production-live', 'runs/recheck-live', 'work/result',
                              'commercial-refresh-20261102/result', 'runs-088/live',
                              'runs-123/private', 'runs/experiment-smoke', 'archive/old', 'tests/data']:
                path = root / directory
                path.mkdir(parents=True)
                (path / 'organizations.json').write_text('{"audience":"internal_only","rows":[]}')
            found, warnings = discover(root)
            self.assertEqual(len(found), 4)
            self.assertEqual(warnings, 0)
            self.assertEqual(load_organizations(root), ([], 0))
            (root / 'work/result/organizations.json').write_text('invalid')
            self.assertEqual(load_organizations(root), ([], 1))
            original = __import__('os').scandir
            def denied(path):
                if Path(path) == root / 'work/result':
                    raise PermissionError()
                return original(path)
            with patch('control_catalog.os.scandir', side_effect=denied):
                self.assertEqual(discover(root)[1], 1)
