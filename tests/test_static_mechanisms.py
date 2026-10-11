"""Saved synthetic source only; static declarations never establish processing."""

import json
import socket

import pytest
from test_primary import EXAMPLES, make_dossier, run

from lexradar.primary.cli import write_report
from lexradar.primary.inline import form_routes, resources
from lexradar.primary.mechanisms import MechanismMarkup, normalize_reference


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Static analysis cannot perform external actions")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr("lexradar.collector.network.Fetcher.get", blocked)
    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", blocked)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", blocked)


def inspect(source):
    parser = MechanismMarkup("https://clinic.example/")
    parser.feed(source)
    parser.close()
    return parser


def test_form_routes_are_separate_unknown_and_hidden_values_not_retained(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        '<form id="f" action="#" method="POST" '
        'data-href-ajax="php/callback.php" onsubmit="fetch(\'/other\')" '
        'data-handler="submitClinic"><input type="hidden" name="doctor" '
        'value="DO_NOT_RETAIN"><input name="phone"></form>',
    )
    report = run(root)
    form = report.forms[0]
    mechanism = form.mechanism
    assert mechanism.declared_method == "POST"
    assert len(mechanism.html_route_ids) == len(mechanism.alternative_route_ids) == 1
    assert len(mechanism.js_route_candidate_ids) == 1
    assert len(mechanism.handler_declaration_ids) == 2
    assert mechanism.hidden_field_names == ["doctor"]
    assert mechanism.actual_route == mechanism.actual_recipient == "unknown"
    assert "DO_NOT_RETAIN" not in report.model_dump_json()
    assert report.production_outcome == "HOLD"


def test_booking_group_preserves_three_occurrences_and_queries(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        "".join(
            '<a href="https://BOOKING.medflex.ru:443/A?clinic=1&amp;doctor=2">Запись</a>'
            for _ in range(3)
        ),
    )
    report = run(root)
    group = report.integration_groups[0]
    assert group.kind == "booking" and len(group.occurrence_ids) == 3
    assert group.normalized_url == "https://booking.medflex.ru/A?clinic=1&doctor=2"
    assert group.independent_form_count == 0 and not group.network_action_confirmed
    assert not report.forms
    assert len([h for h in report.hypotheses if h.stage_id == "09"]) == 1
    assert all("&amp;" in d.original_value for d in report.declarations)
    assert len({d.citation.static_span.start.column for d in report.declarations}) == 3


@pytest.mark.parametrize(
    "value",
    [
        "http://[broken",
        "javascript:alert(1)",
        "https://user@example.org/",
        "https://x.example/a b",
        "https://x.example:99999/",
        "https://x.example/\\a",
    ],
)
def test_unsafe_normalization_is_withheld(value):
    assert normalize_reference("https://clinic.example/", value) is None


def test_normalization_retains_significant_parameters():
    assert normalize_reference("https://clinic.example/", "/A?x=1&x=2&sig=%2F#part") == (
        "https://clinic.example/A?x=1&x=2&sig=%2F#part"
    )


@pytest.mark.parametrize(
    "body,expected",
    [
        ('<div id="p">Политика согласие</div>', "declared_unique_target"),
        ('<div id="p">Политика</div><div id="p">Согласие</div>', "ambiguous_target"),
        ('<div id="other">Политика</div>', "target_not_inspected"),
    ],
)
def test_popup_linkage_not_visibility_or_legal_reading(tmp_path, body, expected):
    root, _ = make_dossier(tmp_path, '<span data-src="#p" data-fancybox>Открыть</span>' + body)
    report = run(root)
    popup = report.popup_relations[0]
    assert popup.relation_status == expected
    assert not popup.display_confirmed and not popup.document_legally_reviewed
    assert all(d.citation.artifact_sha256 and d.citation.static_span for d in report.declarations)
    if expected != "target_not_inspected":
        assert popup.classification_citations
        assert "policy_text_candidate" in popup.candidate_kinds


def test_resources_gtm_ga_metrika_maps_noscript_and_lazy_separate(tmp_path):
    head = '<script>j.src="https://www.googletagmanager.com/gtm.js?id="+i;</script>'
    body = (
        '<noscript><iframe src="https://www.googletagmanager.com/ns.html?id=X"></iframe></noscript>'
    )
    body += '<script src="https://www.googletagmanager.com/gtag/js?id=Y"></script>'
    body += '<script src="https://mc.yandex.ru/metrika/tag.js"></script>'
    body += '<script src="https://api-maps.yandex.ru/services/constructor/1/js/"></script>'
    body += '<iframe data-src="https://booking.medflex.ru/clinic"></iframe>'
    body += '<script src="/jquery.form.min.js"></script>'
    root, _ = make_dossier(tmp_path, body, head=head)
    report = run(root)
    assert {d.service_kind for d in report.declarations} >= {
        "google_tag_manager",
        "google_analytics",
        "yandex_metrika",
        "yandex_maps",
        "medflex",
        "form_library_candidate",
    }
    inline = next(d for d in report.declarations if d.declaration_type == "inline_resource")
    assert inline.normalized_value is None and "Dynamic" in " ".join(inline.limitations)
    assert any(d.declaration_type == "noscript_iframe" for d in report.declarations)
    assert all(
        not d.execution_confirmed
        and not d.data_transfer_confirmed
        and d.actual_recipient == "unknown"
        for d in report.declarations
    )


