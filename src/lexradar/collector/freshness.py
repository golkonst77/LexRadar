"""Passive publisher claims; neither age nor headers establish a legal violation."""

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from .models import FreshnessSignal, SiteFreshness

_COPYRIGHT = re.compile(
    r"(?:©|\bcopyright\b|\(c\))\s*((?:19|20)\d{2})"
    r"(?:\s*[-–—]\s*((?:19|20)\d{2}))?(?!\d)", re.I,
)
_CREDIT = re.compile(r"создан|разработ|дизайн|develop|design|powered|site created", re.I)
_DATE = r"(?:\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}-\d{2}-\d{2})"
_POLICY_DATE = re.compile(
    r"(?:редакци\w*|утвержд\w*|revision|revised|approved|last\s+updated)"
    r"[^\d\n]{0,60}(" + _DATE + r")(?!\d)", re.I,
)


class _FooterParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []
        self.in_footer = False

    def handle_starttag(self, tag, attrs):
        attrs = {key: value or "" for key, value in attrs}
        footer = (
            tag == "footer" or attrs.get("role") == "contentinfo"
            or "footer" in (attrs.get("id", "") + " " + attrs.get("class", "")).casefold()
        )
        if tag in {"div", "p", "li", "br", "footer"}:
            self.parts.append("\n")
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, footer))
            self.in_footer = self.in_footer or footer

    def handle_endtag(self, tag):
        if tag in {"div", "p", "li", "footer"}:
            self.parts.append("\n")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                if self.in_footer:
                    self.in_footer = any(footer for _, footer in self.stack)
                break

    def handle_data(self, data):
        if self.in_footer and not any(
            tag in {"script", "style", "template", "noscript", "article", "address"} for tag, _ in self.stack
        ):
            self.parts.append(re.sub(r"\s+", " ", data))


def http_signals(last_modified, etag, source):
    signals = []
    if last_modified:
        try:
            date = parsedate_to_datetime(last_modified)
            if date.tzinfo is not None:
                signals.append(FreshnessSignal(
                    kind="http_last_modified", value=date.astimezone(UTC).isoformat(),
                    source=source, confidence="server_declared",
                ))
        except (TypeError, ValueError, OverflowError):
            pass
    if etag:
        signals.append(FreshnessSignal(
            kind="http_etag", value=etag, source=source, confidence="opaque_validator",
        ))
    return signals


def site_freshness(body: bytes, last_modified=None, etag=None, source=None):
    parser = _FooterParser()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
        parser.close()
        malformed = False
    except (AssertionError, ValueError):
        malformed = True
    years = []
    ambiguous = malformed or any(footer for _, footer in parser.stack)
    current = datetime.now(UTC).year
    for line in re.split(r"[\n.!;]", "".join(parser.parts)):
        matches = list(_COPYRIGHT.finditer(line))
        if matches and _CREDIT.search(line):
            ambiguous = True
        for match in matches:
            start = int(match.group(1))
            end = int(match.group(2) or start)
            if not start <= end <= current or re.match(r"\s*[-–—]", line[match.end():]):
                ambiguous = True
            years.append(end)
    year = years[0] if len(set(years)) == 1 and not ambiguous else None
    signals = http_signals(last_modified, etag, source)
    if year is not None:
        signals.insert(0, FreshnessSignal(
            kind="footer_copyright", value=str(year), source=source, confidence="explicit_text",
        ))
    result = SiteFreshness(
        footer_year=year,
        footer_year_stale=year < current if year is not None else None,
        http_last_modified=last_modified,
        http_etag=etag,
        signal_source=source if year is not None or last_modified or etag else None,
        policy_priority="high" if year is not None and year < current else None,
        signals=signals or None,
    )
    prioritize_policy(result)
    return result


def prioritize_policy(freshness):
    """Old technical calendar years guide review only, never legal conclusions."""
    current = datetime.now(UTC).year
    for signal in freshness.signals or []:
        if signal.kind not in {"http_last_modified", "pdf_creation_date", "pdf_modification_date"}:
            continue
        try:
            date = datetime.fromisoformat(signal.value)
        except ValueError:
            continue
        if date.year < current:
            freshness.policy_priority = "high"
            return


def policy_signals(text, source, evidence_id, metadata=None):
    signals = []
    # shortcut: explicit numeric revision/approval dates only; add locale dates with fixtures.
    for match in _POLICY_DATE.finditer(text or ""):
        raw = match.group(1)
        formats = ("%Y-%m-%d",) if re.match(r"\d{4}-", raw) else (
            "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y",
        )
        for fmt in formats:
            try:
                value = datetime.strptime(raw, fmt).date().isoformat()
                break
            except ValueError:
                value = None
        if value and not any(signal.value == value for signal in signals):
            signals.append(FreshnessSignal(
                kind="policy_declared_date", value=value, source=source,
                confidence="explicit_text", evidence_id=evidence_id,
            ))
            if len(signals) >= 16:
                break  # Bound redundant publisher date claims, not policy text analysis.
    for kind, value in (metadata or {}).items():
        signals.append(FreshnessSignal(
            kind=kind, value=value, source=source, confidence="document_declared",
            evidence_id=evidence_id,
        ))
    return signals
