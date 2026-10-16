import csv
import json
import socket
from decimal import Decimal

import pytest

from lexradar.auditors.budget import Budget, BudgetExceeded
from lexradar.auditors.evidence import load_packet
from lexradar.auditors.provider import OpenRouterTransport
from lexradar.mvp_batch import read_csv, run
from lexradar.mvp_demo import create_inputs
from lexradar.single_auditor import MODEL, messages_for, profile


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No network or paid requests permitted")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(OpenRouterTransport, "post", forbidden)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


@pytest.fixture
def inputs(tmp_path):
    return create_inputs(tmp_path / "inputs")


@pytest.mark.parametrize("delimiter", [",", ";"])
@pytest.mark.parametrize("bom", [False, True])
def test_csv_extra_empty_dedup(tmp_path, delimiter, bom):
    path = tmp_path / "input.csv"
    path.write_text(
        delimiter.join(["url", "company_name", "email", "extra"])
        + "\n"
        + delimiter.join(["https://one.example", "", "", "value"])
        + "\n"
        + delimiter.join(["https://one.example/", "Other", "", ""])
        + "\n",
        encoding="utf-8-sig" if bom else "utf-8",
    )
    rows = read_csv(path)
    assert rows[0]["input"]["extra"] == "value"
    assert rows[1]["status"] == "SKIPPED"


def test_bad_rows_are_isolated(tmp_path):
    path = tmp_path / "input.csv"
    path.write_text(
        "url,company_name,email\n,Empty,\nhttps://ok.example/,Good,\n"
        "https://long.example/,Many,,extra\nhttps://short.example/\n"
    )
    assert [r["status"] for r in read_csv(path)] == ["ERROR", "pending", "ERROR", "ERROR"]


@pytest.mark.parametrize("content", ["", "url,email\n", "url,company_name,email,email\n"])
def test_bad_headers(tmp_path, content):
    path = tmp_path / "bad.csv"
    path.write_text(content)
    with pytest.raises(ValueError):
        read_csv(path)


def test_demo_end_to_end(inputs, tmp_path):
    root = tmp_path / "results"
    state = run(inputs, root)
    assert state["summary"] == {"SUCCESS": 3, "ERROR": 1, "SKIPPED": 1}
    assert [r.get("qualification") for r in state["rows"][:4]] == ["GO", "GO", "HOLD", "HOLD"]
    proposals = list(root.rglob("proposal.html"))
    assert len(proposals) == 2
    assert not state["automatic_send_allowed"]
    for row in state["rows"][:2]:
        text = (root / row["proposal"]).read_text()
        assert row["input"]["company_name"] in text and row["url"] in text
        assert "01.11.2026 включительно" in text
        report = json.loads((root / row["directory"] / "analysis/analysis.json").read_text())
        assert len(report["runs"]) == 1 and report["runs"][0]["model"] == MODEL
        assert all(not log["request_sent"] for log in report["logs"])
        assert report["budget_limit_usd"] == "0.50"
    assert (root / "results.csv").is_file() and (root / "report.md").is_file()


def rewrite(path, transform):
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig"), delimiter=";"))
    transform(rows)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def test_personalization_escaping(inputs, tmp_path):
    rewrite(inputs, lambda rows: rows[0].update(company_name='<script>alert("x")</script>'))
    root = tmp_path / "escape"
    state = run(inputs, root)
    text = (root / state["rows"][0]["proposal"]).read_text()
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "demo-1.example" not in text


def test_resume_no_repeat_and_integrity(inputs, tmp_path):
    root = tmp_path / "resume"
    state = run(inputs, root)
    before = {str(p): p.read_bytes() for p in root.rglob("proposal.html")}
    resumed = run(inputs, root, resume=True)
    assert state["rows"] == resumed["rows"]
    assert before == {str(p): p.read_bytes() for p in root.rglob("proposal.html")}
    next(root.rglob("proposal.html")).write_text("tampered")
    with pytest.raises(ValueError, match="integrity"):
        run(inputs, root, resume=True)


def test_resume_interrupted_attempt(inputs, tmp_path):
    root = tmp_path / "interrupted"
    state = run(inputs, root)
    first = state["rows"][0]
    first["status"] = "pending"
    first.pop("files")
    (root / "manifest.json").write_text(json.dumps(state))
    resumed = run(inputs, root, resume=True)
    assert resumed["rows"][0]["status"] == "SUCCESS"
    assert "retry-1" in resumed["rows"][0]["directory"]


def test_resume_changed_input_rejected(inputs, tmp_path):
    root = tmp_path / "changed"
    run(inputs, root)
    inputs.write_text(inputs.read_text(encoding="utf-8-sig") + "\n")
    with pytest.raises(ValueError, match="changed"):
        run(inputs, root, resume=True)


def test_limit(inputs, tmp_path):
    state = run(inputs, tmp_path / "limited", max_sites=1)
    assert len([r for r in state["rows"] if r.get("directory")]) == 1
    assert state["summary"] == {"SUCCESS": 1, "SKIPPED": 4}


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(),
        dict(allow_paid_single=True),
        dict(allow_paid_single=True, allow_external_transfer=True),
        dict(
            allow_paid_single=True, allow_external_transfer=True, packet_approval="old", max_sites=2
        ),
    ],
)
def test_paid_gate_before_any_output(inputs, tmp_path, kwargs):
    with pytest.raises(PermissionError):
        run(inputs, tmp_path / "blocked", mode="paid", **kwargs)
    assert not (tmp_path / "blocked").exists()


