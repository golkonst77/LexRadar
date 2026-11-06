"""Synthetic data only; no network or external application writes."""

import csv
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from lexradar.espo_export import Hold, validate
from lexradar.parser_import import REQUIRED, convert, prepare


def parser_csv(**changes):
    row = dict.fromkeys(sorted(REQUIRED), "")
    row.update({"Организация": "Synthetic clinic", "Официальный сайт": "https://example.invalid",
                "Бренд": "probable", **changes})
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=row, delimiter=";")
    writer.writeheader()
    writer.writerow(row)
    return stream.getvalue().encode("utf-8-sig")


class ParserImportTests(unittest.TestCase):
    def test_empty_email_and_uncertain_identity(self):
        audit, candidates = convert(parser_csv())
        row = list(csv.DictReader(io.StringIO(audit.decode())))[0]
        self.assertEqual(set(row), {"url", "company_name", "email"})
        self.assertEqual(row["email"], "")
        self.assertNotIn("Synthetic clinic", row["company_name"])
        self.assertIn("не подтверждена", row["company_name"])
        self.assertIn("missing_email", candidates[0]["hold_reasons"])
        self.assertFalse(candidates[0]["automatic_send_allowed"])
        with self.assertRaises(Hold):
            validate(candidates[0], Path("/tmp"))

    def test_email_does_not_grant_approval(self):
        audit, candidates = convert(parser_csv(Email="test@example.invalid"))
        self.assertEqual(candidates[0]["email"], "test@example.invalid")
        self.assertFalse(candidates[0]["recipient_verified"])
        self.assertFalse(candidates[0]["export_approved"])
        self.assertEqual(list(csv.DictReader(io.StringIO(audit.decode())))[0]["email"], "")

    def test_invalid_rows(self):
        for raw in (b"wrong;headers\na;b\n", parser_csv(**{"Официальный сайт": "file:///tmp/a"}),
                    parser_csv() + b"extra;row\n", parser_csv(**{"Организация": ""})):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                convert(raw)
        raw = parser_csv()
        with self.assertRaises(ValueError):
            convert(raw + raw.splitlines(keepends=True)[-1])

    def test_snapshot_repeat_tampering_and_wal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.csv"
            source.write_bytes(parser_csv())
            database = root / "source.db"
            with sqlite3.connect(database) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE example (value TEXT)")
                db.execute("INSERT INTO example VALUES ('synthetic')")
                db.commit()
                target, count, reused = prepare(source, database, root / "data")
                self.assertEqual(count, 1)
                self.assertFalse(reused)
                with sqlite3.connect(target / "parser.sqlite") as copy:
                    self.assertEqual(copy.execute("SELECT * FROM example").fetchall(),
                                     [("synthetic",)])
                self.assertTrue(prepare(source, database, root / "data")[2])
                self.assertEqual(json.loads((target / "import.json").read_text())["crm_status"],
                                 "NOT_EXPORTED")
                self.assertEqual((target / "parser.csv").stat().st_mode & 0o777, 0o600)
                (target / "lexradar.csv").write_text("changed")
                with self.assertRaises(ValueError):
                    prepare(source, database, root / "data")

    def test_no_database_creation_on_missing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.csv"
            source.write_bytes(parser_csv())
            with self.assertRaises(sqlite3.Error):
                prepare(source, root / "missing.db", root / "data")
            self.assertFalse((root / "missing.db").exists())
            self.assertFalse(list((root / "data").glob("parser-*")))


if __name__ == "__main__":
    unittest.main()
