"""One grounded material observation prepares a proposal, without legal approval."""

import json

import pytest
from test_approved_proposal_084 import mask_fields
from test_operator_audit_102 import case, dossier, no_network, ready, review_for  # noqa: F401

from lexradar import client_proposal
from lexradar.batch import snapshot
from lexradar.finding_assessment import evaluate
from lexradar.operator_audit import audit_main, finalize


def test_one_fact_generates_proposal_without_review_or_legal_proof(case, tmp_path):  # noqa: F811
    _, packet, result, report = case
    f = result.findings[0]
    f.status = "potential"
    f.legal_position = "undetermined"
    f.additional_checks = ["Проверить форму записи и подготовить инструкцию вебмастеру."]
    f.proposed_work = ["webmaster_instructions"]
    out = tmp_path / "mvp"
    out.mkdir()
    template = client_proposal.APPROVED_TEMPLATE.read_bytes()
    assert evaluate(result, packet, report)["outcome"] == "HOLD"
    decision = finalize(report, result, packet, out, commercial_mvp=True, replayed=True)
    assert decision["outcome"] == "GO"
    assert decision["allowed_findings"] == [f.id]
    row = decision["findings"][0]
    assert row["operator_review"] is None
    assert row["legal_status"] == "UNVERIFIED" and not row["legal_violation_proven"]
    assert row["commercial_offer"]["services"] == ["webmaster_instructions"]
    page = (out / "proposal.html").read_text()
    assert f.fact in page and "ТЗ вебмастеру" in page
    assert f.additional_checks[0] not in page
    assert "Нарушение не установлено" not in page
    cards = page.split('<ol class="findings editable" contenteditable="true">')[1].split("</ol>")[0]
    assert cards.count("<li><strong>") == 1
    assert mask_fields(page) == (
        mask_fields(template.decode().replace("LexRadar · КП v2.2", "LexRadar · КП v2.4", 1))
    )
    assert client_proposal.APPROVED_TEMPLATE.read_bytes() == template
    summary = json.loads((out / "summary.json").read_text())
    assert summary["commercial_mode"] == "mvp"
    assert summary["new_ai_calls"] == 0 and summary["new_ai_cost_usd"] == "0"
    assert not summary["automatic_send_allowed"] and not summary["client_release_allowed"]
    assert summary["manual_approval_required"] and summary["manual_approval"] is None
    assert not summary["sent"]
    justification = json.loads((out / "commercial-justification.json").read_text())
    item = justification["findings"][0]
    assert item["factual_scope"] == f.fact
    assert item["risk_reason"] and item["service_description"] and item["deliverables"]
    assert "Результат для клиента" in (out / "commercial-justification.md").read_text()
    assert "Проверить подготовленное КП" in (out / "report.md").read_text()


@pytest.mark.parametrize(
    "change",
    [
        "no_quote",
        "wrong_source",
        "bad_offsets",
        "paraphrase",
        "absence",
        "immaterial",
        "no_service",
        "no_work",
        "blank_work",
        "positive",
        "positive_declaration",
        "rejected",
        "unverifiable",
        "failed_audit",
    ],
)
def test_mvp_holds_without_grounded_material_service(case, tmp_path, change):  # noqa: F811
    _, packet, result, report = case
    f = result.findings[0]
    if change == "no_quote":
        f.text_grounding = []
    elif change == "wrong_source":
        f.text_grounding[0].evidence_id = "other-page"
    elif change == "bad_offsets":
        f.text_grounding[0].start = 999999
    elif change == "paraphrase":
        f.fact = "A paraphrase of a confirmed fragment is not a fully confirmed fact."
    elif change == "absence":
        f.fact_assertion = "absent"
    elif change == "immaterial":
        f.material = False
    elif change == "no_service":
        f.proposed_work = []
    elif change == "no_work":
        f.additional_checks.clear()
    elif change == "blank_work":
        f.additional_checks = [" " * 30]
    elif change == "positive":
        f.legal_position = "no_issue"
    elif change == "positive_declaration":
        f.status = "confirmed"
        f.legal_position = "undetermined"
    elif change in {"rejected", "unverifiable"}:
        f.status = change
    else:
        report.runs[0].status = "provider_error"
        report.completed = False
    out = tmp_path / change
    out.mkdir()
    decision = finalize(report, result, packet, out, commercial_mvp=True)
    assert decision["outcome"] == "HOLD"
    assert decision["allowed_findings"] == []
    assert not (out / "proposal.html").exists()
    assert "no_eligible_commercial_mvp_finding" in decision["reasons"]


