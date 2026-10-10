"""Offline regressions for excluded HTML and conservative extraction coverage."""

import pytest

from lexradar.collector.reading import StaticText, extract_html


@pytest.mark.parametrize("tag", sorted(StaticText.EXCLUDED))
def test_excluded_area(tag):
    text, reading = extract_html(f"<div>before<{tag}>SECRET</{tag}>after</div>".encode(), 1000)
    assert text == "before after"
    assert reading.extraction_state == "complete"


@pytest.mark.parametrize("tag", ["template", "noscript", "head"])
def test_same_name_nested_exclusions_close_only_last(tag):
    text, reading = extract_html(
        f"before<{tag}>SECRET1<{tag}>SECRET2</{tag}>SECRET3</{tag}>after".encode(),
        1000,
    )
    assert text == "before after"
    assert reading.extraction_state == "complete"


def test_mixed_nested_exclusions():
    text, reading = extract_html(
        b"before<template><noscript>SECRET<head><title>SECRET</title>"
        b"</head>SECRET</noscript>SECRET</template>after",
        1000,
    )
    assert text == "before after"
    assert reading.extraction_state == "complete"


@pytest.mark.parametrize(
    "suffix",
    [
        "<template><template>SECRET</template>SECRET</noscript>SECRET</template>after",
        "<template><noscript>SECRET</template>SECRET</noscript>after",
        "<template>SECRET</style>SECRET</template>after",
        "</template>SECRET",
        "<div><span>visible</div>SECRET</span>",
        "<template>SECRET",
        "<script>SECRET",
        "<style>SECRET",
        "<head><title>SECRET</title>",
        "<template/>SECRET</template>after",
        "<script/>SECRET</script>after",
        "<script>SECRET<script>SECRET</script>SECRET</script>after",
        "<style>SECRET<style>SECRET</style>SECRET</style>after",
        "<title>SECRET<title>SECRET</title>SECRET</title>after",
        "<template>SECRET</template bogus>SECRET",
        "<template>SECRET</>SECRET</template>after",
        "<script",
        "<!-- SECRET",
        "<div",
        "<div>visible",
    ],
)
def test_malformed_structure_never_leaks_excluded_suffix(suffix):
    text, reading = extract_html(("before" + suffix).encode(), 1000)
    assert "SECRET" not in text
    assert "after" not in text
    assert text.startswith("before")
    assert reading.extraction_state == "partial"
    assert reading.pages[0].extraction_state == "partial"
    assert reading.pages[0].limitations
    assert reading.original_chars is None
    assert reading.pages[0].original_chars is None
    assert not reading.text_coverage_complete
    assert not reading.legal_research_completed


def test_void_elements_entities_and_comments_do_not_break_valid_extraction():
    text, reading = extract_html(
        b"<div>before<br/><img src='local'> &lt;script&gt;literal&lt;/script&gt;"
        b"<!-- hidden --><input>after</div>",
        1000,
    )
    assert text == "before <script>literal</script> after"
    assert reading.extraction_state == "complete"


def test_truncation_and_malformed_structure_preserve_both_limitations():
    text, reading = extract_html(b"before visible<template>SECRET", 4)
    assert text == "befo"
    assert reading.text_truncated
    assert reading.pages[0].truncated
    assert reading.extraction_state == "partial"
    assert reading.original_chars is None
    assert reading.supplied_chars == 4
    assert reading.pages[0].limitations


def test_valid_truncation_keeps_known_original_length():
    text, reading = extract_html(b"<p>before<template>SECRET</template>after</p>", 4)
    assert text == "befo"
    assert reading.extraction_state == "partial"
    assert reading.original_chars == len("before after")
    assert reading.text_truncated


def test_incremental_parser_preserves_excluded_stack():
    parser = StaticText()
    for chunk in ["before<template>", "<template>SECRET</template>", "SECRET</template>after"]:
        parser.feed(chunk)
    parser.close()
    assert parser.parts == ["before", "after"]
    assert not parser.limitations
