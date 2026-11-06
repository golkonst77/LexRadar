"""No crawl, database writes, provider calls or CRM imports."""
import sys
from pathlib import Path

import pytest

import control_analysis_runner as runner
from control_enrichment import commercial_decision


@pytest.mark.parametrize('reason', ['HTTP 415', 'crawl_duration_limit',
    'Fetch failed: SSLCertVerificationError', 'HTTP 403', 'Encoded response unsupported'])
def test_expected_unavailability_is_hold(monkeypatch, tmp_path, reason):
    import lexradar.mvp_batch as batch
    row = dict(status='SKIPPED', qualification='HOLD', reason=reason, stages=[])
    monkeypatch.setattr(batch, 'run', lambda *args, **kwargs: {'rows': [row]})
    source = tmp_path / 'input.csv'
    source.write_text('url,company_name,email\nhttps://fixture.example/,Clinic,\n')
    monkeypatch.setattr(sys, 'argv', ['runner', str(source), str(tmp_path / 'output')])
    assert runner.main() == 0
    assert commercial_decision(tmp_path, Path('missing'), row) == ('HOLD', reason)


def test_unexpected_error_stays_error(monkeypatch, tmp_path):
    import lexradar.mvp_batch as batch
    row = dict(status='ERROR', qualification='HOLD', reason='ValueError: broken packet', stages=[])
    monkeypatch.setattr(batch, 'run', lambda *args, **kwargs: {'rows': [row]})
    source = tmp_path / 'input.csv'
    source.write_text('url,company_name,email\nhttps://fixture.example/,Clinic,\n')
    monkeypatch.setattr(sys, 'argv', ['runner', str(source), str(tmp_path / 'output')])
    assert runner.main() == 1
    assert commercial_decision(tmp_path, Path('missing'), row)[0] == 'ERROR'
