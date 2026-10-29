"""Synthetic legal cards never authenticate a law, reviewer or production decision."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from lexradar.decision import decide_production
from lexradar.verifier.legal_cards import (
    LegalSourceCard,
    SourceLegalReview,
    assess_card,
    card_hash,
    check_review,
    hash_bytes,
)
from lexradar.verifier.legal_source_cli import legal_source_main, prepare_card
from lexradar.verifier.models import SourceRegistry
from lexradar.verifier.registry import assess_registry, load_registry

NOW = datetime(2026, 10, 10, tzinfo=UTC)
EVENT = date(2026, 6, 1)
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
RAW = b"SYNTHETIC TEST ONLY, NOT A LAW. First synthetic provision.\r\n"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Legal-source workflow must not make network/LLM requests")

    monkeypatch.setattr("lexradar.collector.network.Fetcher.get", blocked)
    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", blocked)


def payload(**updates):
    value = {
        "id": "synthetic-v1",
        "act_id": "synthetic-act",
        "source_kind": "normative_act",
        "domain": "other",
        "act_name": "SYNTHETIC TEST ONLY, NOT A LAW",
        "act_number": "SYNTHETIC-1",
        "document_date": "2024-01-01",
        "provision": "Synthetic provision 1",
        "norm_text": "First synthetic provision.",
        "source_url": "https://pravo.gov.ru/synthetic-unverified-fixture",
        "publication_id": None,
        "publication_date": "2024-01-02",
        "revision": "Synthetic v1",
        "effective_from": "2024-02-01",
        "effective_until": "2026-12-31",
        "checked_at": "2026-10-01T00:00:00Z",
        "verification_method": "local_integrity",
        "text_sha256": hash_bytes(RAW),
        "original_path": "original.txt",
        "revision_check_basis": "Synthetic declaration only; not independent evidence",
        "limitations": ["SYNTHETIC; not official law"],
        "uncertainties": ["Origin unverified"],
        "source_availability": "saved_locally",
    }
    value.update(updates)
    return value


def save_registry(root, values):
    path = root / "registry.json"
    path.write_text(json.dumps({"schema_version": "0.4", "sources": [], "cards": values}))
    return path


@pytest.fixture
def card(tmp_path):
    (tmp_path / "original.txt").write_bytes(RAW)
    return LegalSourceCard.model_validate(payload())


def assert_hold(registry_path):
    decision = decide_production(
        EXAMPLES / "analysis-dossier", EXAMPLES / "decision-finding.json", registry_path
    )
    assert decision.outcome == "HOLD"
    assert not decision.client_release_allowed
    assert not decision.legal_research_completed


@pytest.mark.parametrize(
    "updates",
    [
        {"official": True},
        {"status": "current_confirmed"},
        {"reviewer": "Human", "verified": True},
        {"claimed_official": True, "claimed_status": "current_confirmed"},
        {"claimed_reviewer": "Human", "verification_method": "human_review"},
        {"verification_method": "llm", "claimed_status": "verified"},
        {"source_url": "https://publication.pravo.gov.ru/document/fake"},
        {"source_availability": "unavailable"},
        {"source_kind": "secondary_material", "claimed_official": True},
        {"source_kind": "official_explanation", "intended_use": "binding_norm"},
        {"source_kind": "judicial_act", "intended_use": "binding_norm"},
        {"source_kind": "ai_claim", "claimed_official": True},
        {"text_sha256": "a" * 64},
        {"original_path": "missing.txt"},
        {"effective_from": "2027-01-01", "effective_until": None},
        {"effective_until": "2025-12-31", "claimed_lifecycle": "repealed"},
        {"effective_from": None},
        {"effective_until": "2023-01-01"},
        {"transition_note": "Unknown transition scope"},
        {"norm_text": "Fabricated text not in saved original"},
    ],
)
def test_all_untrusted_claims_and_invalid_inputs_block_production(tmp_path, updates):
    (tmp_path / "original.txt").write_bytes(RAW)
    path = save_registry(tmp_path, [payload(**updates)])
    assert_hold(path)
    try:
        registry, _ = load_registry(path)
    except ValidationError:
        assert set(updates) & {"official", "status", "reviewer", "verified"}
        return
    report = assess_registry(registry, EVENT, NOW, 30)["synthetic-v1"]
    assert report.status == "unverified"
    assert not report.card_assessment.trusted
    assert report.card_assessment.authenticity == "unverified"
    assert report.card_assessment.applicability == "unestablished"
    assert report.card_assessment.factual_violation == "unestablished"
    assert report.reasons


def test_correct_local_hash_of_invented_law_only_confirms_integrity(card, tmp_path):
    result = assess_card(card, tmp_path, EVENT, NOW)
    assert result.technical_integrity == "consistent"
    assert result.status == "unverified"
    assert result.revision_confirmation == "unverified"
    assert result.legal_force == "unverified"
    assert result.temporal_claim == "within_declared_interval"


@pytest.mark.parametrize(
    "event,expected",
    [
        ("2023-12-31", "not_yet_effective"),
        ("2024-02-01", "within_declared_interval"),
        ("2026-12-31", "within_declared_interval"),
        ("2027-01-01", "expired"),
    ],
)
def test_event_dates_and_inclusive_boundaries(card, tmp_path, event, expected):
    result = assess_card(card, tmp_path, date.fromisoformat(event), NOW)
    assert result.temporal_claim == expected
    assert result.status == "unverified"


def test_two_revisions_no_latest_revision_fallback(tmp_path):
    (tmp_path / "original.txt").write_bytes(RAW)
    first = payload(effective_until="2025-12-31")
    second = payload(id="synthetic-v2", revision="Synthetic v2", effective_from="2026-01-01")
    path = save_registry(tmp_path, [first, second])
    registry, _ = load_registry(path)
    past = assess_registry(registry, date(2025, 1, 1), NOW, 30)
    current = assess_registry(registry, EVENT, NOW, 30)
    assert past["synthetic-v1"].card_assessment.temporal_claim == "within_declared_interval"
    assert past["synthetic-v2"].card_assessment.temporal_claim == "not_yet_effective"
    assert current["synthetic-v1"].card_assessment.temporal_claim == "expired"
    assert current["synthetic-v2"].card_assessment.temporal_claim == "within_declared_interval"
    assert all(r.status == "unverified" for r in [*past.values(), *current.values()])
    assert_hold(path)


@pytest.mark.parametrize(
    "changes",
    [
        {"effective_from": "2025-01-01"},
        {"effective_from": None},
        {"effective_from": "2026-01-01", "act_number": "CONTRADICTORY"},
    ],
)
def test_overlaps_and_identity_conflicts_even_outside_event(tmp_path, changes):
    (tmp_path / "original.txt").write_bytes(RAW)
    other = payload(id="synthetic-v2", revision="Synthetic v2", **changes)
    path = save_registry(tmp_path, [payload(), other])
    registry, _ = load_registry(path)
    results = assess_registry(registry, date(2020, 1, 1), NOW, 30)
    assert all(r.card_assessment.temporal_claim == "ambiguous" for r in results.values())
    assert all(r.card_assessment.technical_integrity == "invalid" for r in results.values())
    assert_hold(path)


@pytest.mark.parametrize(
    "event,expected",
    [
        ("2026-06-01", "not_yet_effective"),
        ("2026-07-01", "within_declared_interval"),
        ("2026-09-01", "expired"),
    ],
)
def test_transition_application_interval(tmp_path, event, expected):
    (tmp_path / "original.txt").write_bytes(RAW)
    card = LegalSourceCard.model_validate(
        payload(
            transition_from="2026-07-01",
            transition_until="2026-08-31",
            transition_note="Synthetic delayed application to a hypothetical activity",
        )
    )
    report = assess_card(card, tmp_path, date.fromisoformat(event), NOW)
    assert report.temporal_claim == expected
    assert any("Transition" in reason for reason in report.reasons)
    assert report.applicability == "unestablished"


def review_for(card):
    return SourceLegalReview(
        card_sha256=card_hash(card),
        original_sha256=card.text_sha256,
        event_date=EVENT,
        reviewer="Self-declared synthetic human",
        reviewed_at=NOW,
        independent_of_author=True,
        origin_checked=True,
        revision_checked=True,
        legal_force_checked=True,
        operator_reference="synthetic-operator",
        activity="synthetic-activity",
        applicability_rationale="Synthetic rationale, not verified expertise",
        confirmation_references=["Self-authored assertion"],
    )


def test_self_authored_legal_review_never_authenticated(card, tmp_path):
    result = assess_card(card, tmp_path, EVENT, NOW)
    review = review_for(card)
    assert check_review(review, card, result, NOW) == "untrusted"
    with pytest.raises(ValidationError):
        SourceLegalReview.model_validate({**review.model_dump(), "authenticated": True})


@pytest.mark.parametrize("mutation", ["original", "hash", "quote", "revision", "url", "period"])
def test_mutation_invalidates_prepared_legal_review(card, tmp_path, mutation):
    review = review_for(card)
    value = card.model_dump(mode="json")
    if mutation == "original":
        (tmp_path / "original.txt").write_bytes(b"SUBSTITUTED synthetic text")
    else:
        key, replacement = {
            "hash": ("text_sha256", "b" * 64),
            "quote": ("norm_text", "Changed quote"),
            "revision": ("revision", "changed"),
            "url": ("source_url", "https://pravo.gov.ru/"),
            "period": ("effective_from", "2025-01-01"),
        }[mutation]
        value[key] = replacement
    changed = LegalSourceCard.model_validate(value)
    result = assess_card(changed, tmp_path, EVENT, NOW)
    assert check_review(review, changed, result, NOW) == "invalidated"
    assert result.status == "unverified"


@pytest.mark.parametrize("path", ["../outside.txt", "/tmp/any-source.txt", "missing.txt"])
def test_unsafe_missing_original_not_confirmed(card, tmp_path, path):
    changed = LegalSourceCard.model_validate({**card.model_dump(), "original_path": path})
    assert assess_card(changed, tmp_path, EVENT, NOW).technical_integrity == "unavailable"


def test_symlink_original_rejected(card, tmp_path):
    (tmp_path / "linked.txt").symlink_to(tmp_path / "original.txt")
    changed = LegalSourceCard.model_validate({**card.model_dump(), "original_path": "linked.txt"})
    assert assess_card(changed, tmp_path, EVENT, NOW).technical_integrity == "unavailable"


def test_old_registry_has_no_automatic_trust():
    registry, _ = load_registry(EXAMPLES / "legal-registry.json")
    assert not registry.cards
    results = assess_registry(registry, EVENT, NOW, 30)
    assert all(r.status == "unverified" and r.card_assessment is None for r in results.values())
    assert_hold(EXAMPLES / "legal-registry.json")


def test_publication_identifier_without_url_supported(card, tmp_path):
    value = {**card.model_dump(), "source_url": None, "publication_id": "synthetic-id"}
    registry = SourceRegistry(cards=[LegalSourceCard.model_validate(value)])
    registry._source_root = tmp_path
    result = assess_registry(registry, EVENT, NOW, 30)[card.id]
    assert result.source_url is None and result.status == "unverified"


def test_prepare_imports_exact_bytes_and_requires_exact_quote(card, tmp_path):
    metadata = tmp_path / "metadata.json"
    value = card.model_dump(mode="json")
    for key in ("text_sha256", "original_path"):
        value.pop(key)
    metadata.write_text(json.dumps(value))
    out = tmp_path / "bundle"
    prepared = prepare_card(metadata, tmp_path / "original.txt", out, EVENT)
    assert (out / "original.txt").read_bytes() == RAW
    assert prepared.text_sha256 == hash_bytes(RAW)
    assert json.loads((out / "review.json").read_text())["trust_status"] == "untrusted"
    assert json.loads((out / "report.json").read_text())["status"] == "unverified"
    assert_hold(out / "registry.json")
    value["norm_text"] = "Absent exact quote"
    metadata.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="quote"):
        prepare_card(metadata, tmp_path / "original.txt", tmp_path / "bad-bundle", EVENT)
    assert not (tmp_path / "bad-bundle").exists()


def test_cli_check_and_review(card, tmp_path):
    registry = save_registry(tmp_path, [card.model_dump(mode="json")])
    check = tmp_path / "check"
    legal_source_main(
        ["check", "--registry", str(registry), "--event-date", str(EVENT), "--output", str(check)]
    )
    assert not json.loads((check / "report.json").read_text())["production_go_allowed"]
    submission = tmp_path / "submission.json"
    submission.write_text(review_for(card).model_dump_json())
    reviewed = tmp_path / "reviewed"
    legal_source_main(
        [
            "review",
            "--registry",
            str(registry),
            "--source-id",
            card.id,
            "--submission",
            str(submission),
            "--event-date",
            str(EVENT),
            "--output",
            str(reviewed),
        ]
    )
    report = json.loads((reviewed / "report.json").read_text())
    assert report["review_status"] == "untrusted" and not report["production_go_allowed"]


def test_duplicate_card_ids_and_legacy_collisions_rejected(card):
    with pytest.raises(ValidationError, match="Duplicate"):
        SourceRegistry(cards=[card, card])
    legacy, _ = load_registry(EXAMPLES / "legal-registry.json")
    conflicting = LegalSourceCard.model_validate({**card.model_dump(), "id": legacy.sources[0].id})
    with pytest.raises(ValidationError, match="Duplicate"):
        SourceRegistry(sources=legacy.sources, cards=[conflicting])


def test_source_not_supplied_root_never_claims_integrity(card):
    assert assess_card(card, None, EVENT, NOW).technical_integrity == "unavailable"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"publication_date": "2023-01-01"}, "predates document"),
        ({"checked_at": "2024-01-01T00:00:00Z"}, "predates claimed publication"),
        ({"effective_from": "2027-01-01", "effective_until": "2025-01-01"}, "Contradictory"),
        ({"transition_from": "2026-09-01", "transition_until": "2026-01-01"}, "Contradictory"),
        ({"norm_text": " "}, "empty/whitespace"),
    ],
)
def test_internal_contradictions_explained(tmp_path, changes, reason):
    (tmp_path / "original.txt").write_bytes(RAW)
    card = LegalSourceCard.model_validate(payload(**changes))
    result = assess_card(card, tmp_path, EVENT, NOW)
    assert result.technical_integrity == "invalid"
    assert any(reason in item for item in result.reasons)


def test_oversized_and_non_utf8_original(card, tmp_path):
    (tmp_path / "original.txt").write_bytes(b"x" * 6_000_001)
    assert assess_card(card, tmp_path, EVENT, NOW).technical_integrity == "unavailable"
    (tmp_path / "original.txt").write_bytes(b"\xff")
    assert assess_card(card, tmp_path, EVENT, NOW).technical_integrity == "unavailable"


def test_cli_prepare_dispatch(card, tmp_path, monkeypatch):
    from lexradar.cli import main

    metadata = tmp_path / "metadata.json"
    value = card.model_dump(mode="json")
    for key in ("text_sha256", "original_path"):
        value.pop(key)
    metadata.write_text(json.dumps(value))
    monkeypatch.setattr(
        "sys.argv",
        [
            "lexradar",
            "legal-source",
            "prepare",
            "--metadata",
            str(metadata),
            "--text",
            str(tmp_path / "original.txt"),
            "--event-date",
            str(EVENT),
            "--output",
            str(tmp_path / "prepared"),
        ],
    )
    main()
    assert (tmp_path / "prepared" / "registry.json").is_file()


def test_single_unavailable_source_retains_reason(card, tmp_path):
    changed = LegalSourceCard.model_validate(
        {**card.model_dump(), "source_availability": "unavailable"}
    )
    result = assess_card(changed, tmp_path, EVENT, NOW)
    assert result.technical_integrity == "consistent"
    assert result.authenticity == "unverified"
    assert any("source declared unavailable" in r for r in result.reasons)
