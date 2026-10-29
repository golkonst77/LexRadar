"""Synthetic normative text and actual saved bytes; no website, LLM or legal release."""

import json
import socket
from datetime import date

import pytest
from pydantic import ValidationError
from test_legal_sources import RAW, payload
from test_primary import EXAMPLES, NOW, make_dossier, run
from test_qualification import FORM, POLICY

from lexradar.approval import send_client_message
from lexradar.decision import decide_production
from lexradar.primary.qualification import qualify_primary
from lexradar.verifier.legal_cards import card_hash
from lexradar.verifier.models import SourceRegistry
from lexradar.verifier.requirement_cli import normative_main
from lexradar.verifier.requirement_models import NormativeVerification, RequirementApplicability
from lexradar.verifier.requirements import export_normative, verify_normative

EVENT = date(2026, 10, 10)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Normative MVP is offline")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr("lexradar.collector.network.Fetcher.get", blocked)
    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", blocked)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", blocked)


def setup(tmp_path, body=POLICY + FORM, *, updates=None, conditions=None, **primary_kwargs):
    root, _ = make_dossier(tmp_path, body)
    primary = run(root, **primary_kwargs)
    assessment = qualify_primary(primary)
    norm_root = tmp_path / "norms"
    norm_root.mkdir()
    (norm_root / "original.txt").write_bytes(RAW)
    data = {
        "cards": [payload(**(updates or {}))],
        "requirements": [
            {
                "requirement_id": "synthetic-requirement",
                "source_card_id": "synthetic-v1",
                "categories": list("ABCDEFGH"),
                "activity_scope": "personal_data",
                "conditions": [
                    {"condition_id": p, "predicate": p, "rationale": "SYNTHETIC condition, not law"}
                    for p in (conditions or ["static_form_declared", "operator_identity"])
                ],
                "necessary_evidence": ["SYNTHETIC: actual obligated person and actual processing"],
                "exceptions_and_alternatives": [
                    "SYNTHETIC: exceptions and alternative bases require review"
                ],
                "limitations": [
                    "SYNTHETIC ONLY, no normative interpretation or official provenance"
                ],
            }
        ],
    }
    registry = norm_root / "registry.json"
    registry.write_text(json.dumps(data), encoding="utf-8")
    return root, primary, assessment, registry


def verify(args, **kwargs):
    result = verify_normative(*args, event_date=EVENT, evaluated_at=NOW, **kwargs)
    assert result.production_outcome == "HOLD" and result.legal_status == "not_confirmed"
    assert not result.client_release_allowed and not result.production_go_allowed
    assert not result.automatic_send_allowed and not result.legal_research_completed
    assert not result.sanctions
    return result


def test_exact_saved_text_is_not_official_or_current_norm(tmp_path):
    result = verify(setup(tmp_path))
    r = result.requirements[0]
    assert r.text_check == "matched_saved_text" and r.actual_sha256 == r.expected_sha256
    assert r.official_origin == "unverified" and r.revision_check == "revision_unverified"
    assert not r.legal_requirement_confirmed
    assert all(a.status == "source_unverified" for a in result.applicability)
    assert any(
        c.status == "supported_static_fact"
        for a in result.applicability
        for c in a.condition_checks
    )
    assert all(not a.obligated_person_established for a in result.applicability)


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("missing", "missing"),
        ("quote", "contradictory"),
        ("hash", "contradictory"),
        ("empty", "incomplete"),
        ("unsafe", "missing"),
    ],
)
def test_missing_false_or_changed_source(tmp_path, mutation, expected):
    args = setup(tmp_path)
    registry = args[-1]
    data = json.loads(registry.read_text())
    if mutation == "missing":
        (registry.parent / "original.txt").unlink()
    elif mutation == "quote":
        data["cards"][0]["norm_text"] = "Fabricated provision absent from saved bytes"
    elif mutation == "hash":
        data["cards"][0]["text_sha256"] = "0" * 64
    elif mutation == "empty":
        data["cards"][0]["norm_text"] = " "
    else:
        data["cards"][0]["original_path"] = "../outside.txt"
    registry.write_text(json.dumps(data))
    result = verify(args)
    assert result.requirements[0].text_check == expected
    assert all(a.status == "source_unverified" for a in result.applicability)


