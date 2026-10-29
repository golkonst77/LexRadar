"""Technical extraction coverage, never legal completeness or authenticated review."""

import re
from html.parser import HTMLParser
from typing import Literal

from pydantic import Field, PositiveInt

from ..models import Model


class PageReading(Model):
    text_examination: Literal["not_examined", "acknowledged_excerpt", "acknowledged_text"] = (
        "not_examined"
    )
    page: int = Field(ge=1)
    original_chars: int | None = Field(default=None, ge=0)
    supplied_chars: int = Field(default=0, ge=0)
    text_available: bool = False
    extraction_state: Literal["unknown", "complete", "partial", "unread", "failed", "no_text"] = (
        "unknown"
    )
    truncated: bool | None = None
    visual_examined: Literal[False] = False
    limitations: list[str] = Field(default_factory=list)


class DiagnosticHTML(Model):
    text: str = Field(max_length=1_000_000)
    text_truncated: bool
    evidence_eligible: Literal[False] = False
    extraction_state: Literal["partial"] = "partial"
    scope: Literal["full_saved_html"] = "full_saved_html"
    notice: Literal["Diagnostic search only; not verified text or legal quotations"] = (
        "Diagnostic search only; not verified text or legal quotations"
    )


class ReadingState(Model):
    diagnostic_html: DiagnosticHTML | None = None
    original_saved: bool | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    extraction_state: Literal["unknown", "complete", "partial", "failed"] = "unknown"
    text_layer_extracted: bool | None = None
    text_truncated: bool | None = None
    original_chars: int | None = Field(default=None, ge=0)
    supplied_chars: int = Field(default=0, ge=0)
    page_count: int | None = Field(default=None, ge=1)
    pages: list[PageReading] = Field(default_factory=list, max_length=500)
    # Inclusive ranges preserve the unread tail of documents over 500 pages.
    unread_page_ranges: list[tuple[PositiveInt, PositiveInt]] = Field(default_factory=list)
    max_page_chars: int = Field(default=100_000, ge=1, le=100_000)
    max_total_chars: int = Field(default=1_000_000, ge=1, le=1_000_000)
    page_limit: int = Field(default=100, ge=1, le=500)
    provenance: Literal["unknown", "reproduced", "mismatch", "failed", "missing_original"] = (
        "unknown"
    )
    text_coverage_complete: bool = False
    visual_examined: Literal[False] = False
    legal_research_completed: Literal[False] = False
    limitations: list[str] = Field(default_factory=list)


class PDFInventory(Model):
    text: str | None = None
    page_texts: dict[int, str] = Field(default_factory=dict)
    reading: ReadingState


