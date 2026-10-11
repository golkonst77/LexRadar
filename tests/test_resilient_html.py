"""Synthetic offline regressions; no real pilot HTML or personal data."""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from test_primary import assert_internal, make_dossier, run, stage
from test_reading_integrity import save_manifest

from lexradar.collector.provenance import MaterialOrigin
from lexradar.collector.reading import StaticText
from lexradar.primary.cli import write_report
from lexradar.primary.markup import SavedMarkup, compact


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Offline regression attempted an external action")

    monkeypatch.setattr("socket.getaddrinfo", blocked)
    monkeypatch.setattr("lexradar.collector.network.Fetcher.get", blocked)
    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", blocked)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", blocked)


def parse(html):
    p = SavedMarkup("https://clinic.example/", recover=True)
    p.feed(html)
    p.close()
    return p


@pytest.mark.parametrize("tag", ["span", "p", "li"])
def test_stray_close_preserves_following_text_and_exact_error(tag):
    html = f"<html><body><p>До ошибки</p>\n</{tag}><p>После ошибки</p></body></html>"
    p = parse(html)
    assert "После ошибки" in compact(p.parts)
    assert p.diagnostics[0].position.model_dump() == {"line": 2, "column": 0}
    assert p.diagnostics[0].action == "ignored_stray_close"
    inspection = p.inspection()
    assert inspection.status == "partial" and not inspection.original_modified
    assert inspection.recovered_text_chars > 0
    assert all(s.status == "partial" and s.limitations for s in inspection.text_spans)
    assert len(inspection.text_spans) == 2
    assert inspection.text_spans[0].end_exclusive.column == 0
    assert inspection.text_spans[1].start.column == len(f"</{tag}>")
    strict = StaticText()
    strict.feed(html)
    strict.close()
    assert "После ошибки" not in compact(strict.parts)


def test_repeated_closes_never_upgrade_page_or_form():
    p = parse('<body><span>Начало</span></span></span><form><input name="field"></form></body>')
    assert len(p.forms) == 1 and len(p.forms[0].fields) == 1
    c = p.coverage(p.forms[0])
    assert c.status == c.page_status == "partial" and c.recovered
    assert c.form_end_exclusive and c.observed_region.status == "partial"
    assert len([d for d in p.diagnostics if d.action == "ignored_stray_close"]) == 2
    assert p.forms[0].field_spans[0].status == "partial"


@pytest.mark.parametrize(
    "damage",
    [
        "<div></span>",
        "<div><span></div>",
        "<script>SECRET<script>HIDDEN</script>",
        "<style>SECRET<style>HIDDEN</style>",
        "<template><div></span>SECRET</template>",
    ],
)
def test_form_damage_preserves_only_partial_prefix(tmp_path, damage):
    root, _ = make_dossier(
        tmp_path,
        '<form><input name="first">'
        + damage
        + '<input name="unknown"></form><form><input name="fake"></form>',
    )
    r = run(root)
    assert_internal(r)
    assert len(r.forms) == 1
    f = r.forms[0]
    assert [v.name for v in f.fields] == ["first"]
    assert f.static_coverage.status == f.static_coverage.page_status == "partial"
    assert f.static_coverage.reasons and f.static_coverage.observed_region
    assert f.citation.static_span.status == "partial"
    assert all(s.status == "partial" for s in f.field_spans)
    assert "SECRET" not in r.model_dump_json() and "HIDDEN" not in r.model_dump_json()
    assert any("пустой список не доказывает" in note for note in f.unresolved_processing)


