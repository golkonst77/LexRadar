import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from lexradar import pipeline10 as pipeline


def rows():
    return [{"id": f"parser-{i}", "company_name": f"Clinic {i}",
             "website": f"https://clinic{i}.invalid", "email": "",
             "export_approved": False} for i in range(10)]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = io.StringIO()

    def run_pipeline(self, approvals=None, **kwargs):
        with redirect_stdout(self.output):
            return pipeline.process(rows(), self.root, approvals or {},
                                    journal=self.root / "journal.json", **kwargs)

    def test_no_permission_makes_no_network_or_audit_calls(self):
        with patch.object(pipeline, "CircuitAPI") as api, patch.object(pipeline, "audit") as audit:
            self.assertEqual(self.run_pipeline(), 1)
            api.assert_not_called()
            audit.assert_not_called()
        self.assertEqual(json.loads((self.root / "summary.json").read_text())["HOLD"], 10)
        self.assertIn("[10/10]", self.output.getvalue())

    def test_connection_failure_checked_once_does_not_block_local_audit(self):
        with patch.object(pipeline, "CircuitAPI", side_effect=pipeline.espo.Hold("blocked")) as api:
            with patch.object(pipeline, "audit") as audit:
                self.run_pipeline(check_crm=True, allow_collection=True)
                self.assertEqual(api.call_count, 1)
                self.assertEqual(audit.call_count, 10)

    def test_row_error_continues_and_technical_pdfs_never_export(self):
        effects = [RuntimeError("private details"), *((self.root / "p.pdf", "sha", False),) * 9]
        with patch.object(pipeline, "CircuitAPI"), patch.object(pipeline.espo, "check_schema"), \
                patch.object(pipeline, "find_account", return_value="existing"), \
                patch.object(pipeline, "audit", side_effect=effects), \
                patch.object(pipeline.espo, "run") as export:
            self.run_pipeline(check_crm=True, confirm=True, allow_collection=True)
            export.assert_not_called()
        summary = json.loads((self.root / "summary.json").read_text())
        self.assertEqual((summary["ERROR"], summary["REVIEW"], summary["PDF"]), (1, 9, 9))
        self.assertNotIn("private details", self.output.getvalue())

    def test_approved_pdf_uses_existing_exporter_and_journal(self):
        approvals = {r["id"]: {**r, "identity_verified": True, "export_approved": True,
                              "pdf_sha256": "sha"} for r in rows()}
        with patch.object(pipeline, "CircuitAPI"), patch.object(pipeline.espo, "check_schema"), \
                patch.object(pipeline, "find_account", return_value=""), \
                patch.object(pipeline, "audit", return_value=(self.root / "p.pdf", "sha", True)), \
                patch.object(pipeline.espo, "run", return_value=[{"status": "DRAFT_VERIFIED"}]) as export:
            self.assertEqual(self.run_pipeline(approvals, check_crm=True, confirm=True, allow_collection=True), 0)
            self.assertEqual(export.call_count, 10)
            call = export.call_args
            self.assertEqual(call.args[2], self.root / "journal.json")
            self.assertNotIn("sendAt", call.args[0][0])

    def test_changed_pdf_or_missing_confirm_blocks_export(self):
        approvals = {r["id"]: {**r, "identity_verified": True, "export_approved": True,
                              "pdf_sha256": "wrong"} for r in rows()}
        with patch.object(pipeline, "CircuitAPI"), patch.object(pipeline.espo, "check_schema"), \
                patch.object(pipeline, "find_account", return_value=""), \
                patch.object(pipeline, "audit", return_value=(self.root / "p.pdf", "sha", True)), \
                patch.object(pipeline.espo, "run") as export:
            self.run_pipeline(approvals, check_crm=True, confirm=True, allow_collection=True)
            export.assert_not_called()
            for approval in approvals.values():
                approval["pdf_sha256"] = "sha"
            self.run_pipeline(approvals, check_crm=True, confirm=False, allow_collection=True)
            export.assert_not_called()

    def test_local_collection_needs_no_crm_and_never_exports(self):
        with patch.object(pipeline, "CircuitAPI") as api, \
                patch.object(pipeline, "audit", return_value=(self.root / "p.pdf", "sha", False)), \
                patch.object(pipeline.espo, "run") as export:
            self.run_pipeline(allow_collection=True)
            api.assert_not_called()
            export.assert_not_called()
        summary = json.loads((self.root / "summary.json").read_text())
        self.assertEqual((summary["audit_success"], summary["proposal_eligible"],
                          summary["full_PDF"], summary["CRM_Drafts"]), (10, 10, 10, 0))
        self.assertNotIn("Technical draft only", self.output.getvalue())

    def test_audit_preserves_contact_and_existing_engine_settings(self):
        row = {**rows()[0], "email": "verified@example.invalid"}
        state = {"rows": [{"status": "SKIPPED", "reason": "no_confirmed_triggers_1_2_3_5",
                           "stages": ["evidence_packet:SUCCESS"],
                           "html_pages_attempted": 5, "html_pages_captured": 4}]}
        with patch("lexradar.mvp_batch.run", return_value=state) as run:
            with self.assertRaises(pipeline.espo.Hold):
                pipeline.audit(row, {}, self.root, True)
        import csv

        actual = next(csv.DictReader((self.root / "prepare.csv").open()))
        self.assertEqual(actual["email"], row["email"])
        self.assertEqual(actual["company_name"], "")  # Identity approval remains required.
        self.assertEqual(run.call_args.kwargs["mode"], "prepare")
        self.assertEqual(run.call_args.kwargs["max_ai_calls"], 0)
        self.assertIs(run.call_args.kwargs["ignore_robots"], True)
        details = json.loads((self.root / "audit-summary.json").read_text())
        self.assertTrue(details["audit_success"])
        self.assertFalse(details["proposal_eligible"])

    def test_cli_passes_ignore_robots_to_batch(self):
        source = self.root / "source.csv"
        source.write_bytes(b"original")
        state = {"rows": [{"status": "SKIPPED", "reason": "no_confirmed_triggers"}]}
        for flags, expected in (([], True), (["--ignore-robots"], True)):
            with self.subTest(ignore_robots=expected), \
                    patch.object(pipeline, "convert", return_value=(b"", rows())), \
                    patch("lexradar.mvp_batch.run", return_value=state) as run, \
                    patch.object(pipeline, "CircuitAPI") as api, \
                    patch.object(pipeline.espo, "run") as export, \
                    redirect_stdout(self.output):
                self.assertEqual(pipeline.main([
                    str(source), "--output", str(self.root / str(expected)),
                    "--allow-site-collection", *flags,
                ]), 1)
                self.assertEqual(run.call_count, 10)
                for call in run.call_args_list:
                    self.assertIs(call.kwargs["ignore_robots"], expected)
                api.assert_not_called()
                export.assert_not_called()

    def test_pdf_check_rejects_blank_pdf(self):
        from pypdf import PdfWriter

        pdf = self.root / "blank.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(pdf)
        with self.assertRaises(pipeline.espo.Hold):
            pipeline.pdf_check(pdf)

    def test_repeat_keeps_edited_approval_template_and_creates_new_log(self):
        source = self.root / "source.csv"
        source.write_bytes(b"original")
        output = self.root / "run"
        args = [str(source), "--output", str(output)]
        with patch.object(pipeline, "convert", return_value=(b"", rows())), \
                redirect_stdout(self.output):
            self.assertEqual(pipeline.main(args), 1)
            template = output / "approval-template.json"
            template.write_text('{"operator": "preserved"}')
            self.assertEqual(pipeline.main(args), 1)
        self.assertEqual(template.read_text(), '{"operator": "preserved"}')
        self.assertEqual(len(list(output.glob("pipeline-*.log"))), 2)

    def test_api_circuit_never_retries_after_failure(self):
        api = object.__new__(pipeline.CircuitAPI)
        with patch.object(pipeline.espo.API, "request", side_effect=pipeline.espo.Hold("blocked")) as request:
            for _ in range(3):
                with self.assertRaises(pipeline.espo.Hold):
                    api.request("GET", "Metadata")
            self.assertEqual(request.call_count, 1)

    def test_ambiguous_accounts_hold(self):
        api = Mock()
        api.find.side_effect = [[{"id": "a", "website": rows()[0]["website"]}],
                                [{"id": "b", "website": rows()[0]["website"]}]]
        with self.assertRaises(pipeline.espo.Hold):
            pipeline.find_account(api, rows()[0])


if __name__ == "__main__":
    unittest.main()
