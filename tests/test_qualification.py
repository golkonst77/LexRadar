"""Offline legal questions and commercial scopes never become legal/client approval."""

import json
import socket

import pytest
from pydantic import ValidationError
from test_primary import EXAMPLES, NOW, make_dossier, run

from lexradar.approval import send_client_message
from lexradar.decision import decide_production
from lexradar.primary.cli import write_report
from lexradar.primary.models import PrimaryAudit
from lexradar.primary.qualification import digest, preliminary_outline, qualify_primary
from lexradar.primary.qualification_models import (
    FindingKind,
    InternalAssessment,
    LegalFinding,
    PreliminaryClientSummary,
)

POLICY = "<div>Политика обработки персональных данных: цели обработки, категории данных и "
POLICY += "сроки должны сопоставляться с фактическими процессами оператора.</div>"
FORM = '<form><input name="phone"><p>Согласие на обработку персональных данных.</p></form>'


@pytest.fixture(autouse=True)
def no_actions(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Qualification is offline and cannot send or execute")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr("lexradar.collector.network.Fetcher.get", blocked)
    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", blocked)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", blocked)


def assess(tmp_path, body, **kwargs):
    root, _ = make_dossier(tmp_path, body, **kwargs)
    primary = run(root)
    result = qualify_primary(primary)
    return root, primary, result


def categories(result):
    return [f.category for f in result.legal_findings]


def safe(result):
    assert result.production_outcome == "HOLD" and result.legal_status == "not_confirmed"
    assert not result.client_release_allowed and not result.production_go_allowed
    assert not result.automatic_send_allowed and not result.legal_research_completed
    assert all(
        f.kind == FindingKind.REVIEW_QUESTION and not f.possible_legal_consequences
        for f in result.legal_findings
    )


def test_three_booking_links_create_one_question_no_paid_work(tmp_path):
    _, _, result = assess(
        tmp_path, '<a href="https://booking.medflex.ru/A?a=1&amp;b=2">Запись</a>' * 3
    )
    assert categories(result) == ["E"]
    finding = result.legal_findings[0]
    assert len(finding.facts) == 1 and len(finding.facts[0].declaration_ids) == 3
    assert len(finding.facts[0].citations) == 3
    assert not result.commercial_work
    safe(result)


def test_multiple_organizations_are_mentions_not_confirmed_operators(tmp_path):
    _, _, result = assess(
        tmp_path,
        "<p>ООО «Синтетическая организация»</p>"
        "<p>Образование врача: ООО «Синтетическое обучение»</p>",
    )
    assert categories(result) == ["A", "A", "H"]
    a = [f for f in result.legal_findings if f.category == "A"]
    assert all(len(f.objects) == 1 and len(f.facts) == 1 for f in a)
    assert a[0].objects[0].source_id != a[1].objects[0].source_id
    h = next(f for f in result.legal_findings if f.category == "H")
    assert len(h.facts) == 2
    assert not any(
        o.identity_or_operator_confirmed for f in result.legal_findings for o in f.objects
    )
    assert not result.commercial_work
    safe(result)


@pytest.mark.parametrize(
    "body",
    [
        '<form><input name="phone"></form>',
        '<form><input name="phone"><input type="checkbox" name="consent"></form>',
        '<form><input name="phone"><a href="/policy">Политика</a></form>',
        '<form><input name="phone"></div><input type="checkbox" name="not_inspected"></form>',
    ],
)
def test_absent_policy_checkbox_or_partial_html_not_work_or_violation(tmp_path, body):
    _, _, result = assess(tmp_path, body)
    assert {"A", "B", "C"} <= set(categories(result))
    assert not result.commercial_work
    b = next(f for f in result.legal_findings if f.category == "B")
    assert any("не означает отсутствие на сайте" in v for v in b.limitations)
    safe(result)


def test_gtm_medflex_and_maps_are_not_transfer_risk_confirmation(tmp_path):
    body = '<a href="https://booking.medflex.ru/">Запись</a>'
    head = '<script>j.src="https://www.googletagmanager.com/gtm.js?id="+i;</script>'
    head += '<script src="https://api-maps.yandex.ru/services/constructor/js/"></script>'
    _, _, result = assess(tmp_path, body, head=head)
    assert categories(result) == ["E", "F"]
    assert not result.commercial_work
    assert all(f.unconfirmed_assumptions and f.additional_checks for f in result.legal_findings)
    safe(result)