@pytest.mark.parametrize(
    "updates,temporal,state",
    [
        ({"effective_until": "2025-12-31"}, "expired", "stale"),
        (
            {"effective_from": "2027-01-01", "effective_until": None},
            "not_yet_effective",
            "available_local_unverified",
        ),
        ({"effective_from": None}, "ambiguous", "available_local_unverified"),
        ({"checked_at": None}, "within_declared_interval", "stale"),
    ],
)
def test_declared_dates_do_not_confirm_revision(tmp_path, updates, temporal, state):
    result = verify(setup(tmp_path, updates=updates))
    r = result.requirements[0]
    assert r.declared_temporal_state == temporal and r.source_state == state
    assert r.revision_check == "revision_unverified"
    assert all(a.norm_in_force == "unverified" for a in result.applicability)


def test_overlapping_editions_no_latest_fallback(tmp_path):
    args = setup(tmp_path)
    registry = args[-1]
    data = json.loads(registry.read_text())
    other = dict(data["cards"][0], id="synthetic-v2", revision="Different declared edition")
    data["cards"].append(other)
    registry.write_text(json.dumps(data))
    result = verify(args)
    assert len(result.requirements) == 2
    assert all(r.source_state == "contradictory" for r in result.requirements)
    assert all(r.declared_temporal_state == "ambiguous" for r in result.requirements)


def test_medical_word_and_sector_hint_not_provider_proof(tmp_path):
    args = setup(
        tmp_path,
        "<p>Врач упомянут в немедицинском контексте.</p>" + FORM,
        conditions=["medical_activity", "medical_provider_identity"],
        sector="unknown",
    )
    result = verify(args)
    assert all(c.status == "unknown" for a in result.applicability for c in a.condition_checks)
    assert all(a.activity_supported == "unknown" for a in result.applicability)
    assert all(
        a.conditional_factual_support == "applicability_unknown" for a in result.applicability
    )


def test_two_organizations_do_not_establish_operator_or_merge_roles(tmp_path):
    result = verify(
        setup(
            tmp_path,
            "<p>ООО «Синтетическая клиника»</p><p>Образование: ООО «Синтетическое обучение»</p>",
            conditions=[
                "organization_mention",
                "multiple_organization_mentions",
                "operator_identity",
            ],
        )
    )
    assert sum(q["category"] == "A" for q in result.question_summaries) == 2
    assert sum(q["category"] == "H" for q in result.question_summaries) == 1
    assert all(not a.obligated_person_established for a in result.applicability)
    assert all(
        c.status == "unknown"
        for a in result.applicability
        for c in a.condition_checks
        if c.predicate == "operator_identity"
    )


@pytest.mark.parametrize(
    "body",
    [
        FORM,
        '<form><input name="phone"></form>',
        '<form><input name="phone"></div><a href="/policy.pdf">Политика</a></form>',
        '<form><a href="/policy.pdf">Политика</a></form>',
        POLICY + '<form><input name="phone"></form>',
    ],
)
def test_partial_policy_checkbox_and_scope_never_prove_absence(tmp_path, body):
    result = verify(
        setup(
            tmp_path,
            body,
            conditions=["scope_coverage", "document_deficiency", "whole_current_document_read"],
        )
    )
    for a in result.applicability:
        assert (
            not a.absence_check.absence_proven
            and not a.absence_check.sufficient_coverage_for_absence
        )
        assert a.absence_check.website_denominator is None
        assert all(c.status == "unknown" for c in a.condition_checks)
    assert all(
        not w.necessity_confirmed and w.new_document_needed == "unknown"
        for w in result.commercial_work
    )


