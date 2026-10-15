"""Inspect saved static attributes only; never execute scripts, resolve DNS or submit forms."""

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from ..collector.models import InputField
from ..collector.reading import StaticText
from .models import HTMLPosition, ParseDiagnostic, StaticFormCoverage, StaticInspection, StaticSpan


def compact(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def static_reference(base: str, value: str) -> str:
    try:
        return urljoin(base, value)
    except ValueError:
        # Preserve a malformed declared reference as data, without interpreting/following it.
        return value


def link_kind(url: str, text: str) -> str | None:
    for value in (url.casefold(), text.casefold()):
        if any(word in value for word in ("политик", "privacy", "policy", "politika")):
            return "policy"
        if any(word in value for word in ("реклам", "маркетинг", "advertising", "marketing")):
            return "advertising"
        if any(word in value for word in ("соглас", "consent", "soglas")):
            return "consent"
    return None


@dataclass
class Link:
    url: str
    form_index: int | None
    parts: list[str] = field(default_factory=list)
    span: StaticSpan | None = None

    @property
    def text(self):
        return compact(self.parts)


@dataclass
class SavedForm:
    index: int
    action: str | None
    purpose: str | None
    fields: list[InputField] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)
    elements: list[str] = field(default_factory=list)
    start: tuple[int, int] | None = None
    end_exclusive: tuple[int, int] | None = None
    recovered: bool = False
    field_spans: list[StaticSpan] = field(default_factory=list)


@dataclass
class TextRegion:
    start: tuple[int, int]
    end: tuple[int, int] | None = None
    parts: list[str] = field(default_factory=list)
    recovered: bool = False