def test_policy_and_consent_support_conditional_documents_not_repair(tmp_path):
    _, _, result = assess(tmp_path, POLICY + FORM)
    work = {w.work_type: w for w in result.commercial_work}
    assert set(work) == {"policy_document", "processing_consent", "legal_webmaster_specification"}
    assert all(
        w.necessity_conditions
        and w.client_clarifications
        and not w.document_need_confirmed
        and not w.direct_site_fix_included
        and not w.offer_generated
        for w in work.values()
    )
    assert result.commercial_model.seller_and_payment_recipient == "ИП Голубев"
    assert result.commercial_model.sales_site == "golwebstudio.ru"
    safe(result)


def test_advertising_consent_separate_only_with_specific_form_context(tmp_path):
    body = (
        '<form><input name="email"><p>Согласие на обработку данных и рекламную рассылку.</p></form>'
    )
    _, _, result = assess(tmp_path, body)
    assert "D" in categories(result)
    assert "advertising_consent" in {w.work_type for w in result.commercial_work}
    safe(result)


def test_site_promotion_does_not_imply_advertising_messages(tmp_path):
    _, _, result = assess(tmp_path, '<p>Рекламная акция сайта</p><form><input name="phone"></form>')
    assert "D" not in categories(result)
    assert not result.commercial_work


def test_work_deduplicated_across_distinct_forms_with_scopes_preserved(tmp_path):
    _, _, result = assess(tmp_path, POLICY + FORM + FORM)
    policy = [w for w in result.commercial_work if w.work_type == "policy_document"]
    assert len(policy) == 1 and len(policy[0].legal_finding_ids) == 2
    assert policy[0].scope_policy == "separate_organization_and_form_scopes_until_verified"
    assert len([f for f in result.legal_findings if f.category == "B"]) == 2
    assert len(result.commercial_work) == 3


def test_standalone_document_question_has_no_automatic_paid_scope(tmp_path):
    _, _, result = assess(tmp_path, POLICY)
    assert categories(result) == ["B"]
    assert not result.commercial_work
    safe(result)


def test_medical_markers_require_substantive_content_not_nav(tmp_path):
    _, _, result = assess(
        tmp_path,
        '<p>Стоматологическая клиника</p><nav><a href="/price">Цены</a>'
        '<a href="/services">Услуги</a></nav>',
    )
    assert "G" in categories(result)
    assert not result.commercial_work
    safe(result)


def test_substantive_medical_scope_is_conditional_documents(tmp_path):
    _, _, result = assess(
        tmp_path,
        "<p>Стоматологическая клиника</p>"
        "<p>Услуги стоматолога: лечение и профилактические осмотры.</p>"
        "<p>Цена профилактического осмотра 1000 руб.</p>",
    )
    assert "medical_disclosure_documents" in {w.work_type for w in result.commercial_work}
    safe(result)


def test_partial_html_reasons_and_original_hash_retained(tmp_path):
    _, primary, result = assess(tmp_path, POLICY + FORM + "<section/>НЕОБСЛЕДОВАННЫЙ ХВОСТ")
    assert result.source_origin == primary.source_origin and result.materials == primary.materials
    assert all(f.evidence_completeness == "partial" for f in result.legal_findings)
    assert all(
        c.artifact_sha256 for f in result.legal_findings for fact in f.facts for c in fact.citations
    )
    safe(result)


