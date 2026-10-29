"""Recovered search text must never become quotation or policy evidence."""

from datetime import UTC, datetime

import pytest

from lexradar.auditors.evidence import load_packet
from lexradar.auditors.grounding import quote_locations
from lexradar.collector import CrawlOptions, collect
from lexradar.collector.artifacts import ArtifactStore, verify_integrity
from lexradar.collector.models import CollectedEvidence, CollectionResult, Limits, PageObservation
from lexradar.collector.reading import extract_html
from lexradar.policy5 import inspect_policy5, inspect_policy_review
from lexradar.primary import audit_saved_dossier
from lexradar.proposal_rules import evaluate_rules
from lexradar.quality.fixtures import FixtureFetcher


@pytest.mark.parametrize(
    "html, strict, recovered",
    [
        ("<div/>searchable", "", "searchable"),
        ("<div><span>before</div>after</span>", "before", "before after"),
        ("<div>unclosed", "unclosed", "unclosed"),
        ('<div/><svg><filter><feColorMatrix values="ATTRIBUTE"/></filter></svg>clinic', "", "clinic"),
    ],
)
def test_container_errors_only_recover_diagnostic_text(html, strict, recovered):
    text, reading = extract_html(html.encode(), 1000)
    assert text == strict
    assert reading.extraction_state == reading.pages[0].extraction_state == "partial"
    assert reading.original_chars is reading.pages[0].original_chars is None
    assert not reading.text_coverage_complete
    assert not reading.legal_research_completed
    diagnostic = reading.diagnostic_html
    assert diagnostic.text == recovered
    assert diagnostic.extraction_state == "partial" and not diagnostic.evidence_eligible


@pytest.mark.parametrize("tag", ["script", "style", "template", "noscript", "head", "title"])
def test_diagnostic_excludes_content_attributes_and_comments(tag):
    html = f'<div/><p title="ATTRIBUTE">before</p><{tag}>SECRET</{tag}><!-- COMMENT -->after'
    _, reading = extract_html(html.encode(), 1000)
    assert reading.diagnostic_html.text == "before after"


@pytest.mark.parametrize(
    "suffix",
    [
        "<script/>SECRET</script>after",
        "<style/>SECRET</style>after",
        "<template/>SECRET</template>after",
        "<template>SECRET</style>SECRET</template>after",
        "<script>SECRET<script>SECRET</script>SECRET</script>after",
        "<template>SECRET",
        "<!-- SECRET",
    ],
)
def test_ambiguous_exclusion_still_quarantines_diagnostic_suffix(suffix):
    _, reading = extract_html(("<div/>before" + suffix).encode(), 1000)
    assert reading.diagnostic_html.text == "before"


def test_valid_html_and_diagnostic_limit():
    text, reading = extract_html(b"<p>valid<script>SECRET</script>text</p>", 1000)
    assert text == "valid text" and reading.extraction_state == "complete"
    assert reading.diagnostic_html is None
    text, reading = extract_html(b"<div/>long text", 4)
    assert text == "" and reading.diagnostic_html.text == "long"
    assert reading.diagnostic_html.text_truncated
    assert reading.original_chars is None


def test_static_collection_saves_both_channels_without_replacing_strict_artifact(tmp_path):
    fetcher = FixtureFetcher({"routes": {"/": {"html":
        '<html><body><div/><svg><filter><feColorMatrix/></filter></svg>'
        '<p>Searchable clinic</p><script>SECRET</script></body></html>'
    }}})
    root = tmp_path / "new-collection"
    data = collect(
        "https://benchmark.example/", root, Limits(max_pages=1, max_documents=0),
        fetcher=fetcher, static_only=True,
        crawl_options=CrawlOptions(min_delay_seconds=0.1, max_requests=4),
    )
    page = data.pages[0]
    assert page.text == ""
    assert page.reading.diagnostic_html.text == "Searchable clinic"
    assert page.reading.extraction_state == "partial"
    assert not page.reading.text_coverage_complete
    assert (root / "artifacts/page-0001.txt").read_bytes() == b""
    assert "Searchable clinic" in (root / "collection.json").read_text()
    assert verify_integrity(data, root) == []
    assert load_packet(root, 2_000_000).data.pages[0].reading.diagnostic_html == (
        page.reading.diagnostic_html
    )
    manifest = (root / "collection.json").read_bytes()
    report = audit_saved_dossier(root)
    assert len(report.stages) == 13 and report.production_outcome == "HOLD"
    assert (root / "collection.json").read_bytes() == manifest


def test_recovered_form_without_screenshot_preserves_integrity(tmp_path):
    from test_primary import make_dossier

    root, _ = make_dossier(tmp_path, '<div/><form><input name="phone"></form>')
    packet = load_packet(root, 2_000_000)
    form = packet.data.pages[0].forms[0]
    assert form.screenshot is None and form.unavailable_reason
    assert verify_integrity(packet.data, root) == []
    assert audit_saved_dossier(root).production_outcome == "HOLD"


def test_dossier_reproduces_diagnostics_but_never_uses_them_as_evidence(tmp_path):
    root = tmp_path / "dossier"
    store = ArtifactStore(root, Limits())
    body = ("<html><body><div/>Диагностическая цитата "
            + "Оператор: ООО «Клиника». Персональные данные посетителей. " * 20
            + "</body></html>").encode()
    text, reading = extract_html(body, 100000)
    artifacts = [store.save("page.html", body, "html"), store.save("page.txt", b"", "text")]
    evidence = CollectedEvidence(
        id="page-0001", source="https://clinic.test/privacy/", captured_at=datetime.now(UTC),
        observed_fact="Synthetic malformed HTML", available=True, status="partial",
        collection_errors=["Synthetic screenshot unavailable"], artifacts=artifacts,
        artifact_path=artifacts[0].path, sha256=artifacts[0].sha256,
    )
    data = CollectionResult(
        target_url=evidence.source, started_at=datetime.now(UTC), limits=Limits(),
        evidence=[evidence], pages=[PageObservation(
            requested_url=evidence.source, captured_at=evidence.captured_at,
            evidence_id=evidence.id, http_status=200, text=text, reading=reading,
        )],
    )
    # A manifest's diagnostic claim is untrusted: reproduce it from the hashed HTML.
    data.pages[0].reading.diagnostic_html.text = "FORGED"
    manifest = root / "collection.json"
    manifest.write_text(data.model_dump_json(), encoding="utf-8")
    saved = manifest.read_bytes()
    packet = load_packet(root, 2_000_000)
    page = packet.data.pages[0]
    assert page.text == ""
    assert "Диагностическая цитата" in page.reading.diagnostic_html.text
    assert "FORGED" not in page.reading.diagnostic_html.text
    assert page.reading.provenance == "reproduced"
    assert page.reading.extraction_state == "partial"
    assert not page.reading.text_coverage_complete
    assert "diagnostic_html" not in packet.payload
    assert "Диагностическая цитата" not in packet.payload
    assert quote_locations(packet, "Диагностическая цитата") == []
    assert all(r["status"] == "UNDEFINED" and not r["confirmed"] for r in inspect_policy5(packet))
    assert all(not r["confirmed"] for r in inspect_policy_review(packet))
    assert evaluate_rules(packet) == []
    assert verify_integrity(packet.data, root) == []
    assert manifest.read_bytes() == saved
    assert (root / artifacts[0].path).read_bytes() == body
