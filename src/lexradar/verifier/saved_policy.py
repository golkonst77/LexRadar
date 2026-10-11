"""Bounded document observations from original HTML, not imported finding quotations."""

import hashlib
import re
from dataclasses import dataclass, field

from ..primary.markup import SavedMarkup, compact
from .requirement_models import PolicyBlock, SavedPolicyObservation


@dataclass
class Block:
    start: tuple[int, int]
    depth: int
    identifier: str
    diagnostic_start: int
    parts: list[str] = field(default_factory=list)
    chars: int = 0
    truncated: bool = False
    end: tuple[int, int] | None = None
    complete: bool = False


class PolicyMarkup(SavedMarkup):
    """Reuse the conservative parser; local block closure does not repair the page."""

    def __init__(self, url):
        super().__init__(url, recover=True)
        self.active_blocks = []
        self.policy_blocks = []
        self.last_text_end = (1, 0)

    def handle_starttag(self, tag, attrs):
        diagnostics = len(self.diagnostics)
        super().handle_starttag(tag, attrs)
        if self.quarantined or self.excluded or tag not in {"div", "section", "article", "main"}:
            return
        if len(self.policy_blocks) + len(self.active_blocks) >= 1000:
            self.uncertain("Policy block inventory limit; suffix withheld")
            return
        # Attributes ignored by recovery cannot nominate a document.
        identifier = dict(attrs).get("id", "") if diagnostics == len(self.diagnostics) else ""
        self.active_blocks.append(
            Block(self.getpos(), len(self.open_elements), identifier, len(self.diagnostics))
        )

    def uncertain(self, reason):
        if not self.quarantined:
            for block in self.active_blocks:
                block.end = self.getpos()
        super().uncertain(reason)

    def handle_data(self, data):
        before = len(self.parts)
        super().handle_data(data)
        if len(self.parts) == before:
            return
        self.last_text_end = self.token_end(self.getpos(), data)
        for block in self.active_blocks:
            remaining = 50000 - block.chars
            if len(data) > remaining:
                block.truncated = True
            if remaining:
                block.parts.append(data[:remaining])
                block.chars += min(len(data), remaining)

    def handle_endtag(self, tag):
        depth = len(self.open_elements)
        closes = bool(self.open_elements) and self.open_elements[-1] == tag
        super().handle_endtag(tag)
        if self.quarantined or not closes:
            return
        for block in list(self.active_blocks):
            if block.depth == depth:
                block.end = self.token_end(self.getpos(), self.closing_token or f"</{tag}>")
                block.complete = block.diagnostic_start == len(self.diagnostics)
                self.policy_blocks.append(block)
                self.active_blocks.remove(block)

    def close(self):
        super().close()
        for block in self.active_blocks:
            block.end = block.end or self.getpos()
            self.policy_blocks.append(block)
        self.active_blocks = []


def policy_observations(research):
    observations = []
    if research is None:
        return observations
    for eid, (artifact, raw) in sorted(research.originals.items()):
        if artifact.kind != "html":
            continue
        source = str(research.evidence[eid].source)
        parser = PolicyMarkup(source)
        decoded = raw.decode("utf-8", errors="replace")
        decoding_loss = decoded.encode("utf-8") != raw
        parser.feed(decoded)
        parser.close()
        seen = set()
        selected_ranges = []
        # Prefer an explicit popup/document identifier over enclosing title/trigger wrappers.
        ordered = sorted(
            parser.policy_blocks,
            key=lambda b: (
                not bool(re.search(r"policy|privacy|политик", b.identifier, re.I)),
                b.start,
            ),
        )
        for block in ordered:
            text = compact(block.parts)
            if len(text) > 50000:
                block.truncated = True
                text = text[:50000]
            title = re.match(r"^(?:политика\b|privacy policy\b)", text, re.IGNORECASE)
            identified = re.search(r"policy|privacy|политик", block.identifier, re.IGNORECASE)
            personal = re.search(r"персональн|personal data", text, re.IGNORECASE)
            if (
                not (title or identified)
                or not personal
                or len(text) < 80
                or text in seen
                or block.end is None
                or block.end <= block.start
                or any(
                    (start <= block.start and block.end <= end)
                    or (block.start <= start and end <= block.end)
                    for start, end in selected_ranges
                )
            ):
                continue
            seen.add(text)
            selected_ranges.append((block.start, block.end))
            complete = block.complete and not block.truncated and not decoding_loss
            limitations = [
                "Saved lexical document candidate, not proof of legal sufficiency/current version",
                "CSS, popup behaviour, JavaScript and live accessibility not examined",
            ]
            if not complete:
                limitations.append("Unclosed, damaged, decoding-loss or truncated policy block")
            observations.append(
                SavedPolicyObservation(
                    id="saved-policy-"
                    + hashlib.sha256(
                        f"{eid}:{artifact.sha256}:{block.start}:{block.end}".encode()
                    ).hexdigest(),
                    evidence_id=eid,
                    source_url=source,
                    artifact_path=artifact.path,
                    artifact_sha256=artifact.sha256,
                    block=PolicyBlock(
                        start={"line": block.start[0], "column": block.start[1]},
                        end_exclusive={"line": block.end[0], "column": block.end[1]},
                        status="complete" if complete else "partial",
                        limitations=[] if complete else limitations,
                    ),
                    text=text,
                    text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    reading_completeness="complete" if complete else "partial",
                    page_completeness="partial"
                    if parser.limitations or decoding_loss
                    else "complete",
                    limitations=limitations,
                )
            )
    return observations
