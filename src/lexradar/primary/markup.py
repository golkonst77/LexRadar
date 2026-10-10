"""Inspect saved static attributes only; never execute scripts, resolve DNS or submit forms."""

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from ..collector.models import InputField
from ..collector.reading import StaticText


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


class SavedMarkup(StaticText):
    """Bounded, conservative extraction; inherited ambiguity handling withholds suffixes."""

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self.forms: list[SavedForm] = []
        self.links: list[Link] = []
        self.resources: list[tuple[str, str]] = []
        self.images: list[tuple[str, str]] = []
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

    def handle_starttag(self, tag, attrs):
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
            elif source:
                self.uncertain("Static resource reference limit; suffix withheld")
        if self.excluded or self.quarantined:
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
        elif tag == "a" and values.get("href"):
            if len(self.links) >= 1000:
                self.uncertain("Static link limit; suffix withheld")
                return
            self.anchor = Link(
                static_reference(self.url, values["href"]), self.form.index if self.form else None
            )
            self.links.append(self.anchor)
        elif tag == "img" and values.get("src"):
            if len(self.images) < 200:
                self.images.append(
                    (static_reference(self.url, values["src"]), values.get("alt") or "")
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
        super().handle_endtag(tag)
        if self.quarantined:
            return
        if tag == self.heading_tag:
            if self.form and not self.form.purpose:
                self.form.purpose = compact(self.heading_parts) or None
            self.heading_tag = None
            self.heading_parts = []
        if tag == "form":
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
        for form in self.forms:
            for index, input_field in enumerate(form.fields):
                element_id = self.field_ids.get((form.index, index))
                key = (form.index, element_id)
                if key in self.labels and not input_field.label:
                    input_field.label = self.labels[key]
