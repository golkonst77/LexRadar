"""No external requests or credentials required."""

import hashlib
import json
from pathlib import Path

import pytest

from lexradar.audit_capture import Capture
from lexradar.audit_review import export
from lexradar.auditors.models import Usage
from lexradar.auditors.orchestrator import analyze
from lexradar.auditors.provider import ProviderReply
from lexradar.single_auditor import messages_for, profile

AUDIT = Path("runs-064/doktstom-deepseek-chat-001")
DOSSIER = Path("runs-054/doktstom-improved/dossier")


def digest(root):
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize("outcome", ["GO", "HOLD", "ERROR"])
def test_export_decisions_and_immutable(tmp_path, outcome):
    before = digest(AUDIT), digest(DOSSIER)
    output = tmp_path / "review"
    export(AUDIT, output, dossier=DOSSIER, qualification={"outcome": outcome, "reasons": ["test"]})
    assert "ORIGINAL_NOT_AVAILABLE" in (output / "01-deepseek-original.txt").read_text()
    assert (
        json.loads((output / "04-qualification.json").read_text())["decision"]["outcome"] == outcome
    )
    assert before == (digest(AUDIT), digest(DOSSIER))
    assert output.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o077 == 0 for p in output.rglob("*"))
    assert "consent_form_issue" in (output / "CHATGPT-REVIEW.md").read_text()
    assert "PARTIAL" in (output / "05-findings-comparison.html").read_text()


def test_capture_exact_and_secrets(tmp_path):
    capture = Capture(tmp_path / "capture")
    text = ' \r\n{"findings": []}\n '
    capture.response(text)
    assert (capture.root / "original.txt").read_bytes() == text.encode()
    capture.write("secret.txt", "Authorization: Bearer secret-value sk-or-v1-test api_key=hello")
    saved = (capture.root / "secret.txt").read_text()
    assert "secret-value" not in saved and "sk-or-v1-test" not in saved and "hello" not in saved
    with pytest.raises(FileExistsError):
        Capture(capture.root)


def test_invalid_original_saved_before_validation(tmp_path):
    class Fake:
        def complete(self, settings, messages):
            return ProviderReply("  INVALID JSON\r\n", Usage())

    root = tmp_path / "analysis"
    report = analyze(DOSSIER, root, profile(), provider=Fake(), message_builder=messages_for)
    assert not report.completed
    assert (root / "capture-A-1/original.txt").read_bytes() == b"  INVALID JSON\r\n"
    assert "invalid" in (root / "capture-A-1/status.json").read_text()
    export(root, tmp_path / "export")
    assert (tmp_path / "export/01-deepseek-original.txt").read_bytes() == b"  INVALID JSON\r\n"


@pytest.mark.parametrize("finish", ["length", "stop"])
def test_provider_capture_before_envelope_validation(tmp_path, finish):
    from lexradar.auditors.provider import HTTPReply, OpenRouterProvider, ProviderError

    text = ' { "unparsed": true }\r\n'

    class Transport:
        def post(self, payload, key, timeout, max_bytes):
            return HTTPReply(
                200,
                json.dumps(
                    {
                        "model": profile().auditor_a.model,
                        "choices": [{"finish_reason": finish, "message": {"content": text}}],
                        "usage": {"prompt_tokens": "invalid"},
                    }
                ).encode(),
            )

    # Construct with a synthetic credential; never inspect environment or key files.
    provider = object.__new__(OpenRouterProvider)
    provider._key = "synthetic-test-credential"
    provider.config = profile()
    provider.compact_paid = False
    provider.transport = Transport()
    provider.audit_capture = Capture(tmp_path / "capture")
    with pytest.raises(ProviderError):
        provider.complete(profile().auditor_a, [{"role": "user", "content": "test"}])
    assert (tmp_path / "capture/original.txt").read_bytes() == text.encode()
    assert "synthetic-test-credential" not in "".join(
        p.read_text() for p in (tmp_path / "capture").iterdir()
    )