@pytest.mark.parametrize("change", ["missing_hash", "different_hash", "unknown_material"])
def test_broken_source_chain_downgrades_and_blocks_paid_scope(tmp_path, change):
    _, primary, _ = assess(tmp_path, POLICY + FORM)
    # Model-copy represents an imported/modified report; it cannot create legal trust.
    for o in primary.observations:
        for c in o.citations:
            if change == "missing_hash":
                c.artifact_sha256 = None
            elif change == "different_hash":
                c.artifact_sha256 = "0" * 64
    for form in primary.forms:
        form.citation.artifact_sha256 = None
    if change == "unknown_material":
        primary.organizations = []
        primary.forms[0].citation.evidence_id = "not_in_materials"
    result = qualify_primary(primary)
    assert any(f.chain_status == "incomplete" for f in result.legal_findings)
    assert not result.commercial_work
    safe(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "confirmed_violation"),
        ("kind", "potential_risk"),
        ("legal_status", "confirmed"),
        ("possible_legal_consequences", ["штраф 1000000"]),
    ],
)
def test_no_forged_confirmation_consequences_or_promotion(tmp_path, field, value):
    _, _, result = assess(tmp_path, FORM)
    payload = result.legal_findings[0].model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError):
        LegalFinding.model_validate(payload)


@pytest.mark.parametrize(
    "field", ["production_go_allowed", "client_release_allowed", "automatic_send_allowed"]
)
def test_assessment_cannot_enable_go_or_release(tmp_path, field):
    _, _, result = assess(tmp_path, POLICY + FORM)
    payload = result.model_dump(mode="json")
    payload[field] = True
    with pytest.raises(ValidationError):
        InternalAssessment.model_validate(payload)
    imported = tmp_path / "internal.json"
    imported.write_text(result.model_dump_json())
    decision = decide_production(
        EXAMPLES / "analysis-dossier",
        EXAMPLES / "decision-finding.json",
        EXAMPLES / "legal-registry.json",
        imported_report=imported,
    )
    assert decision.outcome == "HOLD"
    with pytest.raises(PermissionError):
        send_client_message(decision)


def test_outline_never_leaks_quotes_routes_fines_or_repair_instructions(tmp_path):
    _, _, result = assess(
        tmp_path,
        POLICY + FORM + "<p>СЕКРЕТНАЯ ИНСТРУКЦИЯ: вставьте готовый документ; штраф 999999.</p>",
    )
    summary = preliminary_outline(result)
    value = summary.model_dump(mode="json")
    serialized = summary.model_dump_json()
    assert "client_release_allowed" not in value
    assert not value["dispatch_permitted"] and value["draft_only"]
    assert "СЕКРЕТНАЯ" not in serialized and "999999" not in serialized
    assert "artifact_path" not in serialized and "quote" not in serialized
    assert summary.source_assessment_sha256 == digest(result.model_dump(mode="json"))
    value["client_release_allowed"] = False
    with pytest.raises(ValidationError):
        PreliminaryClientSummary.model_validate(value)


def test_deterministic_ids_json_and_non_mutating_legacy_report(tmp_path):
    _, primary, first = assess(tmp_path, POLICY + FORM)
    before = primary.model_dump_json()
    second = qualify_primary(primary)
    assert first.model_dump_json() == second.model_dump_json()
    assert (
        preliminary_outline(first).model_dump_json()
        == preliminary_outline(second).model_dump_json()
    )
    assert before == primary.model_dump_json()
    assert first.source_primary_sha256 == digest(primary.model_dump(mode="json"))


def test_export_four_new_reports_and_preserve_old_json(tmp_path):
    root, primary, result = assess(tmp_path, POLICY + FORM)
    out = tmp_path / "result"
    write_report(primary, out, root)
    assert (out / "audit.json").read_text() == primary.model_dump_json(indent=2) + "\n"
    internal = InternalAssessment.model_validate_json(
        (out / "internal-assessment.json").read_text()
    )
    assert internal == result
    legal = json.loads((out / "legal-findings.json").read_text())
    work = json.loads((out / "commercial-work.json").read_text())
    assert legal["rows"] == [f.model_dump(mode="json") for f in result.legal_findings]
    assert work["rows"] == [w.model_dump(mode="json") for w in result.commercial_work]
    assert legal["source_origin"] == primary.source_origin.model_dump(mode="json")
    assert not json.loads((out / "preliminary-client-summary.json").read_text())[
        "dispatch_permitted"
    ]


def test_empty_saved_package_not_invented_findings_or_paid_work():
    primary = run(EXAMPLES / "primary-audit/incomplete/dossier")
    result = qualify_primary(primary)
    assert not result.legal_findings and not result.commercial_work
    assert not preliminary_outline(result).directions
    assert len(result.assessed_stage_ids) == 13
    safe(result)


