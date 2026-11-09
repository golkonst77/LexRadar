import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from daily_call_plan import save_local


class DailyCallPlanTest(unittest.TestCase):
    def test_saved_plan_is_not_recalculated_or_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            now = datetime(2026, 11, 9, 22, tzinfo=timezone.utc)
            build = Mock(return_value={'tasks': [{'task_id': str(i)} for i in range(10)]})
            first = save_local(directory, build, now)
            path = directory / 'daily-call-plan-2026-11-10.json'
            original = path.read_bytes()
            build.side_effect = AssertionError('must not recalculate')
            self.assertEqual(save_local(directory, build, now), first)
            build.assert_called_once_with()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(json.loads(original)['tasks']), 10)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(list(directory.glob('.daily-call-plan-*')))
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                save_local(directory, build, now)
            self.assertEqual(path.read_text(), '{broken')
