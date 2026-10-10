"""Primary methodology uses actual synthetic saved artifacts, never live sites or legal GO."""

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_reading_integrity import pdf_dossier, save_manifest

from lexradar.approval import send_client_message
from lexradar.collector.artifacts import ArtifactStore
from lexradar.collector.models import (
    CollectedEvidence,
    CollectionResult,
    EntityObservation,
    FormObservation,
    InputField,
    Limits,
    PageObservation,
)
from lexradar.collector.reading import extract_html
from lexradar.decision import decide_production
from lexradar.primary import audit_saved_dossier
from lexradar.primary.cli import write_report
from lexradar.primary.markup import SavedMarkup
from lexradar.primary.models import PrimaryAudit
from lexradar.primary.stages import STAGES

NOW = datetime(2026, 10, 10, tzinfo=UTC)
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j2ioAAAAASUVORK5CYII="
)


@pytest.fixture(autouse=True)
def no_external_actions(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Primary audit may not access sites, browsers, registries or LLM")

    monkeypatch.setattr("lexradar.collector.network.Fetcher.get", blocked)
    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", blocked)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", blocked)


def make_dossier(parent: Path, body: str | None, *, text_limit=100_000, head=""):
    root = parent / "dossier"
    store = ArtifactStore(root, Limits())
    data = CollectionResult(
        target_url="https://clinic.example/", started_at=NOW, limits=store.limits
    )
    if body is not None:
        raw = (
            "<!doctype html><html><head>" + head + "</head><body>" + body + "</body></html>"
        ).encode()
        html = store.save("page-0001.html", raw, "html")
        text, reading = extract_html(raw, text_limit)
        derivative = store.save("page-0001.txt", text.encode(), "text")
        screenshot = store.save("synthetic-pixel.png", PNG, "screenshot")
        e = CollectedEvidence(
            id="page-0001",
            source=data.target_url,
            captured_at=NOW,
            observed_fact="Synthetic saved fixture, not a real browser collection",
            available=True,
            status="complete",
            artifacts=[html, derivative, screenshot],
            artifact_path=html.path,
            sha256=html.sha256,
        )
        data.evidence.append(e)
        data.pages.append(
            PageObservation(
                requested_url=e.source,
                final_url=e.source,
                captured_at=NOW,
                evidence_id=e.id,
                http_status=200,
                text=text,
                reading=reading,
            )
        )
    save_manifest(root, data)
    return root, data


def run(root, **kwargs):
    return audit_saved_dossier(root, as_of=NOW, **kwargs)


def stage(report, sid):
    return next(s for s in report.stages if s.stage_id == sid)


def assert_internal(report):
    assert len(report.stages) == 13 and [s.stage_id for s in report.stages] == [
        s.stage_id for s in STAGES
    ]
    assert report.production_outcome == "HOLD" and report.legal_status == "not_confirmed"
    assert not report.production_go_allowed and not report.client_release_allowed
    assert not report.automatic_send_allowed and not report.audit_complete
    assert not report.coverage.legal_research_completed
    assert all(
        not row.operator_confirmed and not row.identity_verified for row in report.organizations
    )
    assert all(not f.submitted and not f.functional_test_performed for f in report.forms)
    assert all(not o.legal_violation_established for o in report.observations)


@pytest.mark.parametrize(
    "body,count,situation",
    [
        (
            "<p>ООО «Синтетика А» ИНН 0000000000 ОГРН 0000000000000</p>",
            1,
            "one_organization_mentioned",
        ),
        (
            "<p>ООО «Синтетика А» ИНН 0000000000</p><p>ООО «Синтетика Б» ИНН 1111111111</p>",
            2,
            "multiple_roles_unestablished",
        ),
        ("<p>Синтетический бренд без установленного оператора</p>", 0, "operator_unestablished"),
        (
            "<p>ИП «Синтетический предприниматель» ИНН 000000000000</p>",
            1,
            "one_organization_mentioned",
        ),
    ],
)
def test_organization_situations(tmp_path, body, count, situation):
    root, _ = make_dossier(tmp_path, body)
    result = run(root)
    assert len(result.organizations) == count and result.operator_situation == situation
    assert_internal(result)
    if count:
        assert result.organizations[0].identification_sources[0].quote
        assert result.organizations[0].identification_sources[0].artifact_sha256