def test_legacy_primary_without_new_fields_remains_usable():
    primary = run(EXAMPLES / "primary-audit/dossier", sector="medical")
    payload = primary.model_dump(mode="json")
    for key in ("declarations", "integration_groups", "popup_relations"):
        payload.pop(key)
    for form in payload["forms"]:
        form.pop("mechanism")
    result = qualify_primary(PrimaryAudit.model_validate(payload))
    assert result.legal_findings
    safe(result)


def test_unknown_static_coverage_not_qualified_for_paid_documents(tmp_path):
    _, primary, _ = assess(tmp_path, POLICY + FORM)
    span = primary.forms[0].citation.static_span
    span.limitations = ["Decoding loss: unknown static region"]
    span.status = "unknown"
    result = qualify_primary(primary)
    assert any(f.evidence_completeness == "unknown" for f in result.legal_findings)
    assert not result.commercial_work
    safe(result)


def test_advertising_link_is_question_not_proof_or_paid_need(tmp_path):
    _, _, result = assess(
        tmp_path,
        '<form><input name="email"><a href="/advertising-consent.html">Подробнее</a></form>',
    )
    assert "D" in categories(result)
    assert not result.commercial_work
    safe(result)


def test_forged_artifact_sha_does_not_establish_fact(tmp_path):
    _, _, result = assess(tmp_path, FORM)
    payload = result.model_dump(mode="json")
    payload["legal_findings"][0]["facts"][0]["citations"][0]["artifact_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        InternalAssessment.model_validate(payload)


def test_changed_source_content_changes_finding_identity(tmp_path):
    _, primary, result = assess(tmp_path, FORM)
    old_ids = {f.id for f in result.legal_findings}
    copied = primary.model_copy(deep=True)
    # An inconsistent imported hash cannot be silently treated as a new verified source.
    copied.forms[0].citation.artifact_sha256 = "0" * 64
    changed = qualify_primary(copied)
    assert old_ids != {f.id for f in changed.legal_findings}
    safe(changed)


def test_committed_qualification_examples_match_real_synthetic_run():
    base = EXAMPLES / "primary-audit"
    expected = qualify_primary(run(base / "dossier", sector="medical"))
    result = base / "result"
    assert (
        InternalAssessment.model_validate_json((result / "internal-assessment.json").read_text())
        == expected
    )
    assert PreliminaryClientSummary.model_validate_json(
        (result / "preliminary-client-summary.json").read_text()
    ) == preliminary_outline(expected)


def test_unverified_norms_never_generate_legal_consequences(tmp_path):
    from lexradar.primary import audit_saved_dossier

    root, _ = make_dossier(tmp_path, POLICY + FORM)
    primary = audit_saved_dossier(root, registry_path=EXAMPLES / "legal-registry.json", as_of=NOW)
    assert primary.normative_references
    result = qualify_primary(primary)
    safe(result)
    assert all(
        f.consequences_status == "blocked_no_trusted_applicable_norm" for f in result.legal_findings
    )


@pytest.mark.parametrize("change", ["unknown_finding", "unknown_fact", "duplicate_type"])
def test_forged_or_duplicate_work_chain_rejected(tmp_path, change):
    _, _, result = assess(tmp_path, POLICY + FORM)
    payload = result.model_dump(mode="json")
    if change == "unknown_finding":
        payload["commercial_work"][0]["legal_finding_ids"] = ["missing"]
    elif change == "unknown_fact":
        payload["commercial_work"][0]["support_fact_ids"] = ["missing"]
    else:
        payload["commercial_work"].append(payload["commercial_work"][0])
    with pytest.raises(ValidationError):
        InternalAssessment.model_validate(payload)


@pytest.mark.parametrize(
    "text",
    ["Вставьте этот полный текст политики...", "Нарушение доказано!", "Штраф 1000000 рублей"],
)
def test_preliminary_outline_rejects_uncontrolled_client_text(tmp_path, text):
    _, _, result = assess(tmp_path, FORM)
    payload = preliminary_outline(result).model_dump(mode="json")
    payload["directions"][0]["attention_reason"] = text
    with pytest.raises(ValidationError):
        PreliminaryClientSummary.model_validate(payload)
