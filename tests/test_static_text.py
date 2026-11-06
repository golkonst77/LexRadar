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


@pytest.mark.parametrize("split", range(1, 57))
def test_incremental_nested_title_quarantines_suffix(split):
    html = "before<title>SECRET<title>SECRET</title>SECRET</title>after"
    parser = StaticText()
    parser.feed(html[:split])
    parser.feed(html[split:])
    parser.close()
    assert "".join(parser.parts) == "before"
    assert parser.quarantined
    assert parser.limitations


def test_svg_animation_does_not_quarantine_following_text():
    parser = StaticText()
    parser.feed('<body><svg><circle><animate attributeName="cx"/></circle></svg>'
                '<p>Policy after animation</p></body>')
    parser.close()
    assert not parser.limitations
    assert "Policy after animation" in " ".join(parser.parts)

    parser = StaticText()
    parser.feed('<body><animate/>Uncertain HTML</body>')
    parser.close()
    assert parser.quarantined
    assert not parser.parts


@pytest.mark.parametrize("tag", sorted(StaticText.SVG_SELF_CLOSING - {"svg"}))
def test_svg_self_closing_elements_preserve_strict_text(tag):
    text, reading = extract_html(
        f'<svg><defs><{tag} data-value="SECRET"/></defs></svg><p>Clinic</p>'.encode(), 1000
    )
    assert text == "Clinic"
    assert reading.extraction_state == "complete"
    assert reading.diagnostic_html is None


@pytest.mark.parametrize("container", ["", "<svg><foreignObject>", "<svg><desc>"])
@pytest.mark.parametrize("tag", ["feColorMatrix", "feFuncR", "path", "div", "script", "style", "title", "unknown"])
def test_svg_slash_exception_does_not_apply_to_html_or_excluded_elements(container, tag):
    text, reading = extract_html(f'{container}<{tag}/>SECRET'.encode(), 1000)
    assert text == ""
    assert reading.extraction_state == "partial"


@pytest.mark.parametrize("tag", ["script", "style", "title", "unknown", "div"])
def test_svg_context_does_not_allow_excluded_or_non_svg_slash_tags(tag):
    text, reading = extract_html(f'<svg><{tag}/>SECRET</svg>after'.encode(), 1000)
    assert text == ""
    assert reading.extraction_state == "partial"


def test_nested_svg_reenters_svg_context_inside_foreign_object():
    text, reading = extract_html(
        b'<svg><foreignObject><svg><feColorMatrix/></svg></foreignObject></svg>Clinic',
        1000,
    )
    assert text == "Clinic"
    assert reading.extraction_state == "complete"


@pytest.mark.parametrize('tag', ['ul', 'ol', 'menu'])
def test_optional_list_item_end_tags(tag):
    text, reading = extract_html(f'<{tag}><li>one<li>two</{tag}>after'.encode(), 1000)
    assert text == 'one two after'
    assert reading.extraction_state == 'complete'


@pytest.mark.parametrize('html', [
    '<ul><li>one</li></li><li>two</li></ul>after',
    '</li>one<ul><li>two</li></ul></li>after',
    '<ul><li>outer<ul></li><li>inner</ul></li></ul>after',
])
def test_stray_list_end_preserves_text_but_not_completeness(html):
    text, reading = extract_html(html.encode(), 1000)
    assert text.endswith('after')
    assert reading.extraction_state == 'partial'
    assert reading.original_chars is None
    assert not reading.text_coverage_complete


@pytest.mark.parametrize('html', [
    '<ul><li><span>before</li>SECRET</span></ul>',
    '<ul><li><template>before</li>SECRET</template></ul>',
    '<ul><li>before</li bogus>SECRET</ul>',
    '<svg></li>SECRET</svg>',
])
def test_list_recovery_does_not_cross_ambiguous_boundaries(html):
    text, reading = extract_html(html.encode(), 1000)
    assert 'SECRET' not in text
    assert reading.extraction_state == 'partial'