def test_different_entities_for_different_forms_not_actual_routing(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        '<form action="/medical"><p>ООО «Синтетическая клиника»</p>'
        '<input name="phone"><button>Запись</button></form>'
        '<form action="https://intake.example/submit"><p>ООО «Синтетические заявки»</p>'
        '<input name="email"><button>Заявка</button></form>',
    )
    result = run(root)
    assert len(result.organizations) == 2 and len(result.forms) == 2
    assert result.forms[0].organization_candidates != result.forms[1].organization_candidates
    assert all(len(f.organization_candidates) == 1 for f in result.forms)
    assert all(o.form_ids and o.flow_ids for o in result.organizations)
    assert all(
        f.actual_recipient == "unknown" and f.network_route == "not_checked" for f in result.flows
    )
    assert_internal(result)


@pytest.mark.parametrize(
    "checkbox",
    [
        "",
        '<input type="checkbox" name="consent">',
        '<input type="CHECKBOX" checked name="consent">',
    ],
)
def test_checkbox_is_observation_not_lawfulness(tmp_path, checkbox):
    root, _ = make_dossier(
        tmp_path,
        '<form><legend>Запись</legend><input name="phone">'
        + checkbox
        + "<button>Отправить</button></form>",
    )
    report = run(root)
    form = report.forms[0]
    assert form.purpose_hint == "Запись" and form.lawfulness == "unestablished"
    boxes = [f for f in form.fields if f.type == "checkbox"]
    assert len(boxes) == bool(checkbox)
    if "checked" in checkbox:
        assert boxes[0].checked
    assert stage(report, "06").additional_check_ids
    assert_internal(report)


@pytest.mark.parametrize(
    "texts,pages,scan",
    [
        ([""], 100, True),
        (["Synthetic policy excerpt", ""], 100, True),
        (["Synthetic policy ONE", "Synthetic policy TWO"], 1, False),
    ],
)
def test_policy_scan_and_partial_text_never_absent_or_fully_read(tmp_path, texts, pages, scan):
    root, data = pdf_dossier(tmp_path, texts, pages=pages, scans=scan)
    source = "https://clinic.example/privacy-policy.pdf"
    data.evidence[0].source = source
    data.documents[0].requested_url = source
    save_manifest(root, data)
    report = run(root)
    assert stage(report, "04").status == "insufficient_evidence"
    assert stage(report, "04").observed_fact_ids
    assert not report.materials[0].reading.text_coverage_complete
    assert not report.coverage.visual_examination_performed
    assert_internal(report)


def test_policy_not_found_in_subset_is_not_absence(tmp_path):
    root, _ = make_dossier(tmp_path, "<p>Синтетическая главная страница</p>")
    report = run(root)
    assert stage(report, "04").status == "insufficient_evidence"
    facts = [o for o in report.observations if o.stage_id == "04"]
    assert facts and all(o.kind == "scoped_search" for o in facts)
    assert "Политика отсутствует на сайте" not in report.model_dump_json()
    assert not report.commercial_opportunity


def test_policy_truncated_html_has_partial_scope(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        "<h1>Политика обработки персональных данных</h1><p>" + "Синтетический текст " * 50 + "</p>",
        text_limit=70,
    )
    result = run(root)
    assert stage(result, "04").status == "insufficient_evidence"
    assert result.materials[0].reading.text_truncated
    assert result.coverage.incomplete_text_materials == 1


def test_multiple_services_no_execution_cookie_or_transfer_claim(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        "<p>Синтетический сайт</p>",
        head='<script src="https://mc.yandex.ru/metrika/tag.js"></script>'
        '<script src="https://www.google-analytics.com/analytics.js"></script>',
    )
    report = run(root)
    assert len(report.services) == 2
    assert {s.service_hint for s in report.services} == {
        "Yandex Metrika reference",
        "Google Analytics reference",
    }
    assert all(
        not s.execution_confirmed and not s.data_transfer_confirmed and not s.cookies_examined
        for s in report.services
    )
    assert stage(report, "09").hypothesis_ids
    assert_internal(report)


