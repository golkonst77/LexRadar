"""Local reconstruction, negative controls and release boundaries; no live requests."""

import json
import runpy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lexradar.collector.models import DocumentImageObservation
from lexradar.primary.cli import write_report
from lexradar.primary.content_checks import price_rows
from lexradar.primary.markup import SavedMarkup
from lexradar.primary.qualification import qualify_primary
from lexradar.primary.service import audit_saved_dossier
from lexradar.verifier.registry import assess_registry, load_registry

REPO = Path(__file__).resolve().parents[1]
REPLAY = runpy.run_path(str(REPO / "examples/drstom-control/replay.py"))
NOW = datetime(2026, 10, 13, tzinfo=UTC)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Control replay must not make requests")

    monkeypatch.setattr("lexradar.collector.network.Fetcher.get", blocked)
    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", blocked)


def replay(tmp_path, *, files=None):
    fixtures = REPLAY["FIXTURES"]
    if files is not None:
        fixtures = tmp_path / "html"
        fixtures.mkdir()
        for name, body in files.items():
            (fixtures / (name + ".html")).write_text(
                "<html><body>" + body + "</body></html>", encoding="utf-8"
            )
    dossier = REPLAY["build_replay"](
        tmp_path / "dossier", fixtures, base_url="https://other.example"
    )
    return dossier, audit_saved_dossier(dossier, sector="medical", as_of=NOW)


def observations(report, code):
    return [o for o in report.observations if o.description.startswith(code + ":")]


def test_control_forms_images_prices_contacts_and_no_legal_release(tmp_path):
    dossier, report = replay(tmp_path)
    assert len(report.forms) == 3
    by_id = {f.declared_id: f for f in report.forms}
    assert sum(f.type == "checkbox" for f in by_id["form95467681"].fields) == 1
    assert by_id["form95467681"].fields[-1].checked is False
    assert not any(f.type == "checkbox" for f in by_id["form98229460"].fields)
    assert len(by_id["form149094843"].fields) == 4
    assert by_id["form95467681"].url != by_id["form98229460"].url
    assert by_id["form95467681"].visibility_hint == "declared_hidden"
    assert all(not f.popup_opened and not f.submitted for f in report.forms)
    assert all(f.policy_links and not f.consent_links for f in report.forms)

    images = observations(report, "document_image_unread")
    assert len(images) == 2
    assert sorted(len(o.citations) for o in images) == [1, 3]
    assert all("not_read" in o.scope for o in images)
    price = observations(report, "price_discrepancy_candidate")
    assert len(price) == 1 and len(price[0].citations) == 8
    assert {str(c.url).rsplit("/", 1)[-1] for c in price[0].citations} == {"price", "karies"}
    for name in (
        "Лечение пульпита",
        "Лечение периодонтита",
        "Эстетическая реставрация",
        "Профессиональная гигиена",
    ):
        assert name in price[0].description
    mismatch = observations(report, "mailto_recipient_mismatch")
    assert len(mismatch) == 1
    assert "stomboxx@gmail.com" in mismatch[0].citations[0].quote
    assert "hello@company.com" in mismatch[0].citations[0].quote
    for code in (
        "policy_response_period_candidate",
        "policy_contact_url_candidate",
        "cross_border_policy_review",
        "dated_program_role_review",
    ):
        assert observations(report, code)
    assert {g.service_kind for g in report.integration_groups} >= {
        "callibri",
        "tilda_stats",
        "prodoctorov",
    }

    assessment = qualify_primary(report)
    assert (
        len(
            [
                f
                for f in assessment.legal_findings
                if f.factual_description.startswith("price_discrepancy_candidate:")
            ]
        )
        == 1
    )
    assert not assessment.client_release_allowed and assessment.production_outcome == "HOLD"
    assert all(
        not f.possible_legal_consequences and f.legal_status == "not_confirmed"
        for f in assessment.legal_findings
    )
    write_report(report, tmp_path / "report", dossier)
    outline = json.loads(
        (tmp_path / "report/preliminary-client-summary.json").read_text(encoding="utf-8")
    )
    assert not outline["dispatch_permitted"]
    text = json.dumps(outline, ensure_ascii=False).casefold()
    assert all(
        word not in text
        for word in ("штраф", "лицензия отсутствует", "незаконная передача", "гарантируем")
    )


@pytest.mark.parametrize(
    "unit1,unit2,amount2,expected",
    [
        ("за зуб", "за зуб", "1 000 ₽", False),
        ("за зуб", "за канал", "2 000 ₽", False),
        ("", "", "2 000 ₽", True),
    ],
)
def test_price_units_equal_prices_and_unknown_scope(tmp_path, unit1, unit2, amount2, expected):
    def row(unit, amount):
        return f"<table><tr><td>Услуга</td><td>{unit}</td><td>{amount}</td></tr></table>"

    _, report = replay(tmp_path, files={"a": row(unit1, "1 000 ₽"), "b": row(unit2, amount2)})
    assert bool(observations(report, "price_discrepancy_candidate")) == expected


def test_same_email_with_case_query_and_archived_program(tmp_path):
    _, report = replay(
        tmp_path,
        files={
            "contacts": '<a href="mailto:INFO@example.org?subject=Hello">info@EXAMPLE.org</a>',
            "archive": "<p>Архив: программа 2017 года, плановый период 2018–2019.</p>",
        },
    )
    assert not observations(report, "mailto_recipient_mismatch")
    assert observations(report, "archived_program_reference")
    assert not observations(report, "dated_program_role_review")


def test_failed_image_is_document_limitation_not_absence(tmp_path):
    dossier, _ = replay(tmp_path, files={"documents": '<h1>Документы</h1><img src="/opaque.png">'})
    manifest = dossier / "collection.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["pages"][0]["document_images"] = [
        DocumentImageObservation(
            url="https://other.example/opaque.png",
            declarations=["img@src"],
            download_status="failed",
            limitation="Image download timed out; not read",
        ).model_dump(mode="json")
    ]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    report = audit_saved_dossier(dossier, sector="medical", as_of=NOW)
    row = observations(report, "document_image_unread")[0]
    assert "failed" in row.scope and "timed out" in row.scope
    assert not row.legal_violation_established


def test_malformed_table_cannot_create_complete_price_row():
    parser = SavedMarkup("https://fixture.example/", recover=True)
    parser.feed("<table><tr><td>Услуга</td><td>100 ₽<b></tr></table>")
    parser.close()
    assert not list(price_rows(parser))


def test_current_historical_templates_do_not_confirm_norms():
    registry, _ = load_registry(REPO / "examples/legal-registry.json")
    current = assess_registry(registry, NOW.date(), NOW, 30)
    assert all(n.status != "current_confirmed" for n in current.values())
    source = next(s for s in registry.sources if s.id == "736-template")
    source.effective_from = datetime(2023, 9, 1).date()
    source.effective_until = datetime(2026, 8, 31).date()
    # Local claimed periods test the existing registry; they do not certify legal dates.
    current = assess_registry(registry, NOW.date(), NOW, 30)
    old = current["736-template"]
    assert any("expired" in reason for reason in old.reasons)
    past = assess_registry(registry, datetime(2026, 8, 1).date(), NOW, 30)
    assert not any("expired" in reason for reason in past["736-template"].reasons)
    source.status = "unavailable"
    assert assess_registry(registry, NOW.date(), NOW, 30)[source.id].status == "unavailable"
