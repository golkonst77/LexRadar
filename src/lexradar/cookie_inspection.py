"""Static cookie/analytics inventory from saved originals; never live JS evidence."""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from .auditors.evidence import load_packet
from .batch import safe_path

SERVICES = {
    "yandex_metrika": re.compile(r"mc\.yandex|yandex[-_.]?metri[kc]|ym\s*\(", re.I),
    "google_analytics": re.compile(r"google-analytics|googletagmanager.com/gtag|gtag\s*\(", re.I),
    "google_tag_manager": re.compile(r"googletagmanager.com/gtm|GTM-[A-Z0-9]+", re.I),
    "other_analytics": re.compile(r"matomo|clarity\.ms|hotjar|top\.mail\.ru", re.I),
}


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.controls = []
        self.control = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "iframe", "img"} and attrs.get("src"):
            self.tags.append((tag, attrs["src"]))
        if tag in {"button", "a"}:
            self.control = dict(
                tag=tag,
                text="",
                attributes={k: attrs[k] for k in ("id", "class", "href", "type") if k in attrs},
            )
        if tag == "input" and attrs.get("type") in {"button", "submit", "checkbox"}:
            self.controls.append(dict(tag=tag, text=attrs.get("value", ""), attributes=attrs))

    def handle_data(self, text):
        if self.control:
            self.control["text"] += text

    def handle_endtag(self, tag):
        if self.control and tag == self.control["tag"]:
            self.controls.append(self.control)
            self.control = None


def inspect_saved(root):
    root = safe_path(root)
    packet = load_packet(root, 20_000_000)
    rows = []
    for page in packet.data.pages:
        evidence = next(e for e in packet.data.evidence if e.id == page.evidence_id)
        artifact = next((a for a in evidence.artifacts if a.kind == "html"), None)
        if not artifact:
            continue
        raw = (root / artifact.path).read_text(encoding="utf-8")
        parser = Markup()
        parser.feed(raw)
        notices = []
        text = page.text or ""
        for match in re.finditer(r"cookie|cookies|куки", text, re.I):
            start, end = max(0, match.start() - 100), min(len(text), match.end() + 350)
            notices.append(dict(start=start, end=end, exact_quote=text[start:end]))
        rows.append(
            dict(
                evidence_id=page.evidence_id,
                source=str(evidence.source),
                html_sha256=artifact.sha256,
                static_service_hints=[
                    name for name, pattern in SERVICES.items() if pattern.search(raw)
                ],
                resource_references=[
                    dict(tag=tag, url=urljoin(str(evidence.source), url))
                    for tag, url in parser.tags
                    if any(p.search(url) for p in SERVICES.values())
                ],
                notice_excerpts=notices,
                possible_controls=[
                    c
                    for c in parser.controls
                    if re.search(
                        r"cookie|куки|соглас|отказ|настро|accept|reject|consent", str(c), re.I
                    )
                ],
            )
        )
    return dict(
        mode="saved_static_html",
        pages=rows,
        dynamic_status="NOT_VERIFIED",
        dynamic_checks={
            k: "NOT_VERIFIED"
            for k in (
                "scripts_before_interaction",
                "cookies_before_interaction",
                "accept_behavior",
                "reject_behavior",
                "no_consent_behavior",
                "settings_behavior",
            )
        },
        limitation="Static declarations and controls are not proof of execution, visibility or "
        "consent. Live JS requires a separate network-isolated browser worker enforcing robots, "
        "DNS pinning and limits for every subrequest; current collector aborts subrequests.",
    )