def test_maps_brand_not_invented_as_analytics(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        "<p>© Яндекс. Яндекс.Карты</p>",
        head='<script src="https://api-maps.yandex.ru/2.1/"></script>',
    )
    result = run(root)
    assert len(result.services) == 1
    assert "not evidence of Metrika" in result.services[0].service_hint


def test_foreign_form_action_no_confirmed_cross_border(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        '<form action="https://foreign.example/route"><input name="phone">'
        "<button>Запись</button></form>",
    )
    report = run(root)
    assert report.forms[0].action_reference == "https://foreign.example/route"
    assert report.flows[0].cross_border_transfer == "unconfirmed"
    assert report.flows[0].network_route == "not_checked"


def test_patient_caption_without_visual_review_or_known_consent(tmp_path):
    root, _ = make_dossier(tmp_path, '<img src="/synthetic-pixel.png" alt="Фото пациента">')
    report = run(root)
    assert stage(report, "10").hypothesis_ids
    facts = [o for o in report.observations if o.stage_id == "10"]
    assert all(o.kind == "static_markup" and "не осмотрено" in o.description for o in facts)
    assert not report.coverage.visual_examination_performed
    assert not any("Согласие отсутствует" in h.question for h in report.hypotheses)


def test_medical_scope_missing_prices_and_incomplete_license_not_violation(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        "<h1>Синтетическая медицинская клиника</h1>"
        "<p>Услуги врача. Сведения о лицензии требуют проверки.</p>",
    )
    report = run(root)
    checks = {c.check_id: c for c in report.medical_checks}
    assert report.sector_hint == "medical"
    assert checks["prices"].status == "insufficient_evidence"
    assert not checks["prices"].observation_ids
    assert checks["license_details"].status == "insufficient_evidence"
    assert any(t.type == "medical_license_registry_review" for t in report.missing_checks)
    assert all(not c.applicable_obligation_confirmed for c in report.medical_checks)
    assert_internal(report)


def test_medical_organization_does_not_imply_special_data_form(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        '<h1>Синтетическая клиника</h1><form><input name="phone"><button>Запись</button></form>',
    )
    report = run(root)
    assert not stage(report, "10").observed_fact_ids


def test_health_field_marker_and_advertising_consent_are_only_questions(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        '<form aria-label="Рекламная подписка"><label for="s">Жалобы</label>'
        '<textarea id="s" name="symptoms">SYNTHETIC DEFAULT NOT TO EXPORT</textarea>'
        '<a href="/advertising-consent">Отдельное согласие на рекламу</a>'
        "<button>Подписаться</button></form>",
    )
    result = run(root)
    assert stage(result, "07").hypothesis_ids and stage(result, "10").hypothesis_ids
    assert result.forms[0].advertising_links
    assert result.forms[0].fields[0].label == "Жалобы"
    assert "SYNTHETIC DEFAULT NOT TO EXPORT" not in result.forms[0].submission_context


def test_unknown_norm_currency_and_rkn_not_requested(tmp_path):
    root, _ = make_dossier(tmp_path, "<p>ООО «Синтетика» ИНН 0000000000</p>")
    report = run(root, registry_path=EXAMPLES / "legal-registry.json")
    assert report.normative_references
    assert all(n.assessment.status == "unverified" for n in report.normative_references)
    assert stage(report, "11").status == "not_checked"
    tasks = [t for t in report.missing_checks if t.type == "rkn_registry_review"]
    assert tasks and all(t.external_access_required and t.state == "not_performed" for t in tasks)


@pytest.mark.parametrize("unavailable", [False, True])
def test_insufficient_valid_dossier_no_artificial_facts(tmp_path, unavailable):
    root, data = make_dossier(tmp_path, None)
    if unavailable:
        evidence = CollectedEvidence(
            id="page-0001",
            source=data.target_url,
            captured_at=NOW,
            observed_fact="Synthetic failed attempt",
            available=False,
            status="unavailable",
            unavailable_reason="Synthetic access failure",
        )
        data.evidence.append(evidence)
        data.pages.append(
            PageObservation(requested_url=data.target_url, evidence_id=evidence.id, captured_at=NOW)
        )
        save_manifest(root, data)
    result = run(root)
    assert not result.forms and not result.organizations and not result.commercial_opportunity
    assert not stage(result, "04").observed_fact_ids
    assert result.coverage.reproduced_text_materials == 0
    if not unavailable:
        assert not result.observations and all(s.status == "not_checked" for s in result.stages)
    assert_internal(result)


