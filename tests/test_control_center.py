"""Exercise HTTP request handling without requiring a listening socket."""

import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from control_organizations import load_organizations
import unittest
from unittest.mock import patch

from control_center import ControlHandler


class MemoryConnection:
    def __init__(self, path):
        self.request = io.BytesIO(f"GET {path} HTTP/1.0\r\n\r\n".encode("ascii"))
        self.response = bytearray()

    def makefile(self, *args, **kwargs):
        return self.request

    def sendall(self, data):
        self.response.extend(data)


class ControlCenterTest(unittest.TestCase):
    def request(self, path, binary=False):
        connection = MemoryConnection(path)
        ControlHandler(connection, ("127.0.0.1", 0),
                       SimpleNamespace(results_dir=getattr(self, "root", Path.cwd())))
        headers, body = bytes(connection.response).split(b"\r\n\r\n", 1)
        self.assertIn(f"Content-Length: {len(body)}".encode(), headers)
        return headers.decode("ascii"), body if binary else body.decode("utf-8")

    def test_routes(self):
        headers, page = self.request("/")
        self.assertIn("200 OK", headers)
        self.assertIn("text/html; charset=utf-8", headers)
        self.assertIn('<html lang="ru">', page)
        self.assertIn("Центр управления LexRadar", page)
        for section in ("projects", "reports", "settings"):
            self.assertIn(f'href="#{section}"', page)
            self.assertIn(f'id="{section}"', page)
        for path, expected in (
            ("/health", {"status": "ok"}),
            ("/api/status", {
                "service": "LexRadar Control Center", "status": "ok",
                "mode": "read_only", "analysis_enabled": False,
                "mailing_enabled": False,
            }),
        ):
            with self.subTest(path=path):
                headers, body = self.request(path)
                self.assertIn("200 OK", headers)
                self.assertIn("application/json", headers)
                self.assertEqual(json.loads(body), expected)
        headers, body = self.request("/missing")
        self.assertIn("404 Not Found", headers)
        self.assertEqual(json.loads(body), {"error": "not_found"})


