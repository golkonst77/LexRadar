"""Real saved synthetic bytes, not browser access, legal attestations or client approval."""

import hashlib
import json

import pytest
from pydantic import ValidationError
from test_requirements import EVENT, setup, verify
from test_requirements import offline as offline

from lexradar.verifier.legal_cards import card_hash
from lexradar.verifier.models import SourceRegistry
from lexradar.verifier.requirements import export_normative

POLICY_TEXT = (
    "Политика обработки персональных данных. Оператор обрабатывает имя и телефон "
    "для ответа на запрос. Сведения о защите и условиях обработки опубликованы в этом блоке."
)
POLICY = f'<div id="popup__policy"><h2>Политика</h2><p>{POLICY_TEXT}</p></div>'


def add_context(args):
    path = args[-1]
    data = json.loads(path.read_text())
    card = SourceRegistry.model_validate(data).cards[0]
    raw = b"SYNTHETIC SPS editorial evidence, not official law or legal review"
    (path.parent / "sps.txt").write_bytes(raw)
    claim = {
        "source_card_id": card.id,
        "card_sha256": card_hash(card),
        "provision": card.provision,
        "event_date": EVENT.isoformat(),
        "provenance_method": "user_attested",
        "edition_basis": "secondary_SPS_consolidation",
        "declared_revision": card.revision,
        "review_scope": ["Only the named provision, not the whole act"],
        "claimed_result": "current_in_scope",
        "materials": [
            {
                "path": "sps.txt",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "role": "SYNTHETIC secondary edition evidence",
            }
        ],
        "limitations": ["Imported research, independently authenticated origin not established"],
    }
    data["research_contexts"] = [claim]
    path.write_text(json.dumps(data))
    return data


def test_closed_popup_is_a_document_not_live_accessibility(tmp_path):
    args = setup(
        tmp_path,
        '<span data-src="#popup__policy">Политика</span><div hidden>' + POLICY + "</div>",
        conditions=[
            "policy_text_available",
            "saved_policy_text_present",
            "live_policy_accessibility",
        ],
    )
    result = verify(args)
    observation = result.saved_policy_observations[0]
    assert observation.saved_text_present
    assert observation.reading_completeness == "complete"
    assert observation.live_interface_accessibility == observation.policy_sufficiency == "unknown"
    assert (
        observation.artifact_sha256
        == hashlib.sha256((args[0] / observation.artifact_path).read_bytes()).hexdigest()
    )
    assert observation.text_sha256 == hashlib.sha256(observation.text.encode()).hexdigest()
    assert observation.block.start.line <= observation.block.end_exclusive.line
    for row in result.applicability:
        checks = {c.predicate: c for c in row.condition_checks}
        assert checks["policy_text_available"].status == "supported_static_fact"
        assert checks["saved_policy_text_present"].fact_ids == [observation.id]
        assert checks["live_policy_accessibility"].status == "unknown"


def test_policy_not_tied_to_organization_finding_type(tmp_path):
    args = setup(
        tmp_path,
        "<p>ООО «Синтетический оператор»</p>" + POLICY,
        conditions=["policy_text_available"],
    )
    data = json.loads(args[-1].read_text())
    data["requirements"][0]["categories"] = ["A"]
    args[-1].write_text(json.dumps(data))
    result = verify(args)
    assert result.applicability
    assert all(
        c.status == "supported_static_fact"
        for a in result.applicability
        for c in a.condition_checks
    )


def test_nested_popup_wrappers_do_not_duplicate_a_document(tmp_path):
    result = verify(setup(tmp_path, "<div>Политика<div hidden>" + POLICY + "</div></div>"))
    assert len(result.saved_policy_observations) == 1
    assert result.saved_policy_observations[0].text.startswith("Политика Политика обработки")


@pytest.mark.parametrize("tag", ["script", "style", "template", "noscript", "head", "title"])
def test_excluded_policy_text_is_never_a_document(tmp_path, tag):
    result = verify(setup(tmp_path, f"<{tag}>{POLICY}</{tag}><p>Контакты</p>"))
    assert not result.saved_policy_observations


@pytest.mark.parametrize(
    "body",
    [
        "<p>Политика</p>",
        "<p>ООО «Синтетический оператор». " + POLICY_TEXT + "</p>",
        "<div>В биографии приведена цитата: " + POLICY_TEXT + "</div>",
    ],
)
def test_marker_or_incidental_organization_quote_is_insufficient(tmp_path, body):
    assert not verify(setup(tmp_path, body)).saved_policy_observations


@pytest.mark.parametrize("damage", ["</span>", "<script/>", "<div><p>suffix"])
def test_complete_local_block_does_not_upgrade_damaged_page(tmp_path, damage):
    result = verify(setup(tmp_path, POLICY + damage))
    observation = result.saved_policy_observations[0]
    assert observation.reading_completeness == "complete"
    assert observation.page_completeness == "partial"
    assert all(not a.absence_check.all_saved_text_complete for a in result.applicability)