def test_forged_manifest_entities_and_forms_not_promoted(tmp_path):
    root, data = make_dossier(tmp_path, "<p>Синтетическая страница без форм</p>")
    data.entities.append(
        EntityObservation(source=data.target_url, name="ООО «ВЫДУМАНО»", raw_text="ООО «ВЫДУМАНО»")
    )
    data.pages[0].forms.append(
        FormObservation(
            page_url=data.target_url,
            purpose="invented",
            fields=[InputField(tag="input", type="checkbox", checked=True)],
            policy_links=[],
            consent_links=[],
            submission_context="invented",
            unavailable_reason="Synthetic screenshot missing",
        )
    )
    save_manifest(root, data)
    result = run(root)
    assert not result.forms and not result.organizations


def test_commercial_opportunity_never_production_go_or_client_release(tmp_path):
    root, _ = make_dossier(tmp_path, '<form><input name="email"><button>Отправить</button></form>')
    report = run(root)
    assert report.commercial_opportunity
    assert all(
        not w.violation_proof
        and not w.proposal_generated
        and not w.direct_site_implementation_offered
        for w in report.commercial_opportunity
    )
    imported = tmp_path / "primary.json"
    imported.write_text(report.model_dump_json())
    decision = decide_production(
        EXAMPLES / "analysis-dossier",
        EXAMPLES / "decision-finding.json",
        EXAMPLES / "legal-registry.json",
        imported_report=imported,
    )
    assert decision.outcome == "HOLD" and not decision.client_release_allowed
    with pytest.raises(PermissionError):
        send_client_message(decision)
    for key, value in (
        ("production_outcome", "GO"),
        ("client_release_allowed", True),
        ("automatic_send_allowed", True),
        ("audit_complete", True),
    ):
        with pytest.raises(ValidationError):
            PrimaryAudit.model_validate({**report.model_dump(), key: value})


def test_no_issue_observed_is_scoped_not_guarantee(tmp_path):
    root, _ = make_dossier(tmp_path, "<p>Synthetic scoped text</p>")
    report = run(root)
    stage(report, "03").status = "no_issue_observed"
    reloaded = PrimaryAudit.model_validate_json(report.model_dump_json())
    assert "не является юридической гарантией" in stage(reloaded, "03").status_scope
    assert_internal(reloaded)


def test_reproducible_for_exact_inputs_and_explicit_as_of(tmp_path):
    root, _ = make_dossier(tmp_path, '<p>ООО «Синтетика»</p><form><input name="phone"></form>')
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    first, second = run(root), run(root)
    assert first.model_dump_json() == second.model_dump_json()
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_cli_exports_only_internal_sidecars(tmp_path, monkeypatch):
    from lexradar.cli import main

    root, _ = make_dossier(tmp_path, '<p>ООО «Синтетика»</p><form><input name="email"></form>')
    output = tmp_path / "result"
    monkeypatch.setattr(
        "sys.argv",
        [
            "lexradar",
            "primary-audit",
            str(root),
            "--output",
            str(output),
            "--as-of",
            NOW.isoformat(),
        ],
    )
    main()
    assert {p.name for p in output.iterdir()} == {
        "audit.json",
        "organizations.json",
        "forms.json",
        "flows.json",
        "observations.json",
        "hypotheses.json",
        "missing-checks.json",
        "commercial-opportunity.json",
    }
    for p in output.iterdir():
        value = json.loads(p.read_text())
        assert value["audience"] == "internal_only" and not value["client_release_allowed"]


def test_output_cannot_modify_dossier_and_invalid_artifact_stops(tmp_path):
    root, _ = make_dossier(tmp_path, "<p>Synthetic</p>")
    result = run(root)
    with pytest.raises(ValueError):
        write_report(result, root / "result", root)
    (root / "artifacts/page-0001.html").write_bytes(b"substituted")
    with pytest.raises(ValueError, match="integrity"):
        run(root)


