"""Inert acquisition mechanisms: every occurrence is tied to checked original bytes."""

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from .inline import form_routes, resources, route_literals, tokens
from .markup import SavedMarkup

ATTRIBUTES = re.compile(
    r'([A-Za-z_:][A-Za-z0-9_:.-]*)(?:\s*=\s*(?:"([^"]*)"|\x27([^\x27]*)\x27|([^\s"\x27=<>`]+)))?'
)
DATA_ROUTES = {"data-href-ajax", "data-url", "data-action", "data-endpoint", "data-submit-url"}
POPUP_ATTRS = {"href", "data-src", "data-target", "data-bs-target", "aria-controls"}


def normalize_reference(base, value):
    if not value or re.search(r"[\s\x00-\x1f\\]", value):
        return None
    try:
        parsed = urlsplit(urljoin(base, value))
        if parsed.scheme.lower() not in {"https", "http"} or not parsed.hostname or parsed.username:
            return None
        host = parsed.hostname.lower()
        host = "[" + host + "]" if ":" in host else host
        port = parsed.port
        if port is not None and (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host += ":" + str(port)
        # Query, fragment, path case and percent escapes are deliberately retained.
        return urlunsplit(
            (parsed.scheme.lower(), host, parsed.path or "/", parsed.query, parsed.fragment)
        )
    except ValueError:
        return None


def provider(value):
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if host == "booking.medflex.ru":
        return "medflex"
    if host == "www.googletagmanager.com":
        return (
            "google_tag_manager"
            if parsed.path in {"/gtm.js", "/ns.html"}
            else ("google_analytics" if parsed.path == "/gtag/js" else None)
        )
    if host in {"www.google-analytics.com", "google-analytics.com"}:
        return "google_analytics"
    if host == "mc.yandex.ru":
        return "yandex_metrika"
    if host == "api-maps.yandex.ru":
        return "yandex_maps"
    if host == "cdn.callibri.ru":
        return "callibri"
    if host.endswith(".tildacdn.com") and "stat" in parsed.path.lower():
        return "tilda_stats"
    if host in {"prodoctorov.ru", "www.prodoctorov.ru"}:
        return "prodoctorov"
    if re.search(r"(?:jquery[.-]form|jquery[.-]validate|parsley)(?:[.-]|$)", parsed.path):
        return "form_library_candidate"
    return None


@dataclass
class RawDeclaration:
    kind: str
    raw: str
    value: str
    start: tuple[int, int]
    end: tuple[int, int]
    form: int | None = None
    target: str | None = None
    normalized: str | None = None
    normalization: str = "none"
    service: str | None = None
    limitations: list[str] = field(default_factory=list)
    selector: str | None = None


@dataclass
class Target:
    value: str
    declaration: RawDeclaration
    depth: int
    parts: list[str] = field(default_factory=list)
    forms: list[int] = field(default_factory=list)
    closed: bool = False
    end: tuple[int, int] | None = None


class MechanismMarkup(SavedMarkup):
    MAX_DECLARATIONS = 1000
    MAX_SCRIPT_CHARS = 32768

    def __init__(self, url, *, recover=True):
        super().__init__(url, recover=recover)
        self.declarations: list[RawDeclaration] = []
        self.targets: list[Target] = []
        self.active_targets: list[Target] = []
        self.form_ids: dict[str, list[int]] = {}
        self.script_parts: list[str] | None = None
        self.script_start = None
        self.script_chars = 0
        self.script_oversized = False
        self.mechanism_limits: list[str] = []

    def add(self, kind, raw, value, start, end, **kwargs):
        if len(self.declarations) >= self.MAX_DECLARATIONS:
            self.uncertain("Static declaration limit; suffix withheld")
            return None
        if len(raw) > 4096 or len(value) > 4096:
            self.uncertain("Static declaration value limit; suffix withheld")
            return None
        row = RawDeclaration(kind, raw, value, start, end, **kwargs)
        self.declarations.append(row)
        return row

    def attributes(self):
        token = self.get_starttag_text()
        name_end = re.match(r"<[A-Za-z][A-Za-z0-9:._-]*", token).end()
        found = {}
        for m in ATTRIBUTES.finditer(token, name_end):
            value = next((v for v in m.groups()[1:] if v is not None), "")
            found[m.group(1).lower()] = (
                value,
                self.token_end(self.getpos(), token[: m.start()]),
                self.token_end(self.getpos(), token[: m.end()]),
            )
        return found

    def record_declarations(self, tag, values, previous_excluded):
        if not values or any(t not in {"head", "noscript"} for t in previous_excluded):
            return
        attrs = self.attributes()
        form = self.form.index if self.form else None

        def attribute(kind, name, **extra):
            if name not in values or name not in attrs:
                return None
            raw, start, end = attrs[name]
            return self.add(kind, raw, values[name] or "", start, end, **extra)

        if tag == "a" and values.get("href") and not previous_excluded:
            attribute("anchor", "href", normalized=normalize_reference(self.url, values["href"]))
        if "data-fancybox" in values:
            attribute("popup_marker", "data-fancybox")
        if tag == "form":
            if values.get("id"):
                self.form_ids.setdefault(values["id"], []).append(form)
            attribute("form_method", "method", form=form)
            attribute("html_action", "action", form=form)
            for name in sorted(DATA_ROUTES):
                attribute(
                    "data_route",
                    name,
                    form=form,
                    limitations=[f"Declared {name}; actual route/recipient unknown"],
                )
        if form is not None and not previous_excluded:
            for name in sorted(values):
                if name.startswith("on"):
                    row = attribute("inline_handler", name, form=form)
                    if row and tag == "form" and name == "onsubmit":
                        stream, limitation = tokens(values[name] or "")
                        for token in route_literals(stream):
                            self.add(
                                "js_route",
                                row.raw,
                                token.value,
                                row.start,
                                row.end,
                                form=form,
                                limitations=[
                                    "Literal inside form onsubmit, not proof of submission",
                                    *([limitation] if limitation else []),
                                ],
                            )
                elif name in {"data-handler", "data-submit-handler"}:
                    attribute("handler_reference", name, form=form)
        for name in sorted(POPUP_ATTRS):
            value = values.get(name) or ""
            fragment = (
                value if value.startswith("#") else "#" + value if name == "aria-controls" else ""
            )
            if re.fullmatch(r"#[A-Za-z_][A-Za-z0-9_:.-]*", fragment):
                attribute("popup_trigger", name, form=form, target=fragment[1:])
        if values.get("id") and not previous_excluded:
            if len(self.targets) >= 300:
                self.uncertain("Static popup target limit; suffix withheld")
                return
            raw, start, end = attrs["id"]
            row = RawDeclaration("popup_target", raw, values["id"], start, end, target=values["id"])
            target = Target(values["id"], row, len(self.open_elements))
            self.targets.append(target)
            if tag not in self.VOID:
                self.active_targets.append(target)
            else:
                target.closed = True
        if tag == "form":
            for target in self.active_targets:
                target.forms.append(form)
        if tag in {"script", "iframe"} and values.get("src"):
            kind = (
                "noscript_iframe"
                if tag == "iframe" and "noscript" in previous_excluded
                else tag + "_src"
            )
            attribute(kind, "src")
        if tag in {"script", "iframe", "img", "source", "div", "a"} and values.get("data-src"):
            if not values["data-src"].startswith("#"):
                attribute("lazy_resource", "data-src")
        if tag == "script" and not values.get("src") and not previous_excluded[-1:] == ["noscript"]:
            # Only JavaScript-ish inline blocks; JSON/HTML templates are opaque.
            mime = (values.get("type") or "").lower()
            if mime in {"", "text/javascript", "application/javascript", "module"}:
                self.script_parts = []
                self.script_chars = 0
                self.script_oversized = False
                self.script_start = self.token_end(self.getpos(), self.get_starttag_text())

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        # Empty-attribute script tags still need a bounded raw-text buffer.
        if (
            tag == "script"
            and not attrs
            and not self.quarantined
            and all(t in {"head", "script"} for t in self.excluded)
        ):
            self.script_parts = []
            self.script_chars = 0
            self.script_oversized = False
            self.script_start = self.token_end(self.getpos(), self.get_starttag_text())

    def handle_data(self, data):
        before = len(self.parts)
        super().handle_data(data)
        if self.quarantined:
            return
        if self.script_parts is not None and self.excluded[-1:] == ["script"]:
            if self.script_chars + len(data) <= self.MAX_SCRIPT_CHARS:
                self.script_parts.append(data)
                self.script_chars += len(data)
            else:
                self.script_oversized = True
        if len(self.parts) > before:
            for target in self.active_targets:
                if sum(map(len, target.parts)) + len(data) <= 8192:
                    target.parts.append(data)
                elif (
                    "Popup text limit; candidate classification incomplete"
                    not in target.declaration.limitations
                ):
                    target.declaration.limitations.append(
                        "Popup text limit; candidate classification incomplete"
                    )

    def handle_endtag(self, tag):
        was_script = (
            self.script_parts is not None and self.excluded[-1:] == ["script"] and tag == "script"
        )
        super().handle_endtag(tag)
        if self.quarantined:
            self.script_parts = None
            return
        if was_script:
            body = "".join(self.script_parts)
            if self.script_oversized:
                self.mechanism_limits.append("Inline script size limit; entire block not inspected")
            else:
                found, limitation = resources(body)
                routes, route_limit = form_routes(body)
                for start, end, value, dynamic in found:
                    self.add(
                        "inline_resource",
                        body[start:end],
                        value,
                        self.token_end(self.script_start, body[:start]),
                        self.token_end(self.script_start, body[:end]),
                        limitations=[
                            "JS declaration only; no execution/cookies checked",
                            *(["Dynamic expression; full URL unknown"] if dynamic else []),
                            *([limitation] if limitation else []),
                        ],
                        normalization="none" if dynamic else "reference",
                    )
                for selector, start, end, value in routes:
                    self.add(
                        "js_route",
                        body[start:end],
                        value,
                        self.token_end(self.script_start, body[:start]),
                        self.token_end(self.script_start, body[:end]),
                        selector=selector,
                        limitations=[
                            "Exact submit callback, execution/recipient unknown",
                            *([route_limit] if route_limit else []),
                        ],
                    )
                if limitation or route_limit:
                    self.mechanism_limits.append(limitation or route_limit)
            self.script_parts = None
        for target in list(self.active_targets):
            if target.depth > len(self.open_elements):
                target.closed = True
                target.end = self.token_end(self.getpos(), self.closing_token or "")
                self.active_targets.remove(target)

    def close(self):
        super().close()
        # Targets are materialized only when referenced; no every-id hypothesis expansion.
        wanted = {d.target for d in self.declarations if d.kind == "popup_trigger"}
        for target in self.targets:
            if target.value in wanted:
                if not target.closed:
                    target.declaration.limitations.append("Target closing boundary not inspected")
                self.add(
                    "popup_target",
                    target.declaration.raw,
                    target.value,
                    target.declaration.start,
                    target.declaration.end,
                    target=target.value,
                    limitations=target.declaration.limitations,
                )
        for link in self.links:
            normalized = normalize_reference(self.url, link.url)
            if not normalized or not link.span:
                continue
            external = urlsplit(normalized).netloc != urlsplit(self.url).netloc
            if external and (
                provider(normalized) == "medflex"
                or re.search(
                    r"booking|appointment|reservation|запис",
                    link.url + " " + link.text,
                    re.IGNORECASE,
                )
            ):
                # Raw href occurrence was captured separately with its original encoded spelling.
                for d in self.declarations:
                    if d.kind == "anchor" and d.normalized == normalized:
                        d.kind = "booking_anchor"
        for d in self.declarations:
            if d.kind in {
                "html_action",
                "data_route",
                "js_route",
                "script_src",
                "iframe_src",
                "noscript_iframe",
                "lazy_resource",
                "booking_anchor",
                "anchor",
            }:
                d.normalized = normalize_reference(self.url, d.value)
                d.normalization = "reference" if d.normalized else "none"
            elif d.kind in {"popup_trigger", "popup_target"}:
                d.normalized = "#" + d.target
                d.normalization = "fragment"
            elif d.kind == "form_method":
                d.normalized = (
                    d.value.upper() if d.value.lower() in {"get", "post", "dialog"} else None
                )
                d.normalization = "method" if d.normalized else "none"
            elif d.kind == "inline_resource" and d.normalization == "reference":
                d.normalized = normalize_reference(self.url, d.value)
            d.service = provider(d.normalized or d.value)
            if d.kind == "js_route" and d.selector:
                matches = self.form_ids.get(d.selector, [])
                if len(matches) == 1:
                    d.form = matches[0]
        if any(d.kind == "js_route" and d.form is None for d in self.declarations):
            self.mechanism_limits.append(
                "JS route selector not uniquely bound to an inspected form"
            )
        self.declarations = [
            d
            for d in self.declarations
            if d.kind != "anchor" and not (d.kind == "js_route" and d.form is None)
        ]