@pytest.mark.parametrize(
    "source",
    [
        '// j.src="https://x.example/";',
        '/* j.src="https://x.example/"; */',
        "let example='j.src=\"https://x.example/\";';",
        'let example=`j.src="https://x.example/";`;',
        'let regex=/j.src="https://x.example/"/;',
    ],
)
def test_quoted_comments_templates_regex_do_not_become_resource(source):
    assert resources(source)[0] == []


def test_exact_form_js_link_and_ambiguous_ids(tmp_path):
    body = '<form id="f"><input name="phone"></form>'
    js = "document.querySelector('#f').addEventListener('submit',function(e){fetch('/submit');});"
    assert form_routes(js)[0]
    root, _ = make_dossier(tmp_path, body + "<script>" + js + "</script>")
    report = run(root)
    assert len(report.forms[0].mechanism.js_route_candidate_ids) == 1
    ambiguous = inspect(body + body + "<script>" + js + "</script>")
    assert not any(d.kind == "js_route" for d in ambiguous.declarations)
    assert ambiguous.mechanism_limits


@pytest.mark.parametrize(
    "source",
    [
        '<template><script>j.src="https://x.example/";</script></template>',
        '<script type="application/json">j.src="https://x.example/";</script>',
        '<script>j.src="https://x.example/";',
        '<style>j.src="https://x.example/";</style>',
    ],
)
def test_opaque_or_unclosed_blocks_not_analysed(source):
    assert not inspect(source).declarations


def test_partial_html_never_upgrades_declared_source(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        '<form action="#" data-url="/send"><input name="a">'
        '</div><input name="not_inspected"></form>',
    )
    report = run(root)
    assert report.declarations and all(d.completeness == "partial" for d in report.declarations)
    assert all(d.legal_status == "not_confirmed" for d in report.declarations)
    assert report.production_outcome == "HOLD" and not report.client_release_allowed
    assert report.forms[0].mechanism.actual_route == "unknown"


def test_inline_size_bound_no_partial_resource_claim():
    parser = inspect("<script>" + " " * 32769 + 'j.src="https://x.example/";</script>')
    assert not parser.declarations and parser.mechanism_limits


def test_declaration_limit_stops_suffix():
    parser = MechanismMarkup("https://clinic.example/")
    parser.MAX_DECLARATIONS = 2
    parser.feed(
        '<form action="#" method="post" data-url="/send"></form>'
        '<a href="https://booking.medflex.ru/">Запись</a>'
    )
    parser.close()
    assert parser.quarantined and parser.limitations
    assert len(parser.declarations) <= 2
    assert not any(d.kind == "booking_anchor" for d in parser.declarations)


def test_export_exact_source_ranges_and_determinism(tmp_path):
    root, _ = make_dossier(tmp_path, '<a href="https://booking.medflex.ru/?a=1&amp;b=2">Запись</a>')
    first, second = run(root), run(root)
    assert first.model_dump_json() == second.model_dump_json()
    declaration = first.declarations[0]
    source = (root / declaration.citation.artifact_path).read_text()
    span = declaration.citation.static_span
    lines = source.splitlines(keepends=True)

    def offset(pos):
        return sum(map(len, lines[: pos.line - 1])) + pos.column

    region = source[offset(span.start) : offset(span.end_exclusive)]
    assert declaration.original_value in region
    output = tmp_path / "result"
    write_report(first, output, root)
    assert json.loads((output / "declarations.json").read_text())["rows"][0] == (
        declaration.model_dump(mode="json")
    )
    assert not json.loads((output / "integration-groups.json").read_text())[
        "client_release_allowed"
    ]


def test_old_fixture_still_has_thirteen_stages_and_hold():
    report = run(EXAMPLES / "primary-audit/dossier", sector="medical")
    assert len(report.stages) == 13 and report.production_outcome == "HOLD"


@pytest.mark.parametrize(
    "body",
    [
        '<form action="/a" action="/b"><input name="x"></form>',
        '<form data-url="/a" data-url="/b"><input name="x"></form>',
        '<template><form action="/a"><input name="x"></form></template>',
    ],
)
def test_ambiguous_or_template_routes_not_acquired(body):
    parser = inspect(body)
    assert not any(d.kind in {"html_action", "data_route"} for d in parser.declarations)


def test_popup_citation_stops_at_quarantine_boundary(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        '<a href="#p">Открыть</a><div id="p">Политика'
        '<form><input name="a"></div>НЕОБСЛЕДОВАННЫЙ ХВОСТ',
    )
    report = run(root)
    citation = report.popup_relations[0].classification_citations[0]
    source = (root / citation.artifact_path).read_text()
    span = citation.static_span
    assert span.status == "partial"
    assert span.end_exclusive.column == source.index("</div>")
    assert not report.popup_relations[0].display_confirmed


def test_explicit_zero_port_is_not_silently_removed():
    assert normalize_reference("https://clinic.example/", "https://booking.medflex.ru:0/A") == (
        "https://booking.medflex.ru:0/A"
    )
