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
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.excluded = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head", "title", "noscript", "template"}:
            self.excluded.append(tag)

    def handle_endtag(self, tag):
        if tag in self.excluded:
            self.excluded = self.excluded[: self.excluded.index(tag)]

    def handle_data(self, data):
        if not self.excluded:
            self.parts.append(data)


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
    original = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    text = original[:limit]
    truncated = len(original) > limit
    state = "partial" if truncated or decode_loss else "complete"
    reading = ReadingState(
        extraction_state=state,
        text_layer_extracted=True,
        text_truncated=truncated,
        original_chars=len(original),
        supplied_chars=len(text),
        page_count=1,
        max_total_chars=min(limit, 1_000_000),
        pages=[
            PageReading(
                page=1,
                original_chars=len(original),
                supplied_chars=len(text),
                text_available=bool(text.strip()),
                extraction_state=state,
                truncated=truncated,
            )
        ],
        limitations=[
            "Static UTF-8 HTML text; CSS visibility/layout and JavaScript not examined",
            "Extraction is not legal research",
        ],
    )
    if decode_loss:
        reading.limitations.append("UTF-8 decoding loss; complete text not established")
    return text, reading