@pytest.mark.parametrize(
    "body,predicate",
    [
        ('<a href="https://booking.medflex.ru/">Запись</a>', "actual_third_party_transfer"),
        ('<script src="https://www.googletagmanager.com/gtm.js"></script>', "cookies_observed"),
    ],
)
def test_medflex_gtm_do_not_establish_actual_actions(tmp_path, body, predicate):
    result = verify(
        setup(tmp_path, body, conditions=[predicate, "actual_personal_data_processing"])
    )
    assert result.applicability
    assert all(c.status == "unknown" for a in result.applicability for c in a.condition_checks)
    assert all(not a.factual_action_established for a in result.applicability)


def test_advertising_communication_not_established_from_marker(tmp_path):
    result = verify(
        setup(
            tmp_path,
            '<form><input name="email"><p>Согласие на рекламную рассылку.</p></form>',
            conditions=[
                "actual_advertising_communication",
                "alternative_legal_basis_checked",
                "exceptions_checked",
            ],
        )
    )
    assert all(c.status == "unknown" for a in result.applicability for c in a.condition_checks)
    assert all(not a.exceptions_checked for a in result.applicability)


def test_paid_document_need_not_inherited(tmp_path):
    result = verify(setup(tmp_path))
    assert result.commercial_work
    assert all(
        not w.normative_requirement_confirmed
        and not w.requirement_applicable_confirmed
        and w.new_document_needed == w.correction_needed == w.technical_change_only == "unknown"
        for w in result.commercial_work
    )


def test_forged_paid_type_cannot_receive_conditional_normative_support(tmp_path):
    args = setup(tmp_path, conditions=["static_form_declared"])
    args[2].commercial_work[0].work_type = "advertising_consent"
    result = verify(args)
    assert result.commercial_work[0].internal_recommendation == "insufficient_normative_basis"


def test_forged_extra_organization_does_not_support_multiple_roles(tmp_path):
    from lexradar.primary.qualification_models import ReviewObject

    args = setup(
        tmp_path,
        "<p>ООО «Синтетическая организация»</p>",
        conditions=["multiple_organization_mentions"],
    )
    args[2].legal_findings[0].objects.append(ReviewObject(type="organization", source_id="forged"))
    result = verify(args)
    assert all(c.status == "unknown" for a in result.applicability for c in a.condition_checks)


def test_missing_card_is_gap_not_invented_norm(tmp_path):
    args = setup(tmp_path)
    registry = args[-1]
    data = json.loads(registry.read_text())
    data["cards"] = []
    registry.write_text(json.dumps(data))
    r = verify(args).requirements[0]
    assert r.source_state == "missing" and r.exact_quote is None and r.provision is None


def test_empty_registry_yields_search_tasks_not_fictional_provisions(tmp_path):
    args = setup(tmp_path)
    args[-1].write_text('{"cards": [], "sources": []}')
    result = verify(args)
    assert not result.requirements and not result.applicability
    assert result.question_summaries and result.source_search_tasks
    assert all(
        t["quote"] is None and t["provision"] is None and not t["source_verified"]
        for t in result.source_search_tasks
    )
    assert all(
        w.internal_recommendation == "insufficient_normative_basis" for w in result.commercial_work
    )


def test_source_change_during_normative_checks_stops_output(tmp_path, monkeypatch):
    import lexradar.verifier.requirements as module

    args = setup(tmp_path)
    original = module.assess_registry
    calls = []

    def mutate(*values):
        result = original(*values)
        calls.append(1)
        if len(calls) == 1:
            (args[-1].parent / "original.txt").write_bytes(b"Changed normative source")
        return result

    monkeypatch.setattr(module, "assess_registry", mutate)
    with pytest.raises(ValueError, match="Saved normative source changed"):
        verify(args)


def test_authenticated_intake_flag_is_not_editable_trust(tmp_path):
    args = setup(tmp_path)
    value = json.loads(args[-1].read_text())
    card = SourceRegistry.model_validate(value).cards[0]
    value["source_intakes"] = [
        {
            "source_card_id": card.id,
            "original_sha256": card.text_sha256,
            "card_sha256": card_hash(card),
            "acquired_at": "2026-10-01T00:00:00Z",
            "reviewed_at": "2026-10-02T00:00:00Z",
            "reviewer": "Synthetic human",
            "authenticated": True,
        }
    ]
    with pytest.raises(ValidationError):
        SourceRegistry.model_validate(value)