class StaticText(HTMLParser):
    """Conservative lexical extraction; ambiguous structure quarantines the suffix.

    This is not browser error recovery. Unsupported omitted end tags, non-void slash syntax,
    and nested raw-text delimiters can withhold otherwise visible text. Only a
    strictly balanced extraction may be complete; retained prefixes remain
    partial, with an unknown total character count and explicit limitations.
    """

    EXCLUDED = {"script", "style", "head", "title", "noscript", "template"}
    # Excluded title content needs no entity decoding. RAWTEXT delivers nested
    # delimiters intact instead of version-dependent RCDATA callback fragments.
    CDATA_CONTENT_ELEMENTS = (*HTMLParser.CDATA_CONTENT_ELEMENTS, "title")
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    # SVG 2 element names are lowercased by HTMLParser; raw-text stays excluded.
    SVG_SELF_CLOSING = set("""
        a animate animatemotion animatetransform circle clippath defs desc ellipse
        feblend fecolormatrix fecomponenttransfer fecomposite feconvolvematrix
        fediffuselighting fedisplacementmap fedistantlight fedropshadow feflood
        fefunca fefuncb fefuncg fefuncr fegaussianblur feimage femerge femergenode
        femorphology feoffset fepointlight fespecularlighting fespotlight fetile
        feturbulence filter foreignobject g image line lineargradient marker mask
        metadata mpath path pattern polygon polyline radialgradient rect set stop
        svg switch symbol text textpath tspan use view
    """.split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.excluded = []
        self.open_elements = []
        self.limitations = []
        self.quarantined = False
        self.main_start = self.main_end = None
        self.multiple_main = False

    def uncertain(self, reason):
        if reason not in self.limitations:
            self.limitations.append(reason)
        self.quarantined = True

    def handle_starttag(self, tag, attrs):
        if (
            tag == "li" and not self.excluded and "svg" not in self.open_elements
            and self.open_elements[-1:] == ["li"]
            and self.open_elements[-2:-1] in (["ul"], ["ol"], ["menu"])
        ):
            # A following list item implicitly closes the previous item.
            self.open_elements.pop()
        if tag == "main" and not self.excluded:
            if self.main_start is not None:
                self.multiple_main = True
            self.main_start = self.getpos()
        if tag == "body" and self.excluded == ["head"] and self.open_elements[-1:] == ["head"]:
            self.excluded.pop()
            self.open_elements.pop()
        # Python versions differ in whether title content is delivered as data
        # or parsed tags. Quarantine nested raw-text delimiters in either case.
        if self.excluded and self.excluded[-1] in {"script", "style", "title"}:
            if tag == self.excluded[-1]:
                self.uncertain("Nested raw-text opening delimiter; suffix withheld")
        if tag not in self.VOID:
            self.open_elements.append(tag)
        if tag in self.EXCLUDED:
            self.excluded.append(tag)

    def handle_startendtag(self, tag, attrs):
        # SVG integration points contain HTML until another SVG is opened.
        in_svg = next(
            (element == "svg" for element in reversed(self.open_elements)
             if element in {"svg", "foreignobject", "desc", "title"}),
            False,
        )
        if (
            tag in self.VOID
            or tag == "svg"
            or (in_svg and tag in self.SVG_SELF_CLOSING)
        ):
            # SVG foreign content supports self-closing path/rect/etc.
            return
        # In HTML a slash does not close non-void elements (notably script).
        self.handle_starttag(tag, attrs)
        self.uncertain("Self-closing non-void HTML element; suffix withheld")

    def handle_endtag(self, tag):
        if not self.excluded and "svg" not in self.open_elements:
            if (
                tag in {"ul", "ol", "menu"}
                and self.open_elements[-2:] == [tag, "li"]
            ):
                self.open_elements.pop()
            if tag == "li" and self.open_elements[-1:] != ["li"]:
                list_start = max(
                    (i for i, element in enumerate(self.open_elements)
                     if element in {"ul", "ol", "menu"}),
                    default=-1,
                )
                if "li" not in self.open_elements[list_start + 1:]:
                    reason = "Stray HTML list-item closing tag ignored; extraction remains partial"
                    if reason not in self.limitations:
                        self.limitations.append(reason)
                    return
        if tag == "main" and not self.excluded and self.main_start is not None:
            self.main_end = self.getpos()
        if not self.open_elements or self.open_elements[-1] != tag:
            self.uncertain("Mismatched HTML closing element; suffix withheld")
            return
        self.open_elements.pop()
        if tag in self.EXCLUDED:
            if not self.excluded or self.excluded[-1] != tag:
                self.uncertain("Ambiguous excluded HTML nesting; suffix withheld")
                return
            self.excluded.pop()

    def parse_endtag(self, i):
        end = self.rawdata.find(">", i)
        if end >= 0 and not re.fullmatch(
            r"</\s*[a-zA-Z][a-zA-Z0-9:._-]*\s*>", self.rawdata[i : end + 1]
        ):
            self.uncertain("Malformed HTML closing token; suffix withheld")
        return super().parse_endtag(i)

    def handle_data(self, data):
        if self.excluded and self.excluded[-1] in {"script", "style", "title"}:
            tag = self.excluded[-1]
            if re.search(r"<\s*" + tag + r"\b", data, re.IGNORECASE):
                self.uncertain("Nested raw-text opening delimiter; suffix withheld")
        if not self.excluded and not self.quarantined:
            self.parts.append(data)

    def close(self):
        # HTMLParser may emit incomplete markup as data at EOF. Never admit it.
        if self.rawdata:
            self.uncertain("Incomplete HTML token or raw-text block; suffix withheld")
        super().close()
        if self.open_elements or self.excluded:
            self.uncertain("Unclosed HTML elements; complete extraction not established")


class _DiagnosticText(StaticText):
    """Tolerate ordinary container imbalance, never ambiguous excluded boundaries."""

    def handle_startendtag(self, tag, attrs):
        if tag not in self.EXCLUDED:
            # Container slash syntax is ambiguous; this channel establishes no evidence.
            return
        super().handle_startendtag(tag, attrs)

    def handle_endtag(self, tag):
        if not self.excluded and tag not in self.EXCLUDED:
            if tag in self.open_elements:
                index = len(self.open_elements) - 1 - self.open_elements[::-1].index(tag)
                del self.open_elements[index:]
            return
        super().handle_endtag(tag)


def extract_html(body: bytes, limit: int, *, main_only: bool = False) -> tuple[str, ReadingState]:
    """Reproducible static UTF-8 extraction; no CSS visibility, JS or external resources."""
    parser = StaticText()
    decode_loss = False
    try:
        decoded = body.decode("utf-8")
    except UnicodeError:
        decoded = body.decode("utf-8", errors="replace")
        decode_loss = True
    parser.feed(decoded)
    parser.close()
    scoped = False
    if main_only and parser.main_start and parser.main_end and not parser.multiple_main:
        lines = decoded.splitlines(True)
        start = sum(len(x) for x in lines[: parser.main_start[0] - 1]) + parser.main_start[1]
        end = sum(len(x) for x in lines[: parser.main_end[0] - 1]) + parser.main_end[1]
        end = decoded.find(">", end) + 1
        content = StaticText()
        content.feed(decoded[start:end])
        content.close()
        if not content.limitations and not content.quarantined:
            parser = content
            scoped = True
    original = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    text = original[:limit]
    truncated = len(original) > limit
    state = "partial" if truncated or decode_loss or parser.limitations else "complete"
    original_chars = None if parser.limitations else len(original)
    reading = ReadingState(
        extraction_state=state,
        text_layer_extracted=True,
        text_truncated=truncated,
        original_chars=original_chars,
        supplied_chars=len(text),
        page_count=1,
        max_total_chars=min(limit, 1_000_000),
        pages=[
            PageReading(
                page=1,
                original_chars=original_chars,
                supplied_chars=len(text),
                text_available=bool(text.strip()),
                extraction_state=state,
                truncated=truncated,
                limitations=list(parser.limitations),
            )
        ],
        limitations=[
            "Static UTF-8 HTML text; CSS visibility/layout and JavaScript not examined",
            "Extraction is not legal research",
            *(["Complete static main content; outer navigation excluded"] if scoped else []),
            *parser.limitations,
        ],
    )
    if decode_loss:
        reading.limitations.append("UTF-8 decoding loss; complete text not established")
    if parser.limitations or decode_loss:
        diagnostic = _DiagnosticText()
        diagnostic.feed(decoded)
        diagnostic.close()
        recovered = re.sub(r"\s+", " ", " ".join(diagnostic.parts)).strip()
        diagnostic_limit = min(limit, 1_000_000)
        reading.diagnostic_html = DiagnosticHTML(
            text=recovered[:diagnostic_limit], text_truncated=len(recovered) > diagnostic_limit
        )
    return text, reading


RECOVERED_HTML = "Source-accounted HTML recovery; original text spans preserved"


class _SourceAccountedText(HTMLParser):
    """Ignore ordinary container imbalance, but account for every source token."""

    CDATA_CONTENT_ELEMENTS = (*HTMLParser.CDATA_CONTENT_ELEMENTS, "title", "textarea", "iframe")
    EXCLUDED = StaticText.EXCLUDED | {"iframe", "object"}
    BLOCKS = {
        "body",
        "main",
        "div",
        "p",
        "li",
        "ul",
        "ol",
        "table",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "br",
        "hr",
        "pre",
    }

    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.lines = [0]
        for match in re.finditer("\n", source):
            self.lines.append(match.end())
        self.spans = []
        self.parts = []
        self.main_parts = []
        self.excluded = []
        self.errors = []
        self.body_open = self.body_close = self.html_close = 0
        self.main_open = self.main_close = 0
        self.in_body = self.in_main = False
        self.after_html = False
        self.in_textarea = False

    def visible(self, data):
        self.parts.append(data)
        if self.in_main:
            self.main_parts.append(data)

    def record(self, raw, kind):
        line, column = self.getpos()
        start = self.lines[line - 1] + column
        end = start + len(raw)
        if self.source[start:end] != raw or start != (self.spans[-1][1] if self.spans else 0):
            self.errors.append("Source token gap, overlap or mismatch")
        self.spans.append((start, end, kind))

    def handle_starttag(self, tag, attrs):
        self.record(self.get_starttag_text(), "markup")
        if tag == "head" and self.body_open:
            self.errors.append("Head element after explicit body start")
        if self.after_html and tag not in {"script", "style"}:
            self.errors.append("Markup after the document boundary")
        if tag == "body":
            if self.excluded == ["head"]:
                self.excluded.pop()
            if self.excluded:
                self.errors.append("Body inside excluded content")
            else:
                self.body_open += 1
                self.in_body = True
        if tag == "main" and not self.excluded:
            self.main_open += 1
            self.in_main = True
        if tag in {"plaintext", "xmp", "listing"}:
            self.errors.append("Unsupported raw-text element")
        if tag == "textarea":
            self.in_textarea = True
        if tag in self.BLOCKS and not self.excluded:
            self.visible("\n")
        if tag in self.EXCLUDED:
            self.excluded.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.record(self.get_starttag_text(), "markup")
        if tag in self.BLOCKS and not self.excluded:
            self.visible("\n")
        if tag in self.EXCLUDED or tag in {"body", "html", "main", "textarea"}:
            self.errors.append("Self-closing scope or excluded element")

    def handle_endtag(self, tag):
        line, column = self.getpos()
        start = self.lines[line - 1] + column
        end = self.source.find(">", start)
        raw = self.source[start : end + 1] if end >= 0 else ""
        self.record(raw, "markup")
        normal_close = re.fullmatch(r"</\s*[a-zA-Z][a-zA-Z0-9:._-]*\s*>", raw)
        # A missing > before div/input is one end-tag token, containing no visible text.
        swallowed_div = tag == "div" and re.fullmatch(
            r"</div\s+<(?:div|input)\s+[^<>]*>", raw, re.I
        )
        if not normal_close and not swallowed_div:
            self.errors.append("Malformed closing token")
        if tag in self.EXCLUDED:
            if not self.excluded or self.excluded[-1] != tag:
                self.errors.append("Unbalanced excluded boundary")
            else:
                self.excluded.pop()
        if tag == "textarea":
            self.in_textarea = False
        if tag in self.BLOCKS and not self.excluded:
            self.visible("\n")
        if tag == "main" and not self.excluded:
            if not self.in_main:
                self.errors.append("Main boundary out of order")
            self.main_close += 1
            self.in_main = False
        if tag == "body" and not self.excluded:
            self.body_close += 1
            self.in_body = False
        if tag == "html" and not self.excluded:
            if self.in_body:
                self.errors.append("Document end precedes the explicit body end")
            self.html_close += 1
            self.after_html = True

    def handle_data(self, data):
        self.record(data, "excluded" if self.excluded else "text")
        if not self.excluded:
            if data.strip() and (not self.in_body or self.after_html):
                self.errors.append("Non-whitespace text outside explicit body")
            if self.in_textarea:
                from html import unescape

                data = unescape(data)
            self.visible(data)

    def handle_entityref(self, name):
        self.entity(r"&[a-zA-Z][a-zA-Z0-9]*;?")

    def handle_charref(self, name):
        self.entity(r"&#(?:[xX][0-9a-fA-F]+|[0-9]+);?")

    def entity(self, pattern):
        from html import unescape

        line, column = self.getpos()
        start = self.lines[line - 1] + column
        match = re.match(pattern, self.source[start:])
        if not match:
            self.errors.append("Unresolved entity token")
            return
        raw = match[0]
        self.record(raw, "excluded" if self.excluded else "text")
        if not self.excluded:
            if not self.in_body:
                self.errors.append("Entity outside explicit body")
            self.visible(unescape(raw))

    def handle_comment(self, data):
        line, column = self.getpos()
        start = self.lines[line - 1] + column
        if not self.source.startswith("<!--", start):
            self.errors.append("Ambiguous bogus comment")
            return
        end = self.source.find("-->", start + 4)
        if end < 0:
            self.errors.append("Unclosed comment")
            return
        self.record(self.source[start : end + 3], "comment")

    def handle_decl(self, decl):
        self.record("<!" + decl + ">", "declaration")
        if not re.fullmatch(r"DOCTYPE\s+html", decl, re.I):
            self.errors.append("Unsupported declaration")

    def handle_pi(self, data):
        self.errors.append("Unsupported processing instruction")

    def unknown_decl(self, data):
        self.errors.append("Unsupported declaration")


def recover_html(body: bytes, limit: int) -> tuple[str, ReadingState, dict]:
    """Prove lexical source coverage, rather than trusting tolerant parser success."""
    # shortcut: lossless UTF-8 and explicit document boundaries only; extend with source proofs.
    from hashlib import sha256

    try:
        source = body.decode("utf-8")
    except UnicodeError:
        return (
            "",
            ReadingState(
                extraction_state="failed", limitations=["Recovery requires lossless UTF-8"]
            ),
            {
                "complete": False,
                "errors": ["UTF-8 decoding loss"],
                "source_sha256": sha256(body).hexdigest(),
            },
        )
    parser = _SourceAccountedText(source)
    parser.feed(source)
    pending = bool(parser.rawdata)
    parser.close()
    if pending or parser.rawdata:
        parser.errors.append("Incomplete lexical token or raw-text block")
    if not parser.spans or parser.spans[-1][1] != len(source):
        parser.errors.append("Unaccounted source tail")
    if parser.excluded:
        parser.errors.append("Unclosed excluded content")
    if (parser.body_open, parser.body_close, parser.html_close) != (1, 1, 1):
        parser.errors.append("Explicit unique body and document end required")
    complete = not parser.errors
    main_scope = parser.main_open == parser.main_close == 1
    parts = parser.main_parts if main_scope else parser.parts
    original = re.sub(r"\s+", " ", "".join(parts)).strip()
    text = original[:limit]
    truncated = len(original) > limit
    proof = dict(
        scope="main" if main_scope else "body",
        complete=complete and not truncated,
        source_sha256=sha256(body).hexdigest(),
        source_chars=len(source),
        accounted_chars=sum(b - a for a, b, _ in parser.spans),
        spans=parser.spans,
        errors=list(dict.fromkeys(parser.errors)),
        original_text_sha256=sha256(original.encode()).hexdigest(),
        explicit_body_count=parser.body_open,
        explicit_body_end_count=parser.body_close,
        explicit_html_end_count=parser.html_close,
        text_truncated=truncated,
    )
    reading = ReadingState(
        extraction_state="complete" if proof["complete"] else "partial",
        text_layer_extracted=True,
        text_truncated=truncated,
        original_chars=len(original) if complete else None,
        supplied_chars=len(text),
        page_count=1,
        max_total_chars=min(limit, 1_000_000),
        limitations=[
            RECOVERED_HTML,
            "Static text only; CSS visibility and JavaScript not examined",
            "Completeness concerns the saved source, not all site documents",
            *proof["errors"],
        ],
    )
    return text, reading, proof