@pytest.mark.parametrize(
    "block",
    [
        "<script>SECRET<script>HIDDEN</script>",
        "<style>SECRET<style>HIDDEN</style>",
        "<template><template>SECRET</template></template>",
        "<template><div></span>SECRET</template>",
        "<script/>SECRET</script>",
        "<script>SECRET</script broken>",
        "<style>SECRET",
        "<template>SECRET",
        "<!-- SECRET",
        "<!-- SECRET -->",
        "<script>SECRET</script><style>HIDDEN</style>",
    ],
)
def test_excluded_content_never_leaks_during_recovery(block):
    p = parse("<body></span><p>Открытый текст</p>" + block + "<form><input></form></body>")
    assert "Открытый текст" in compact(p.parts)
    assert "SECRET" not in compact(p.parts) and "HIDDEN" not in compact(p.parts)
    assert p.inspection().status == "partial"
    if p.quarantined:
        assert not p.forms and p.diagnostics[-1].action == "suffix_withheld"


@pytest.mark.parametrize(
    "html",
    [
        '<form action="/one" action="/two"><input name="x"></form>',
        '<form action"/ambiguous"><input name="x"></form>',
        '<div class"<script>SECRET</script>"><form><input></form>',
        '<form action="unterminated><input name="x">',
        "<div></span broken><form><input></form>",
        "<div><span></div><form><input></form>",
        "</form><form><input></form>",
    ],
)
def test_ambiguous_form_attributes_and_structure_fail_closed(html):
    p = parse(html)
    assert not p.forms
    assert p.inspection().status == "partial" and p.inspection().limitations
    assert "SECRET" not in compact(p.parts)


def test_ambiguous_non_form_attributes_are_discarded():
    p = parse(
        '<body><div class"ambiguous">Текст</div><a href="/one" href="/two">Ссылка</a>'
        '<form><input name="x"></form></body>'
    )
    assert compact(p.parts) == "Текст Ссылка"
    assert not p.links
    assert p.forms[0].fields[0].name == "x"
    assert p.coverage(p.forms[0]).status == "partial"
    assert p.diagnostics[0].action == "ignored_ambiguous_attributes"


def test_recovery_is_bounded_and_stops_at_truncation():
    p = parse('<body></span><p>Восстановлено</p><form><input name="first"><input name="')
    assert "Восстановлено" in compact(p.parts)
    assert len(p.forms[0].fields) == 1 and p.coverage(p.forms[0]).status == "partial"
    many = parse("<body>" + "</span>" * 101 + "<form><input></form></body>")
    assert not many.forms and many.quarantined
    assert len([d for d in many.diagnostics if d.action == "ignored_stray_close"]) == 100


def test_original_and_derivative_unchanged_recovered_citations_scoped(tmp_path):
    root, data = make_dossier(
        tmp_path,
        '<p>Начало</p></span><p>ООО «Синтетический оператор»</p><form><input name="phone"></form>',
    )
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    r = run(root)
    assert_internal(r)
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert len(r.organizations) == len(r.forms) == 1
    assert (
        r.materials[0].reading.extraction_state
        == r.materials[0].static_inspection.status
        == "partial"
    )
    c = r.organizations[0].identification_sources[0]
    assert c.basis == "saved_html" and c.static_span.status == "partial"
    assert c.artifact_sha256 == data.evidence[0].artifacts[0].sha256
    assert hashlib.sha256((root / c.artifact_path).read_bytes()).hexdigest() == c.artifact_sha256
    write_report(r, tmp_path / "output", root)
    exported = json.loads((tmp_path / "output/forms.json").read_text())
    assert exported["materials"][0]["static_inspection"]["diagnostics"]
    assert exported["rows"][0]["static_coverage"]["status"] == "partial"


def test_decoding_loss_stays_unknown_after_recovery():
    p = SavedMarkup("https://clinic.example/", recover=True)
    p.decoding_loss = True
    p.feed("<body></span><form><input></form></body>")
    p.close()
    assert p.inspection().status == p.coverage(p.forms[0]).status == "unknown"
    assert all(s.status == "unknown" for s in p.inspection().text_spans)