def test_manual_intake_bound_hash_stays_untrusted_and_change_invalidates(tmp_path):
    args = setup(tmp_path)
    registry = args[-1]
    value = json.loads(registry.read_text())
    card = SourceRegistry.model_validate(value).cards[0]
    value["source_intakes"] = [
        {
            "source_card_id": card.id,
            "original_sha256": card.text_sha256,
            "card_sha256": card_hash(card),
            "acquired_at": "2026-10-01T00:00:00Z",
            "reviewed_at": "2026-10-02T00:00:00Z",
            "reviewer": "Synthetic reviewer",
        }
    ]
    registry.write_text(json.dumps(value))
    assert verify(args).requirements[0].intake_status == "untrusted"
    value["source_intakes"][0]["original_sha256"] = "0" * 64
    registry.write_text(json.dumps(value))
    assert verify(args).requirements[0].intake_status == "invalidated"


def test_determinism_and_old_registry_compatible(tmp_path):
    args = setup(tmp_path)
    before = [args[1].model_dump_json(), args[2].model_dump_json(), args[-1].read_bytes()]
    first = verify(args)
    assert verify(args).model_dump_json() == first.model_dump_json()
    assert before == [args[1].model_dump_json(), args[2].model_dump_json(), args[-1].read_bytes()]
    old_data = json.loads((EXAMPLES / "legal-sources/registry.json").read_bytes())
    old_data.pop("requirements", None)
    old_data.pop("source_intakes", None)
    old = SourceRegistry.model_validate(old_data)
    assert not old.requirements and not old.source_intakes


@pytest.mark.parametrize(
    "field,value",
    [
        ("production_outcome", "GO"),
        ("legal_status", "confirmed_violation"),
        ("production_go_allowed", True),
        ("client_release_allowed", True),
        ("automatic_send_allowed", True),
        ("sanctions", ["Unsupported fine"]),
    ],
)
def test_no_legal_promotion_or_client_release(tmp_path, field, value):
    result = verify(setup(tmp_path))
    data = result.model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        NormativeVerification.model_validate(data)
    row = result.applicability[0].model_dump()
    row["status"] = "conditions_supported"
    with pytest.raises(ValidationError):
        RequirementApplicability.model_validate(row)


def test_cli_exports_internal_without_touching_previous_reports(tmp_path):
    from lexradar.primary.cli import write_report
    from lexradar.verifier.offline import export_verification, verify_saved_assessment

    root, primary, assessment, registry = setup(tmp_path)
    old = tmp_path / "primary"
    write_report(primary, old, root)
    verified = tmp_path / "verified"
    export_verification(verify_saved_assessment(root, primary, assessment), verified, root)
    before = {str(p): p.read_bytes() for folder in (old, verified) for p in folder.glob("*.json")}
    out = tmp_path / "normative"
    normative_main(
        [
            str(root),
            "--primary",
            str(old / "audit.json"),
            "--assessment",
            str(old / "internal-assessment.json"),
            "--registry",
            str(registry),
            "--event-date",
            EVENT.isoformat(),
            "--as-of",
            NOW.isoformat(),
            "--output",
            str(out),
        ]
    )
    assert len(before) == 19 and all(
        p.read_bytes() == before[str(p)]
        for folder in (old, verified)
        for p in folder.glob("*.json")
    )
    assert len(list(out.glob("*.json"))) == 3
    for p in out.glob("*.json"):
        data = json.loads(p.read_text())
        assert data["audience"] == "internal_only" and not data["client_release_allowed"]
    decision = decide_production(
        EXAMPLES / "analysis-dossier",
        EXAMPLES / "decision-finding.json",
        EXAMPLES / "legal-registry.json",
        imported_report=out / "requirement-applicability.json",
    )
    assert decision.outcome == "HOLD"
    with pytest.raises(PermissionError):
        send_client_message(decision)
    with pytest.raises(ValueError):
        export_normative(verify((root, primary, assessment, registry)), root / "out", root)