def test_mvp_does_not_override_explicit_review(case):  # noqa: F811
    _, packet, result, report = case
    review = review_for(packet, result, report, service_need_verified=False)
    assert evaluate(result, packet, report, review, commercial_mvp=True)["outcome"] == "HOLD"
    review.findings[0].service_need_verified = True
    assert evaluate(result, packet, report, review, commercial_mvp=True)["outcome"] == "GO"


def test_mvp_cli_replay_never_calls_provider_or_changes_sources(case, tmp_path, monkeypatch):  # noqa: F811
    root, _, _, _ = case
    audit = tmp_path / "analysis"
    before = snapshot(audit), snapshot(root)
    monkeypatch.setattr(
        "lexradar.operator_audit.DeepSeekAdapter", lambda: pytest.fail("No paid provider")
    )
    out = tmp_path / "cli-mvp"
    audit_main(
        [
            "https://clinic.example/",
            "--dossier",
            str(root),
            "--saved-audit",
            str(audit),
            "--output",
            str(out),
            "--commercial-mvp",
        ]
    )
    summary = json.loads((out / "summary.json").read_text())
    assert summary["outcome"] == "GO" and summary["commercial_mode"] == "mvp"
    assert summary["new_ai_calls"] == 0
    assert (out / "proposal.html").is_file()
    assert (snapshot(audit), snapshot(root)) == before


def hypotheses(result):
    base = result.findings[0]
    rows = []
    for i, quote in enumerate(["Synthetic", "public", "form", "public form", "Synthetic public"]):
        f = base.model_copy(deep=True)
        f.id = f"extra-{i}"
        f.topic = "special_categories"
        f.fact = f"FABRICATED_FACT_{i}"
        f.subject = f"FABRICATED_ACCUSATION_{i}"
        f.legal_interpretation = f"FABRICATED_LEGAL_CLAIM_{i}"
        f.additional_checks = [f"FABRICATED_WORK_{i}"]
        f.text_grounding[0].exact_quote = quote
        f.text_grounding[0].start = f.text_grounding[0].end = None
        rows.append(f)
    return rows


def test_primary_plus_one_brief_medical_question(case, tmp_path):  # noqa: F811
    _, packet, result, report = case
    result.findings.extend(hypotheses(result))
    out = tmp_path / "with-hypotheses"
    out.mkdir()
    decision = finalize(report, result, packet, out, commercial_mvp=True)
    assert decision["outcome"] == "GO"
    assert decision["supporting_findings"] == ["extra-0"]
    assert len(decision["proposal_findings"]) == 2
    for row in decision["findings"][1:2]:
        assert row["fact_status"] == "PARTIAL" and row["proposal_role"] == "hypothesis"
        assert not row["commercial_eligible"] and not row["legal_violation_proven"]
        assert row["operator_review"] is None
    page = (out / "proposal.html").read_text()
    assert page.count("<strong>Дополнительный вопрос по медицинским данным</strong>") == 1
    assert "FABRICATED" not in page
    assert "Нарушение независимо проверено" not in page
    justification = json.loads((out / "commercial-justification.json").read_text())
    assert len(justification["findings"]) == 2
    assert all(not row["legal_violation_proven"] for row in justification["findings"])


def test_observed_hypotheses_alone_never_give_go(case, tmp_path):  # noqa: F811
    _, packet, result, report = case
    result.findings = hypotheses(result)
    out = tmp_path / "hypotheses-only"
    out.mkdir()
    decision = finalize(report, result, packet, out, commercial_mvp=True)
    assert decision["outcome"] == "HOLD"
    assert decision["proposal_findings"] == decision["supporting_findings"] == []
    assert not (out / "proposal.html").exists()


def test_rejected_hypotheses_and_bad_quotes_never_reach_proposal(case, tmp_path):  # noqa: F811
    _, packet, result, report = case
    extras = hypotheses(result)
    extras[0].text_grounding[0].exact_quote = "FABRICATED_QUOTE"
    extras[1].status = "rejected"
    extras[2].legal_position = "no_issue"
    result.findings.extend(extras[:3])
    out = tmp_path / "excluded"
    out.mkdir()
    decision = finalize(report, result, packet, out, commercial_mvp=True)
    assert decision["outcome"] == "GO" and decision["supporting_findings"] == []
    assert "FABRICATED" not in (out / "proposal.html").read_text()


def test_cli_has_no_unapproved_sending_path(tmp_path):
    out = tmp_path / "cannot-send"
    with pytest.raises(SystemExit) as error:
        audit_main(
            [
                "https://clinic.example/",
                "--output",
                str(out),
                "--commercial-mvp",
                "--send",
            ]
        )
    assert error.value.code == 2
    assert not out.exists()