class OrganizationTest(unittest.TestCase):
    request = ControlCenterTest.request

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False))
        return path

    def audit(self, count=1):
        return {'schema_version': 'primary-audit-0.1',
                'target_url': 'https://clinic.test/', 'legal_status': 'not_confirmed',
                'organizations': [dict(id=f'organization-{i}', name=f'Клиника {i:02}',
                                       inn=f'770000{i:04}') for i in range(count)]}

    def test_empty_and_invalid_routes(self):
        self.assertIn('Организаций пока нет', self.request('/organizations')[1])
        for path in ('/organizations/no-such-id', '/organizations/../../etc/passwd',
                     '/organizations/%2e%2e%2fsecret'):
            self.assertIn('404 Not Found', self.request(path)[0])
        self.assertFalse(list(self.root.iterdir()))
        self.assertIn('href="/#overview"', self.request('/organizations')[1])

    def test_search_pagination_detail_and_read_only(self):
        path = self.write('run/primary/audit.json', self.audit(23))
        original = path.read_bytes()
        with patch('socket.create_connection', side_effect=AssertionError('Network forbidden')):
            records, warnings = load_organizations(self.root)
            self.request('/organizations')
        self.assertEqual((len(records), warnings), (23, 0))
        page = self.request('/organizations')[1]
        self.assertIn('Найдено: 23', page)
        self.assertIn('Клиника 19', page)
        self.assertNotIn('Клиника 20', page)
        self.assertIn('Далее', page)
        filtered_page = self.request('/organizations?q=clinic.test&status=not_confirmed')[1]
        self.assertIn('q=clinic.test&amp;status=not_confirmed&amp;page=2', filtered_page)
        for query in (quote('кЛиНиКа 21'), '7700000021'):
            page = self.request('/organizations?q=' + query)[1]
            self.assertIn('Найдено: 1', page)
            self.assertIn('Клиника 21', page)
        self.assertIn('Найдено: 23', self.request('/organizations?q=CLINIC.TEST')[1])
        self.assertIn('Клиника 22', self.request('/organizations?page=2')[1])
        for number in ('oops', '-9', '9999999999'):
            self.assertIn('200 OK', self.request('/organizations?page=' + number)[0])
        self.assertIn('По запросу ничего не найдено', self.request('/organizations?status=unknown')[1])
        self.assertIn('Найдено: 23', self.request('/organizations?status=not_confirmed')[1])
        detail = self.request('/organizations/' + records[0]['id'])[1]
        self.assertIn('run/primary/audit.json', detail)
        self.assertIn('Не подтверждён', detail)
        self.assertIn('7700000000', detail)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_matrix_deduplication_and_stable_ids(self):
        audit = self.audit()
        matrix = {'audience': 'internal_only', 'rows': audit['organizations']}
        self.write('a/audit.json', audit)
        self.write('a/organizations.json', matrix)
        self.write('b/organizations.json', matrix)
        records, warnings = load_organizations(self.root)
        self.assertEqual((len(records), warnings), (2, 0))
        self.assertEqual(len({r['id'] for r in records}), 2)
        self.assertEqual([r['id'] for r in records],
                         [r['id'] for r in load_organizations(self.root)[0]])
        self.assertEqual({r['status'] for r in records}, {'not_confirmed', 'unknown'})

    def test_escape_untrusted_content(self):
        audit = self.audit()
        audit['organizations'][0]['name'] = '<script>alert(1)</script>'
        audit['target_url'] = 'javascript:alert(1)'
        self.write('audit.json', audit)
        records, _ = load_organizations(self.root)
        for path in ('/organizations?q=%22%3E%3Cscript%3E',
                     '/organizations', '/organizations/' + records[0]['id']):
            body = self.request(path)[1]
            self.assertNotIn('<script>', body)
            self.assertNotIn('href="javascript:', body)
        self.assertIn('&lt;script&gt;', self.request('/organizations')[1])

    def test_bad_files_links_and_demo_directories(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / 'audit.json'
            external.write_text(json.dumps(self.audit()))
            (self.root / 'audit.json').symlink_to(external)
            (self.root / 'linked').symlink_to(outside, target_is_directory=True)
            (self.root / 'pipe').mkdir()
            import os
            os.mkfifo(self.root / 'pipe/audit.json')
            self.write('examples/result/audit.json', self.audit())
            self.write('tests/result/audit.json', self.audit())
            self.write('.private/audit.json', self.audit())
            self.write('bad/audit.json', [])
            self.write('row/organizations.json', {'audience': 'internal_only', 'rows': [None]})
            broken = self.write('broken/audit.json', {})
            broken.write_text('{')
            self.write('schema/audit.json', {'schema_version': 'unsupported'})
            huge = self.write('huge/audit.json', {})
            with huge.open('wb') as stream:
                stream.truncate(8_000_001)
            records, warnings = load_organizations(self.root)
            self.assertEqual(records, [])
            self.assertGreaterEqual(warnings, 6)
            self.assertIn('Часть файлов недоступна', self.request('/organizations')[1])
            self.assertEqual(load_organizations(self.root / 'linked'), ([], 1))
        self.assertEqual(load_organizations(self.root / 'absent'), ([], 0))

    def test_observations_hypotheses_and_proposal_documents(self):
        source = Path(__file__).resolve().parents[1] / 'examples/primary-audit/result/audit.json'
        self.write('run/audit.json', json.loads(source.read_text()))
        pdf = self.root / 'run/proposal.pdf'
        # Transport fixture only: no PDF generation or rendering is involved.
        pdf.write_bytes(b'%PDF-1.4 saved transport fixture\n%\xff\xfe')
        (pdf.parent / 'privacy.pdf').write_bytes(b'%PDF-private source document')
        records, _ = load_organizations(self.root)
        from control_results import proposals
        documents = proposals(self.root, records[0])
        self.assertEqual([d['name'] for d in documents], ['proposal.pdf'])
        page = self.request('/organizations/' + records[0]['id'])[1]
        self.assertIn('Проблемы и наблюдения сайта', page)
        self.assertIn('Какие основания и получатели', page)
        self.assertIn('artifact_sha256', page)
        self.assertIn('unconfirmed', page)
        self.assertIn('not_performed', page)
        self.assertIn('Скачать PDF', page)
        url = '/documents/' + records[0]['id'] + '/' + documents[0]['id']
        for suffix, disposition in (('', 'inline'), ('?download=1', 'attachment')):
            headers, body = self.request(url + suffix, binary=True)
            self.assertIn('200 OK', headers)
            self.assertIn('application/pdf', headers)
            self.assertIn('Content-Disposition: ' + disposition, headers)
            self.assertEqual(body, pdf.read_bytes())
        pdf.unlink()
        self.assertIn('404 Not Found', self.request(url)[0])
        self.assertIn('Готовых PDF-коммерческих предложений',
                      self.request('/organizations/' + records[0]['id'])[1])

    def test_document_containment_and_empty_states(self):
        from control_results import proposals, read_file
        self.write('run/audit.json', self.audit())
        record = load_organizations(self.root)[0][0]
        page = self.request('/organizations/' + record['id'])[1]
        self.assertIn('Сохранённых проблем и наблюдений нет', page)
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / 'secret.pdf'
            secret.write_bytes(b'%PDF-secret')
            (self.root / 'run/proposal.pdf').symlink_to(secret)
            (self.root / 'link').symlink_to(outside, target_is_directory=True)
            self.assertEqual(proposals(self.root, record), [])
            for relative in ('../secret.pdf', str(secret), 'link/secret.pdf',
                             'run/proposal.pdf'):
                with self.subTest(relative=relative), self.assertRaises((ValueError, OSError)):
                    read_file(self.root, relative)
            import os
            os.mkfifo(self.root / 'run/proposal-pipe.pdf')
            self.assertEqual(proposals(self.root, record), [])
            for path in ('/documents/../../etc/passwd', '/documents/%2e%2e/secret',
                         '/documents/' + record['id'] + '/proposal.pdf'):
                self.assertIn('404 Not Found', self.request(path)[0])
        (self.root / 'run/proposal.pdf').unlink()
        (self.root / 'run/proposal.pdf').write_bytes(b'not a PDF')
        self.assertEqual(proposals(self.root, record), [])

    def test_result_isolation_and_untrusted_observations(self):
        from control_results import proposals, read_file
        audit = self.audit()
        audit['observations'] = [{'id': 'o1', 'stage_id': '01',
                                  'description': '<script>bad</script>',
                                  'citations': [{'quote': '<img src=x onerror=bad>'}]}]
        audit['hypotheses'] = [{'question': 'Review', 'observation_ids': None}]
        self.write('a/audit.json', audit)
        self.write('b/audit.json', self.audit())
        records, _ = load_organizations(self.root)
        first = next(r for r in records if r['source'] == 'a/audit.json')
        second = next(r for r in records if r['source'] == 'b/audit.json')
        pdf = self.root / 'a/proposal.pdf'
        pdf.write_bytes(b'%PDF-existing transport fixture')
        doc = proposals(self.root, first)[0]
        self.assertEqual(proposals(self.root, second), [])
        url = '/documents/' + second['id'] + '/' + doc['id']
        self.assertIn('404 Not Found', self.request(url)[0])
        page = self.request('/organizations/' + first['id'])[1]
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<img', page)
        self.assertNotIn('<script>', page)
        with self.assertRaises(ValueError):
            read_file(self.root, 'a/proposal.pdf', limit=5)
        with tempfile.TemporaryDirectory() as outside:
            # Replacing a directory after discovery cannot redirect a subsequent read.
            (self.root / 'a').rename(self.root / 'old-a')
            (self.root / 'a').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                read_file(self.root, doc['relative'])

    def test_repository_format_fixture(self):
        source = Path(__file__).resolve().parents[1] / 'examples/primary-audit/result'
        # Copy a real exporter fixture into a temporary results directory, never use it as fallback.
        (self.root / 'audit.json').write_bytes((source / 'audit.json').read_bytes())
        records, warnings = load_organizations(self.root)
        self.assertEqual((len(records), warnings), (2, 0))
        self.assertEqual(records[0]['websites'][0], 'https://clinic.example/')
        (self.root / 'audit.json').unlink()
        (self.root / 'organizations.json').write_bytes((source / 'organizations.json').read_bytes())
        records, warnings = load_organizations(self.root)
        self.assertEqual((len(records), warnings), (2, 0))
        self.assertIn('https://clinic.example/', records[0]['websites'])


if __name__ == "__main__":
    unittest.main()