def test_wrong_packet_approval_no_key_or_transport(inputs, tmp_path):
    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps(
            dict(
                packet_sha256="0" * 64,
                approved=True,
                contents_reviewed=True,
                reviewer="test",
                approved_at="2026-01-01T00:00:00Z",
            )
        )
    )
    state = run(
        inputs,
        tmp_path / "paid",
        mode="paid",
        max_sites=1,
        allow_paid_single=True,
        allow_external_transfer=True,
        packet_approval=approval,
    )
    assert state["rows"][0]["status"] == "NEEDS_APPROVAL"
    assert "PermissionError" in state["rows"][0]["reason"]
    assert not list((tmp_path / "paid").rglob("proposal.html"))


def test_model_and_budget(inputs):
    config = profile()
    assert MODEL == "deepseek/deepseek-chat" and config.single_auditor
    assert config.max_budget_usd == Decimal("0.50")
    packet = load_packet(inputs.parent / "dossier-0", config.max_input_bytes)
    messages, _, _ = messages_for("A", packet)
    with pytest.raises(BudgetExceeded):
        Budget(Decimal("0.000001")).reserve(config.auditor_a, messages)


def test_offline_screening_override_rejected(inputs, tmp_path):
    with pytest.raises(ValueError):
        run(inputs, tmp_path / "bad", mvp_no_screening=True)


def test_collection_requires_its_own_permission(inputs, tmp_path):
    with pytest.raises(PermissionError, match="allow-site-collection"):
        run(inputs, tmp_path / "prepare", mode="prepare")


def test_prepare_pipeline_with_local_collector(inputs, tmp_path, monkeypatch):
    import shutil

    import lexradar.collector

    calls = []

    def local_collect(url, output, *args, **kwargs):
        calls.append(url)
        index = int(url.split("demo-")[1].split(".")[0])
        shutil.copytree(inputs.parent / f"dossier-{index}", output)

    monkeypatch.setattr(lexradar.collector, "collect", local_collect)
    state = run(inputs, tmp_path / "prepared", mode="prepare", allow_site_collection=True)
    assert len(calls) == 4
    assert state["summary"] == {"SUCCESS": 4, "SKIPPED": 1}
    assert not list((tmp_path / "prepared").rglob("proposal.html"))
    assert all(row.get("qualification") == "HOLD" for row in state["rows"][:4])
    assert len(list((tmp_path / "prepared").rglob("packet.json"))) == 4


def test_paid_resume_never_repeats_request(inputs, tmp_path):
    root = tmp_path / "paid-resume"
    approval = tmp_path / "missing-approval.json"
    kwargs = dict(
        mode="paid",
        max_sites=1,
        allow_paid_single=True,
        allow_external_transfer=True,
        packet_approval=approval,
    )
    run(inputs, root, **kwargs)
    resumed = run(inputs, root, resume=True, **kwargs)
    assert resumed["rows"][0]["status"] == "NEEDS_APPROVAL"
    assert not list(root.rglob("paid-attempt.json"))


def test_manifest_identity_tampering_rejected(inputs, tmp_path):
    root = tmp_path / "identity"
    state = run(inputs, root)
    state["rows"][0]["directory"] = "../../outside"
    (root / "manifest.json").write_text(json.dumps(state))
    with pytest.raises(ValueError, match="Unsafe resume"):
        run(inputs, root, resume=True)


def test_dossier_symlink_rejected(inputs, tmp_path):
    (inputs.parent / "dossier-0/extra").symlink_to(inputs.parent / "response-0.json")
    state = run(inputs, tmp_path / "symlink")
    assert state["rows"][0]["status"] == "ERROR"
    assert state["rows"][1]["qualification"] == "GO"


def test_paid_single_uses_a_only_and_ignores_recorded_response(inputs, tmp_path, monkeypatch):
    from datetime import UTC, datetime

    import lexradar.mvp_batch as batch
    from lexradar.auditors.models import Usage
    from lexradar.auditors.provider import ProviderReply

    packet = load_packet(inputs.parent / "dossier-0", 200000)
    approval = tmp_path / "test-only-approval.json"
    approval.write_text(
        json.dumps(
            dict(
                packet_sha256=packet.sha256,
                approved=True,
                contents_reviewed=True,
                reviewer="synthetic test",
                approved_at=datetime.now(UTC).isoformat(),
            )
        )
    )
    response = json.loads((inputs.parent / "response-0.json").read_text())
    (inputs.parent / "response-0.json").write_text("not a recorded response")
    calls = []

    class LocalProvider:
        def complete(self, settings, messages):
            calls.append(settings.model)
            return ProviderReply(json.dumps(response), Usage(cost_usd=Decimal("0.001")))

    monkeypatch.setattr(batch, "DeepSeekAdapter", LocalProvider)
    root = tmp_path / "synthetic-paid-path"
    state = run(
        inputs,
        root,
        mode="paid",
        max_sites=1,
        allow_paid_single=True,
        allow_external_transfer=True,
        packet_approval=approval,
    )
    assert calls == [MODEL]
    assert state["rows"][0]["qualification"] == "HOLD"
    assert state["rows"][0]["reason"] == "independent_legal_review_required"
    assert state["rows"][0]["cost_usd"] == "0.001"
    assert not state["automatic_send_allowed"]
    assert sum(bool(row.get("proposal")) for row in state["rows"]) == 0