@pytest.mark.parametrize(
    "body",
    [
        '<template><form><input name="FAKE"></form></template>',
        '<template><template></template><form><input name="FAKE"></form></template>',
        '<template><noscript></template><form><input name="FAKE"></form></noscript>',
        '<script><form><input name="FAKE"></form></script>',
    ],
)
def test_excluded_and_broken_markup_never_invents_forms(tmp_path, body):
    root, _ = make_dossier(tmp_path, body)
    result = run(root)
    assert not result.forms


def test_static_limits_and_labels_do_not_infer_live_behavior():
    parser = SavedMarkup("https://clinic.example/")
    parser.feed(
        '<form><label>Первое<input name="one"/></label></form>'
        '<form><label for="same">Второе</label><input id="same" name="two"/></form>'
    )
    parser.close()
    assert parser.forms[0].fields[0].label == "Первое"
    assert parser.forms[1].fields[0].label == "Второе"
    bounded = SavedMarkup("https://clinic.example/")
    bounded.feed("<form><input>" * 31)
    bounded.close()
    assert bounded.limitations and len(bounded.forms) == 1


def test_bad_reference_preserved_not_followed(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        '<form action="http://[broken"><input name="email"></form>',
        head='<script src="http://[broken"></script>',
    )
    result = run(root)
    assert result.forms[0].action_reference == "http://[broken"
    assert result.services[0].service_hint is None


def test_invalid_primary_references_rejected(tmp_path):
    root, _ = make_dossier(tmp_path, '<form><input name="email"></form>')
    report = run(root)
    raw = report.model_dump()
    raw["hypotheses"][0]["observation_ids"] = ["unknown"]
    with pytest.raises(ValidationError):
        PrimaryAudit.model_validate(raw)


@pytest.mark.parametrize("field", ["required_checks", "normative_reference_ids"])
def test_hypothesis_requires_existing_check_and_norm(tmp_path, field):
    root, _ = make_dossier(tmp_path, '<form><input name="email"></form>')
    raw = run(root).model_dump()
    raw["hypotheses"][0][field] = ["unknown"]
    with pytest.raises(ValidationError):
        PrimaryAudit.model_validate(raw)


def test_reproduced_quotes_match_original_and_bind_hash(tmp_path):
    root, data = make_dossier(tmp_path, "<p>ООО «Синтетическая клиника»</p>")
    report = run(root)
    text = data.pages[0].text
    original = data.evidence[0].artifacts[0]
    for fact in report.observations:
        for citation in fact.citations:
            if citation.basis == "reproduced_text":
                if citation.quote is not None:
                    assert citation.quote in text
                else:
                    assert fact.kind == "scoped_search"
                assert citation.artifact_path == original.path
                assert citation.artifact_sha256 == original.sha256


def test_committed_examples_remain_internal_and_incomplete():
    base = EXAMPLES / "primary-audit"
    report = PrimaryAudit.model_validate_json((base / "result/audit.json").read_text())
    assert report.model_dump_json() == run(base / "dossier", sector="medical").model_dump_json()
    assert len(report.organizations) == len(report.forms) == 2
    incomplete = PrimaryAudit.model_validate_json((base / "incomplete/audit.json").read_text())
    assert not incomplete.observations and not incomplete.commercial_opportunity
    assert incomplete.coverage.page_attempts == 0
    assert incomplete.production_outcome == report.production_outcome == "HOLD"


def test_broken_markup_preserves_prefix_form_with_stage_limitation(tmp_path):
    root, _ = make_dossier(
        tmp_path, '<form><input name="email"></form><template><noscript></template>'
    )
    report = run(root)
    assert len(report.forms) == 1
    assert stage(report, "05").status == "insufficient_evidence"
    assert stage(report, "05").limitations
    assert not report.audit_complete and report.production_outcome == "HOLD"


def test_wrapped_label_never_captures_prefilled_control_value(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        '<form><label>Комментарий<textarea name="comment">SYNTHETIC_DEFAULT</textarea></label>'
        '<label>Выбор<select name="choice"><option>SYNTHETIC_DEFAULT</option></select></label>'
        '<input name="email" value="SYNTHETIC_DEFAULT"></form>',
    )
    form = run(root).forms[0]
    assert "SYNTHETIC_DEFAULT" not in form.model_dump_json()
    assert form.fields[0].label == "Комментарий" and form.fields[1].label == "Выбор"
