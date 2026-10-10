"""Technical extraction coverage, never legal completeness or authenticated review."""

import re
from html.parser import HTMLParser
from typing import Literal

from pydantic import Field

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


class ReadingState(Model):
    original_saved: bool | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    extraction_state: Literal["unknown", "complete", "partial", "failed"] = "unknown"
    text_layer_extracted: bool | None = None
    text_truncated: bool | None = None
    original_chars: int | None = Field(default=None, ge=0)
    supplied_chars: int = Field(default=0, ge=0)
    page_count: int | None = Field(default=None, ge=1)
    pages: list[PageReading] = Field(default_factory=list, max_length=500)
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

    This is not browser error recovery. Omitted end tags, non-void slash syntax,
    and nested raw-text delimiters can withhold otherwise visible text. Only a
    strictly balanced extraction may be complete; retained prefixes remain
    partial, with an unknown total character count and explicit limitations.
    """

    EXCLUDED = {"script", "style", "head", "title", "noscript", "template"}
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

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.excluded = []
        self.open_elements = []
        self.limitations = []
        self.quarantined = False

    def uncertain(self, reason):
        if reason not in self.limitations:
            self.limitations.append(reason)
        self.quarantined = True

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.open_elements.append(tag)
        if tag in self.EXCLUDED:
            self.excluded.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag in self.VOID:
            return
        # In HTML a slash does not close non-void elements (notably script).
        self.handle_starttag(tag, attrs)
        self.uncertain("Self-closing non-void HTML element; suffix withheld")

    def handle_endtag(self, tag):
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


def extract_html(body: bytes, limit: int) -> tuple[str, ReadingState]:
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
            *parser.limitations,
        ],
    )
    if decode_loss:
        reading.limitations.append("UTF-8 decoding loss; complete text not established")
    return text, reading
