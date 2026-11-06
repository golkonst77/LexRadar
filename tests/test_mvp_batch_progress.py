"""Terminal progress checks; no network or paid analysis."""

import io
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from lexradar import mvp_batch as batch
from lexradar.audit_progress import waiting


class BatchProgressTests(unittest.TestCase):
    def test_counts_reasons_eta_and_flush(self):
        clock = Mock(return_value=100)
        output = Mock()
        with patch.object(batch.time, "monotonic", clock), patch("builtins.print", output):
            rows = [
                {"status": "SUCCESS", "reason": ""},
                {"status": "pending", "reason": ""},
                {"status": "pending", "reason": ""},
            ]
            progress = batch.BatchProgress(rows)
            progress.heartbeat(None)
            self.assertIn("осталось примерно: неизвестно", output.call_args.args[0])
            iterator = progress.rows(rows)
            next(iterator)
            row = next(iterator)
            row.update(status="SKIPPED", reason="site_limit")
            next(iterator)
            clock.return_value = 110
            progress.heartbeat(None)
            line = output.call_args.args[0]
            self.assertIn("2/3 (66.7%)", line)
            self.assertIn("SUCCESS: 1; SKIPPED: 1", line)
            self.assertIn("прошло: 10 с; осталось примерно: 10 с", line)
            self.assertTrue(any("Сайт 2/3: SKIPPED; site_limit" in call.args[0]
                                for call in output.call_args_list))
            self.assertTrue(all(call.kwargs == {"flush": True}
                                for call in output.call_args_list))

    def test_heartbeat_during_blocked_collection_and_cleanup(self):
        heartbeat = threading.Event()
        original = batch.BatchProgress.heartbeat

        def pulse(progress, stage):
            original(progress, stage)
            heartbeat.set()

        def collect(*args, **kwargs):
            self.assertTrue(heartbeat.wait(2), "No heartbeat during blocked collection")
            raise ValueError("synthetic error")

        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(batch.BatchProgress, "heartbeat", pulse),
            patch.object(batch, "waiting", lambda callback, interval: waiting(callback, 0.01)),
            patch("lexradar.collector.collect", collect),
            redirect_stdout(output),
        ):
            source = Path(temporary) / "input.csv"
            source.write_text("url,company_name,email\nhttps://example.com/,Example,\n"
                              "https://example.com/,Duplicate,\n")
            state = batch.run(source, Path(temporary) / "output", mode="prepare",
                              allow_site_collection=True)
            text = output.getvalue()
            self.assertIn("Сайтов в пакете: 2", text)
            self.assertIn("Этап: сбор материалов; завершено: 1/2 (50.0%)", text)
            self.assertIn("Сайт 1/2: ERROR; ValueError: synthetic error", text)
            self.assertIn("Сайт 2/2: SKIPPED; duplicate_url", text)
            self.assertEqual(state["summary"], {"ERROR": 1, "SKIPPED": 1})
            heartbeat.clear()
            self.assertFalse(heartbeat.wait(0.05), "Heartbeat leaked after completion")