class SavedMarkup(StaticText):
    """Bounded, conservative extraction; inherited ambiguity handling withholds suffixes."""

    def __init__(self, url: str, *, recover: bool = False):
        super().__init__()
        self.url = url
        self.forms: list[SavedForm] = []
        self.links: list[Link] = []
        self.resources: list[tuple[str, str]] = []
        self.images: list[tuple[str, str]] = []
        self.resource_spans: list[StaticSpan] = []
        self.image_spans: list[StaticSpan] = []
        self.form: SavedForm | None = None
        self.anchor: Link | None = None
        self.label_parts: list[str] | None = None
        self.label_for: str | None = None
        self.label_form_index: int | None = None
        self.label_start_field_count = 0
        self.heading_tag: str | None = None
        self.heading_parts: list[str] = []
        self.labels: dict[tuple[int, str], str] = {}
        self.field_ids: dict[tuple[int, int], str] = {}
        self.stop_position: tuple[int, int] | None = None
        self.end_position: tuple[int, int] | None = None
        self.decoding_loss = False
        self.closing_token: str | None = None
        self.recover = recover
        self.diagnostics: list[ParseDiagnostic] = []
        self.regions = [TextRegion((1, 0))]
        self.semantic_blocks: list[tuple[str, bool, int]] = []
        self.block_parts: list[str] = []
        self.block_navigation = False
        self.block_region = 0
        self.block_tags = {"a", "nav", "p", "div", "li", "td", "th", "h1", "h2", "h3", "h4"}

    def flush_block(self):
        text = compact(self.block_parts)
        if text:
            self.semantic_blocks.append((text, self.block_navigation, self.block_region))
        self.block_parts = []

    @staticmethod
    def position(value):
        return HTMLPosition(line=value[0], column=value[1])

    @staticmethod
    def token_end(start, token):
        lines = token.split("\n")
        return (
            start[0] + len(lines) - 1,
            start[1] + len(token) if len(lines) == 1 else len(lines[-1]),
        )

    def span(self, start, end, *, recovered=False):
        unknown = self.decoding_loss
        partial = recovered or bool(self.limitations)
        reasons = list(self.limitations)
        if unknown:
            reasons.append("UTF-8 decoding loss; static coverage unknown")
        if recovered:
            reasons.append("After local HTML recovery; locally parsed observations only")
        return StaticSpan(
            start=self.position(start),
            end_exclusive=self.position(end),
            status="unknown" if unknown else "partial" if partial else "complete",
            limitations=reasons,
        )

    def region_span(self, index):
        region = self.regions[index]
        return self.span(region.start, region.end, recovered=region.recovered)

    def recover_token(self, reason, token, action):
        self.flush_block()
        point = self.getpos()
        self.regions[-1].end = point
        self.limitations.append(reason)
        self.diagnostics.append(
            ParseDiagnostic(position=self.position(point), reason=reason, action=action)
        )
        self.regions.append(TextRegion(self.token_end(point, token), recovered=True))

    def inspection(self):
        return StaticInspection(
            status="unknown"
            if self.decoding_loss
            else "partial"
            if self.limitations
            else "complete",
            diagnostics=self.diagnostics,
            text_spans=[
                self.region_span(i)
                for i, r in enumerate(self.regions)
                if r.parts and r.end and r.end > r.start
            ],
            recovered_text_chars=sum(len(compact(r.parts)) for r in self.regions if r.recovered),
            limitations=[
                *self.limitations,
                *(["UTF-8 decoding loss"] if self.decoding_loss else []),
            ],
        )

    def parse_endtag(self, i):
        end = self.rawdata.find(">", i)
        self.closing_token = self.rawdata[i : end + 1] if end >= 0 else None
        try:
            return super().parse_endtag(i)
        finally:
            self.closing_token = None

    def uncertain(self, reason):
        if self.stop_position is None:
            self.stop_position = self.getpos()
            self.flush_block()
            self.regions[-1].end = self.getpos()
        if not any(d.reason == reason for d in self.diagnostics):
            self.diagnostics.append(
                ParseDiagnostic(
                    position=self.position(self.getpos()), reason=reason, action="suffix_withheld"
                )
            )
        super().uncertain(reason)

    def coverage(self, form: SavedForm) -> StaticFormCoverage:
        def position(value):
            return HTMLPosition(line=value[0], column=value[1]) if value else None

        page_status = (
            "unknown" if self.decoding_loss else ("partial" if self.limitations else "complete")
        )
        status = (
            "unknown"
            if self.decoding_loss
            else (
                "complete" if form.end_exclusive is not None and not form.recovered else "partial"
            )
        )
        reasons = list(self.limitations)
        if form.end_exclusive is None:
            reasons.append("Form closing boundary was not reliably parsed; inventories are partial")
        if self.decoding_loss:
            reasons.append("UTF-8 decoding loss; static coverage unknown")
        if page_status != "complete" and status == "complete":
            reasons.append("Only this closed form was parsed; the surrounding page is incomplete")
        if form.recovered:
            reasons.append("Form occurs after recovery; complete form inventory not established")
        end = form.end_exclusive or self.stop_position or self.end_position
        region = self.span(form.start, end, recovered=form.recovered) if end > form.start else None
        return StaticFormCoverage(
            status=status,
            page_status=page_status,
            form_start=position(form.start),
            form_end_exclusive=position(form.end_exclusive),
            trusted_page_end_exclusive=position(self.stop_position or self.end_position),
            reasons=reasons,
            recovered=form.recovered,
            observed_region=region,
        )

    def handle_starttag(self, tag, attrs):
        if tag in self.block_tags:
            self.flush_block()
        if self.recover and not self.quarantined:
            names = [name for name, _ in attrs]
            token = self.get_starttag_text()
            start_syntax = (
                r"<[a-zA-Z][a-zA-Z0-9:._-]*"
                r"(?:\s+[a-zA-Z_:][a-zA-Z0-9_:.-]*"
                r"(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?)*\s*/?>"
            )
            if len(names) != len(set(names)) or not re.fullmatch(start_syntax, token):
                if (
                    not self.form
                    and (not self.excluded or (self.excluded == ["head"] and tag == "link"))
                    and tag not in self.EXCLUDED
                    and tag not in {"form", "input", "select", "textarea", "button"}
                    and "<" not in token[1:]
                    and len(self.diagnostics) < 100
                ):
                    # Keep only the explicit lexical tag name. Never reuse ambiguous
                    # attributes for links, resources, labels, purposes or identities.
                    self.recover_token(
                        f"Ambiguous attributes of <{tag}> at {self.getpos()}; attributes ignored",
                        token,
                        "ignored_ambiguous_attributes",
                    )
                    attrs = []
                else:
                    self.uncertain("Ambiguous HTML attributes/start token; suffix withheld")
        previous_excluded = list(self.excluded)
        super().handle_starttag(tag, attrs)
        if self.quarantined:
            return
        values = dict(attrs)
        # A script in head is a saved reference, never evidence of execution.
        if tag in {"script", "iframe"} and not any(t != "head" for t in previous_excluded):
            source = values.get("src")
            if source and len(self.resources) < 200:
                self.resources.append((tag, static_reference(self.url, source)))
                self.resource_spans.append(
                    self.span(
                        self.getpos(),
                        self.token_end(self.getpos(), self.get_starttag_text()),
                        recovered=len(self.regions) > 1,
                    )
                )
            elif source:
                self.uncertain("Static resource reference limit; suffix withheld")
        if self.excluded or self.quarantined:
            if not self.quarantined:
                self.record_declarations(tag, values, previous_excluded)
            return
        if tag == "form":
            if self.form is not None or len(self.forms) >= 30:
                self.uncertain("Nested form or static form limit; suffix withheld")
                return
            self.form = SavedForm(
                len(self.forms) + 1,
                static_reference(self.url, values["action"]) if values.get("action") else None,
                values.get("aria-label"),
            )
            self.forms.append(self.form)
            self.form.start = self.getpos()
            self.form.recovered = len(self.regions) > 1
        elif tag in {"input", "select", "textarea"} and self.form is not None:
            if len(self.form.fields) >= 100:
                self.uncertain("Static field limit; suffix withheld")
                return
            kind = (values.get("type") or ("text" if tag == "input" else tag)).strip().casefold()
            # Values/default textarea contents are deliberately not captured.
            self.form.fields.append(
                InputField(
                    tag=tag,
                    type=kind,
                    name=values.get("name"),
                    label=values.get("aria-label"),
                    required="required" in values,
                    checked=("checked" in values) if kind in {"checkbox", "radio"} else None,
                )
            )
            self.field_ids[(self.form.index, len(self.form.fields) - 1)] = values.get("id") or ""
            self.form.field_spans.append(
                self.span(
                    self.getpos(),
                    self.token_end(self.getpos(), self.get_starttag_text()),
                    recovered=self.form.recovered,
                )
            )
        elif tag == "a" and values.get("href"):
            if len(self.links) >= 1000:
                self.uncertain("Static link limit; suffix withheld")
                return
            self.anchor = Link(
                static_reference(self.url, values["href"]), self.form.index if self.form else None
            )
            self.links.append(self.anchor)
            self.anchor.span = self.span(
                self.getpos(),
                self.token_end(self.getpos(), self.get_starttag_text()),
                recovered=len(self.regions) > 1,
            )
        elif tag == "img" and values.get("src"):
            if len(self.images) < 200:
                self.images.append(
                    (static_reference(self.url, values["src"]), values.get("alt") or "")
                )
                self.image_spans.append(
                    self.span(
                        self.getpos(),
                        self.token_end(self.getpos(), self.get_starttag_text()),
                        recovered=len(self.regions) > 1,
                    )
                )
            else:
                self.uncertain("Static image reference limit; suffix withheld")
        if tag == "label":
            self.label_parts = []
            self.label_for = values.get("for")
            self.label_form_index = self.form.index if self.form else None
            self.label_start_field_count = len(self.form.fields) if self.form else 0
        if self.form is not None and tag in {"button", "legend", "h1", "h2", "h3"}:
            self.form.elements.append(tag)
            if tag != "button":
                self.heading_tag = tag
                self.heading_parts = []
        if not self.quarantined:
            self.record_declarations(tag, values, previous_excluded)

    def record_declarations(self, tag, values, previous_excluded):
        """Extension point after attribute/structure checks; excluded text stays excluded."""

    def handle_startendtag(self, tag, attrs):
        if tag in self.VOID:
            self.handle_starttag(tag, attrs)
        else:
            super().handle_startendtag(tag, attrs)

    def handle_data(self, data):
        before = len(self.parts)
        super().handle_data(data)
        if len(self.parts) == before:
            return
        self.regions[-1].parts.append(data)
        navigation = any(t in {"a", "nav"} for t in self.open_elements)
        if self.block_parts and navigation != self.block_navigation:
            self.flush_block()
        self.block_navigation = navigation
        self.block_region = len(self.regions) - 1
        self.block_parts.append(data)
        if any(t in {"textarea", "option"} for t in self.open_elements):
            # A wrapped label must not absorb a pre-filled control's contents either.
            return
        if self.anchor is not None:
            self.anchor.parts.append(data)
        if self.label_parts is not None:
            self.label_parts.append(data)
        if self.heading_tag is not None:
            self.heading_parts.append(data)
        if self.form is not None:
            self.form.parts.append(data)

    def handle_endtag(self, tag):
        was_excluded = bool(self.excluded)
        # Recovery is deliberately narrow. A syntactically valid close of an ordinary
        # element that was never opened changes no stack/attributes. Inside a form,
        # excluded region, malformed token, ancestor mismatch or resource-limit failure
        # we retain the original fail-closed behaviour. No browser repair is simulated.
        if (
            self.recover
            and not self.quarantined
            and not self.excluded
            and self.form is None
            and tag not in self.open_elements
            and tag not in self.EXCLUDED
            and tag not in {"form", "html", "body"}
            and self.closing_token is not None
            and re.fullmatch(r"</\s*[a-zA-Z][a-zA-Z0-9:._-]*\s*>", self.closing_token)
            and len(self.diagnostics) < 100
        ):
            point = self.getpos()
            reason = f"Mismatched HTML closing element </{tag}> at {point}; stray close ignored"
            self.recover_token(reason, self.closing_token, "ignored_stray_close")
            return
        if tag in self.block_tags:
            self.flush_block()
        super().handle_endtag(tag)
        if self.quarantined or was_excluded:
            return
        if tag == self.heading_tag:
            if self.form and not self.form.purpose:
                self.form.purpose = compact(self.heading_parts) or None
            self.heading_tag = None
            self.heading_parts = []
        if tag == "form":
            if self.form is not None:
                # End-tag callbacks precede HTMLParser's position update. Preserve the exact
                # token end, including whitespace/newlines, rather than assuming </form>.
                line, column = self.getpos()
                token = self.closing_token
                if token:
                    lines = token.split("\n")
                    self.form.end_exclusive = (
                        line + len(lines) - 1,
                        column + len(token) if len(lines) == 1 else len(lines[-1]),
                    )
            self.form = None
        elif tag == "a":
            self.anchor = None
        elif tag == "label":
            if (
                self.label_for
                and self.label_parts is not None
                and self.label_form_index is not None
            ):
                self.labels[(self.label_form_index, self.label_for)] = compact(self.label_parts)
            elif self.form and self.label_parts is not None:
                for input_field in self.form.fields[self.label_start_field_count :]:
                    if not input_field.label:
                        input_field.label = compact(self.label_parts)
            self.label_parts = None
            self.label_for = None

    def close(self):
        super().close()
        self.end_position = self.getpos()
        self.flush_block()
        if self.regions[-1].end is None:
            self.regions[-1].end = self.end_position
        # Field/link ranges acquired before a later page error retain their exact
        # coordinates but must also expose the final page's incompleteness.
        for form in self.forms:
            form.field_spans = [
                self.span(
                    (s.start.line, s.start.column),
                    (s.end_exclusive.line, s.end_exclusive.column),
                    recovered=form.recovered,
                )
                for s in form.field_spans
            ]
        for link in self.links:
            if link.span:
                link.span = self.span(
                    (link.span.start.line, link.span.start.column),
                    (link.span.end_exclusive.line, link.span.end_exclusive.column),
                    recovered=link.span.status != "complete",
                )
        for name in ("resource_spans", "image_spans"):
            setattr(
                self,
                name,
                [
                    self.span(
                        (s.start.line, s.start.column),
                        (s.end_exclusive.line, s.end_exclusive.column),
                        recovered=s.status != "complete",
                    )
                    for s in getattr(self, name)
                ],
            )
        for form in self.forms:
            for index, input_field in enumerate(form.fields):
                element_id = self.field_ids.get((form.index, index))
                key = (form.index, element_id)
                if key in self.labels and not input_field.label:
                    input_field.label = self.labels[key]
