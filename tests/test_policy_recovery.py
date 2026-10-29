"""Recovery completeness comes from source coverage and bounded exclusions."""

import hashlib

import pytest

from lexradar.collector.reading import RECOVERED_HTML, extract_html, recover_html


def test_recovery_accounts_for_every_source_span_despite_bad_formatting():
    source = (
        "<!doctype html><html><head><title>Политика</title></head><body>"
        "<div><p>Обработк&#1072; <strong>персональных</strong> данных</strong></p>"
        "<p>Последний раздел — уничтожение данных.</p></body></html>"
    ).encode()
    _, strict = extract_html(source, 10000)
    assert strict.extraction_state == "partial"
    text, reading, proof = recover_html(source, 10000)
    assert text == "Обработка персональных данных Последний раздел — уничтожение данных."
    assert proof["complete"] and reading.extraction_state == "complete"
    assert RECOVERED_HTML in reading.limitations
    assert proof["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert proof["accounted_chars"] == proof["source_chars"]
    previous = 0
    for start, end, kind in proof["spans"]:
        assert start == previous and end >= start
        previous = end
    assert previous == len(source.decode())


def test_excluded_source_is_accounted_but_not_leaked_into_policy():
    source = (
        b"<html><head><title>SECRET</title></head><body><div><span></div>"
        b'visible<script>var s="<script>SECRET";</script>'
        b"<template><p>SECRET</p></template><style>SECRET</style>"
        b"<iframe>SECRET</iframe>final</body></html>"
    )
    text, reading, proof = recover_html(source, 10000)
    assert proof["complete"] and "SECRET" not in text
    assert text == "visiblefinal"
    assert any(kind == "excluded" for _, _, kind in proof["spans"])


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body><p>Lots of text</p>",
        b"<html><body>text<!-- unclosed</body></html>",
        b"<html><body>text<script>unclosed</body></html>",
        b"<html><body><template>SECRET</body></html>",
        b"<html><body><template/>SECRET</template></body></html>",
        b"<html><body>text</template>SECRET</body></html>",
        b"<html><body>text<div</body></html>",
        b"<html><body>text</body></html><div>tail</div>",
        b"<html><body>text</body></html>tail",
        b"<html><body>text</body></body></html>",
        b"<html><body><plaintext>not a complete document</body></html>",
        b"<html><body><p>text</p bogus></body></html>",
        b"<html><body>\xff</body></html>",
    ],
)
def test_incomplete_or_ambiguous_source_never_becomes_complete(body):
    _, reading, proof = recover_html(body, 10000)
    assert not proof["complete"] and proof["errors"]
    assert reading.extraction_state != "complete"
    assert not reading.text_coverage_complete


def test_truncation_and_double_body_prevent_complete_claim():
    text, reading, proof = recover_html(b"<html><body>long text</body></html>", 3)
    assert text == "lon" and reading.text_truncated and not proof["complete"]
    _, _, proof = recover_html(b"<html><body>A</body><body>B</body></html>", 1000)
    assert not proof["complete"]


def test_recovery_reproduces_exactly_and_default_extractor_stays_conservative():
    source = b"<html><body><div><span></div>tail</body></html>"
    assert extract_html(source, 1000)[1].extraction_state == "partial"
    first = recover_html(source, 1000)
    assert first == recover_html(source, 1000)
    assert first[2]["complete"]


def test_unique_main_scope_keeps_navigation_from_padding_a_short_policy():
    source = (
        b"<html><body><nav>navigation "
        + b"word " * 100
        + b"</nav><main>placeholder</main><footer>tail</footer></body></html>"
    )
    text, _, proof = recover_html(source, 10000)
    assert proof["complete"] and proof["scope"] == "main"
    assert text == "placeholder"


def test_block_boundaries_and_excluded_postlude_preserve_visible_text():
    source = b"<html><body>A<br/>B</body></html><script>SECRET</script>"
    text, _, proof = recover_html(source, 1000)
    assert proof["complete"] and text == "A B"


def test_specific_swallowed_div_markup_preserves_all_text_and_records_its_span():
    source = b'<html><body>before</div\n <div class="form-group">after</body></html>'
    text, _, proof = recover_html(source, 1000)
    assert proof["complete"] and text == "before after"
    assert proof["accounted_chars"] == len(source)


@pytest.mark.parametrize("change", ["none", "wrong_derivative", "truncated_source"])
def test_integrity_reproduction_is_required_before_full_coverage(tmp_path, change):
    from types import SimpleNamespace
    from lexradar.collector.models import PageObservation
    from lexradar.collector.text_integrity import recheck_text

    source = b"<html><body><div><span></div>entire policy tail</body></html>"
    if change == "truncated_source":
        source = source[:-14]
    text, reading, _ = recover_html(source, 10000)
    (tmp_path / "page.html").write_bytes(source)
    (tmp_path / "page.txt").write_text(text)
    page = PageObservation(
        requested_url="https://clinic.test/privacy/",
        evidence_id="p",
        captured_at="2026-10-28T10:00:00Z",
        http_status=200,
        text="invented text" if change == "wrong_derivative" else text,
        reading=reading,
    )
    artifacts = [
        SimpleNamespace(kind="html", path="page.html", sha256=hashlib.sha256(source).hexdigest()),
        SimpleNamespace(kind="text", path="page.txt"),
    ]
    data = SimpleNamespace(
        pages=[page], documents=[], evidence=[SimpleNamespace(id="p", artifacts=artifacts)]
    )
    recheck_text(data, tmp_path)
    assert page.reading.text_coverage_complete == (change == "none")
    assert page.reading.provenance == ("mismatch" if change == "wrong_derivative" else "reproduced")


@pytest.mark.parametrize("kind", ["div", "input"])
def test_only_known_nontext_container_damage_is_tolerated(kind):
    source = ("<html><body>before</div\n <" + kind + ' class="x">after</body></html>').encode()
    text, _, proof = recover_html(source, 1000)
    assert proof["complete"] and text == "before after"


def test_main_boundaries_must_be_in_source_order():
    _, _, proof = recover_html(b"<html><body></main><main>tail</body></html>", 1000)
    assert not proof["complete"]


def test_document_end_must_follow_explicit_body_end():
    _, _, proof = recover_html(b'<html><body>text</html></body>', 1000)
    assert not proof['complete']