def test_manual_origin_reaches_all_exports_without_claiming_http_time(tmp_path):
    root, data = make_dossier(tmp_path, "<p>Импортированный материал</p>")
    origin = MaterialOrigin(
        kind="manual_html_import",
        acquisition_method="user_supplied_view_source",
        declared_source=str(data.target_url),
        source_sha256=data.evidence[0].sha256,
        reported_capture_date="2026-10-09",
        imported_at=data.started_at,
        captured_at_role="local_import",
        started_at_role="local_import",
        limitations=["One manual page; original capture time and HTTP metadata unknown"],
    )
    data.pages[0].http_status = data.pages[0].final_url = None
    data.origin = data.pages[0].origin = origin
    data.warnings = ["Manual source declaration is not independent authentication"]
    save_manifest(root, data)
    r = run(root)
    assert r.source_origin == r.materials[0].origin == origin
    assert r.source_origin.http_response_at is None and not r.source_origin.origin_authenticated
    assert data.warnings[0] in r.limitations and "попытка сбора" not in r.model_dump_json()
    write_report(r, tmp_path / "output", root)
    for name in (
        "audit",
        "forms",
        "organizations",
        "flows",
        "observations",
        "hypotheses",
        "missing-checks",
        "commercial-opportunity",
    ):
        payload = json.loads((tmp_path / "output" / f"{name}.json").read_text())
        assert payload["source_origin"]["kind"] == "manual_html_import"
        assert payload["materials"][0]["origin"]["captured_at_role"] == "local_import"
        assert not payload["source_origin"]["origin_authenticated"]
    assert_internal(r)


def test_legacy_origin_defaults_unknown_without_guessing_from_http(tmp_path):
    root, data = make_dossier(tmp_path, "<p>Старый материал</p>")
    raw = data.model_dump(mode="json")
    raw.pop("origin")
    raw["pages"][0].pop("origin")
    (root / "collection.json").write_text(json.dumps(raw))
    r = run(root)
    assert r.source_origin.kind == r.materials[0].origin.kind == "unknown"
    assert r.source_origin.started_at_role == r.materials[0].origin.captured_at_role == "unknown"


@pytest.mark.parametrize(
    "field,value",
    [
        ("http_response_at", datetime(2026, 10, 9, tzinfo=UTC)),
        ("captured_at_role", "collection_observation"),
        ("started_at_role", "collector_run"),
        ("imported_at", None),
        ("origin_authenticated", True),
    ],
)
def test_manual_origin_cannot_claim_authentication_or_http_time(field, value):
    values = dict(
        kind="manual_html_import",
        imported_at=datetime(2026, 10, 10, tzinfo=UTC),
        captured_at_role="local_import",
        started_at_role="local_import",
    )
    values[field] = value
    with pytest.raises(ValidationError):
        MaterialOrigin(**values)


@pytest.mark.parametrize(
    "body",
    [
        "<p>Стоматологический центр</p>",
        "<p>Лицензия центра</p>",
        "<p>Ценный опыт</p>",
        "<p>Сценарий консультации</p>",
        "<p>5 рубежей исследования</p>",
    ],
)
def test_center_is_not_price_evidence(tmp_path, body):
    root, _ = make_dossier(tmp_path, body)
    r = run(root, sector="medical")
    c = next(c for c in r.medical_checks if c.check_id == "prices")
    assert not c.observation_ids and c.content_level == "unknown"
    assert c.status == "insufficient_evidence"
    assert_internal(r)


@pytest.mark.parametrize("amount", ["33 000 рублей", "1000 руб.", "2 500 ₽"])
def test_price_quote_contains_amount_not_center(tmp_path, amount):
    root, _ = make_dossier(
        tmp_path, f"<p>Стоматологический центр</p><p>Имплантация за {amount}</p>"
    )
    r = run(root, sector="medical")
    c = next(c for c in r.medical_checks if c.check_id == "prices")
    assert c.content_level == "content_candidate" and c.observation_ids
    facts = [o for o in r.observations if o.id in c.observation_ids]
    assert amount in facts[0].citations[0].quote and "центр" not in facts[0].citations[0].quote
    assert not c.applicable_obligation_confirmed
    assert_internal(r)


