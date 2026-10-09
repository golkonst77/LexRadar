import pytest

from lexradar.gateway import decide
from lexradar.models import FindingStatus, Outcome, Signal


def test_confirmed_material(data):
    assert decide(data).outcome == Outcome.GO
    assert decide(data).eligible_finding_ids == ["f1"]


def test_insufficient_evidence(data):
    for a in data.auditors:
        a.findings[0].evidence_ids = []
    assert decide(data).outcome == Outcome.NURTURE


def test_conflicting_auditors(data):
    data.auditors[1].findings[0].status = FindingStatus.REJECTED
    assert decide(data).outcome == Outcome.HOLD


def test_unavailable_document(data):
    data.evidence[0].available = False
    assert decide(data).outcome == Outcome.NURTURE


@pytest.mark.parametrize("signal", list(Signal)[1:])
def test_technical_signal_alone(data, signal):
    data.evidence[0].signal = signal
    for a in data.auditors:
        a.findings[0].signal = signal
    assert decide(data).outcome == Outcome.NURTURE


def test_no_violation(data):
    for a in data.auditors:
        a.findings = []
    data.verification.findings = []
    decision = decide(data)
    assert decision.outcome == Outcome.NURTURE
    assert decision.reasons == ["no_confirmed_violation_no_outreach_basis"]


def test_rejected_finding(data):
    for a in data.auditors:
        a.findings[0].status = FindingStatus.REJECTED
    data.verification.findings[0].status = FindingStatus.REJECTED
    assert decide(data).outcome == Outcome.NURTURE


def test_critical_obstacle_has_priority(data):
    data.target.critical_obstacles = ["Processing prohibited"]
    data.verification.completed = False
    assert decide(data).outcome == Outcome.STOP


@pytest.mark.parametrize("field", ["contradictions", "uncertainties"])
def test_unresolved_review(data, field):
    setattr(data.verification, field, ["Unresolved"])
    assert decide(data).outcome == Outcome.HOLD


def test_incomplete(data):
    data.auditors[0].completed = False
    assert decide(data).outcome == Outcome.HOLD


def test_llm_cannot_promote(data):
    data.verification.findings[0].reviewer_kind = "llm"
    assert decide(data).outcome == Outcome.NURTURE


@pytest.mark.parametrize("field", ["evidence_checked", "legal_basis_checked"])
def test_checks_required(data, field):
    setattr(data.verification.findings[0], field, False)
    assert decide(data).outcome == Outcome.NURTURE


def test_pending_verification(data):
    data.verification.findings = []
    assert decide(data).outcome == Outcome.HOLD


def test_assumptions_prevent_go(data):
    for a in data.auditors:
        a.findings[0].assumptions = ["Unknown processing purpose"]
    assert decide(data).outcome == Outcome.NURTURE


def test_legal_disagreement(data):
    data.auditors[1].findings[0].legal_basis = "Different legal basis"
    assert decide(data).outcome == Outcome.HOLD


def test_deterministic_and_no_mutation(data):
    before = data.model_dump_json()
    assert decide(data) == decide(data)
    assert data.model_dump_json() == before


def test_identity_required(data):
    data.target.legal_entity.identity_verified = False
    assert decide(data).outcome == Outcome.HOLD


def test_missing_auditor(data):
    data.auditors.pop()
    assert decide(data).outcome == Outcome.HOLD


def test_coverage_disagreement(data):
    data.auditors[1].findings = []
    assert decide(data).outcome == Outcome.HOLD


def test_verifier_disagreement(data):
    data.verification.findings[0].status = FindingStatus.REJECTED
    assert decide(data).outcome == Outcome.HOLD


def test_low_confidence_does_not_override_human_review(data):
    for auditor in data.auditors:
        auditor.findings[0].confidence = 0
    assert decide(data).outcome == Outcome.GO