@pytest.mark.parametrize(
    "body",
    [
        '<div id="policy"><p>' + POLICY_TEXT + "</p>",
        '<div id="policy"><p>' + POLICY_TEXT + "</span></p></div>",
        '<div id="policy"><p>' + POLICY_TEXT + "</p><script/>hidden</div>",
        '<div id="policy"><p>' + POLICY_TEXT + "x" * 60000 + "</p></div>",
    ],
)
def test_unclosed_damaged_and_truncated_blocks_are_partial(tmp_path, body):
    result = verify(setup(tmp_path, body))
    assert result.saved_policy_observations
    assert all(p.reading_completeness == "partial" for p in result.saved_policy_observations)
    assert all(p.block.limitations for p in result.saved_policy_observations)


def test_full_block_after_recovery_is_locally_complete(tmp_path):
    result = verify(setup(tmp_path, "<p>Начало</p></span>" + POLICY))
    assert result.saved_policy_observations[0].reading_completeness == "complete"
    assert result.saved_policy_observations[0].page_completeness == "partial"


def test_changed_site_artifact_is_rejected(tmp_path):
    args = setup(tmp_path, POLICY)
    original = args[0] / "artifacts/page-0001.html"
    original.write_bytes(original.read_bytes() + b"changed")
    result = verify(args)
    assert not result.saved_policy_observations
    assert not result.source_research_sha256


def test_fabricated_imported_organization_quote_cannot_create_policy(tmp_path):
    args = setup(
        tmp_path, "<p>ООО «Синтетический оператор»</p>", conditions=["policy_text_available"]
    )
    assessment = args[2]
    assessment.legal_findings[0].facts[0].citations[0].quote = POLICY_TEXT
    result = verify(args)
    assert not result.saved_policy_observations
    assert all(c.status == "unknown" for a in result.applicability for c in a.condition_checks)


def test_user_attestation_and_sps_context_roundtrip_without_trust(tmp_path):
    args = setup(tmp_path, POLICY)
    data = add_context(args)
    result = verify(args)
    context = result.requirements[0].research_context
    assert context.imported_claim.model_dump(mode="json") == data["research_contexts"][0]
    assert context.binding_status == "consistent"
    assert context.mechanism_operations
    assert not context.claim_authenticated and not context.independent_official_portal_verification
    assert context.production_trust == "untrusted"
    assert result.requirements[0].revision_check == "revision_unverified"
    export_normative(result, tmp_path / "exports", args[0])
    exported = json.loads((tmp_path / "exports/norm-requirements.json").read_text())
    assert exported["requirements"][0]["research_context"] == context.model_dump(mode="json")
    assert exported["saved_policy_observations"]


@pytest.mark.parametrize(
    "mutation", ["card", "provision", "date", "edition", "missing", "bytes", "path", "symlink"]
)
def test_context_binding_mutations_invalidate_not_promote(tmp_path, mutation):
    args = setup(tmp_path)
    data = add_context(args)
    claim = data["research_contexts"][0]
    if mutation == "card":
        data["cards"][0]["norm_text"] += "changed"
    elif mutation == "provision":
        claim["provision"] += "other"
    elif mutation == "date":
        claim["event_date"] = "2027-10-11"
    elif mutation == "edition":
        claim["declared_revision"] += "other"
    elif mutation == "missing":
        (args[-1].parent / "sps.txt").unlink()
    elif mutation == "bytes":
        (args[-1].parent / "sps.txt").write_bytes(b"changed")
    elif mutation == "path":
        claim["materials"][0]["path"] = "../sps.txt"
    else:
        target = args[-1].parent / "sps.txt"
        target.rename(target.with_name("real.txt"))
        target.symlink_to(target.with_name("real.txt"))
    args[-1].write_text(json.dumps(data))
    assert verify(args).requirements[0].research_context.binding_status == "invalidated"


@pytest.mark.parametrize(
    "field,value",
    [("authenticated", True), ("official_origin", "verified"), ("production_trust", "trusted")],
)
def test_editable_context_cannot_add_trust_fields(tmp_path, field, value):
    args = setup(tmp_path)
    data = add_context(args)
    data["research_contexts"][0][field] = value
    with pytest.raises(ValidationError):
        SourceRegistry.model_validate(data)


def test_unknown_context_reference_is_not_silently_dropped(tmp_path):
    args = setup(tmp_path)
    data = add_context(args)
    data["research_contexts"][0]["source_card_id"] = "missing"
    with pytest.raises(ValidationError):
        SourceRegistry.model_validate(data)


def test_research_material_changed_during_stage_is_rejected(tmp_path, monkeypatch):
    from lexradar.verifier import requirements

    args = setup(tmp_path, POLICY)
    add_context(args)
    original = requirements.policy_observations

    def changed(research):
        result = original(research)
        (args[-1].parent / "sps.txt").write_bytes(b"changed during stage")
        return result

    monkeypatch.setattr(requirements, "policy_observations", changed)
    with pytest.raises(ValueError, match="Research evidence changed"):
        verify(args)