@pytest.mark.parametrize(
    "body,level",
    [
        (
            '<nav><a href="/services">Услуги</a><a href="/staff">Специалисты</a>'
            '<a href="/prices">Цены на все услуги</a></nav>',
            "navigation_only",
        ),
        ("<h2>Услуги</h2><h2>Специалисты</h2><h2>Цены</h2>", "section_candidate"),
    ],
)
def test_menu_and_headings_do_not_establish_medical_content(tmp_path, body, level):
    root, _ = make_dossier(tmp_path, body)
    r = run(root, sector="medical")
    for c in r.medical_checks:
        if c.check_id in {"services", "medical_staff", "prices"}:
            assert c.content_level == level and c.status == "insufficient_evidence"
            assert not c.applicable_obligation_confirmed
    assert stage(r, "12").status == "insufficient_evidence"
    assert_internal(r)


def test_content_candidates_still_do_not_establish_compliance(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        "<p>Услуги клиники: лечение зубов и удаление зубов.</p>"
        "<p>Врач стоматолог. Сведения о стаже и квалификации требуют проверки.</p>",
    )
    r = run(root, sector="medical")
    for c in r.medical_checks:
        if c.check_id in {"services", "medical_staff"}:
            assert c.content_level == "content_candidate" and c.status == "observed"
            assert not c.applicable_obligation_confirmed
    assert_internal(r)


def test_popup_and_booking_after_error_are_partial_candidates(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        "<p>Начало</p></span>"
        '<span data-src="#policy">Политика конфиденциальности</span>'
        '<div id="policy"><h2>Политика обработки персональных данных</h2>'
        "<p>Синтетический текст документа для проверки.</p></div>"
        '<a href="https://booking.medflex.ru/?test=synthetic">Запись</a>'
        '<form><input name="phone"></form>',
    )
    r = run(root)
    policy = [o for o in r.observations if o.stage_id == "04" and o.kind == "text_quote"]
    assert policy and policy[0].citations[0].static_span.status == "partial"
    assert stage(r, "04").status == "insufficient_evidence"
    bookings = [s for s in r.services if s.reference_kind == "booking_anchor"]
    assert len(bookings) == 1 and bookings[0].citation.static_span.status == "partial"
    assert not bookings[0].data_transfer_confirmed and not bookings[0].execution_confirmed
    assert_internal(r)


def test_nested_template_elements_do_not_close_real_form_or_add_controls(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        '<form><input name="before"><template><form><input name="fake"></form>'
        '<a href="/consent">SECRET</a></template><input name="after"></form>',
    )
    r = run(root)
    assert len(r.forms) == 1
    f = r.forms[0]
    assert [v.name for v in f.fields] == ["before", "after"]
    assert not f.consent_links and "SECRET" not in f.submission_context
    assert f.static_coverage.status == "complete"
    assert_internal(r)


def test_declared_manual_source_hash_must_match_original(tmp_path):
    root, data = make_dossier(tmp_path, "<p>Синтетический материал</p>")
    data.pages[0].origin.source_sha256 = "0" * 64
    save_manifest(root, data)
    with pytest.raises(ValueError, match="acquisition hash"):
        run(root)


def test_no_go_from_recovered_source_even_if_manifest_claims_legal_identity(tmp_path):
    from lexradar.collector.models import EntityObservation

    root, data = make_dossier(tmp_path, '</span><form><input name="field"></form>')
    data.entities = [
        EntityObservation(
            source=data.target_url, raw_text="Injected declaration", name="ООО «Вымышленное лицо»"
        )
    ]
    save_manifest(root, data)
    r = run(root)
    assert not r.organizations and r.forms[0].static_coverage.status == "partial"
    assert_internal(r)
