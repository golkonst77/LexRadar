"""Bounded robots rules for the fixed LexRadar product token; fail closed on ambiguity."""

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlsplit

PRODUCT_TOKEN = "LexRadar"
USER_AGENT = "LexRadar/0.2 (bounded evidence collector)"
MAX_ROBOTS_BYTES = 500_000
UNRESERVED = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")


def octets(value):
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("Malformed robots percent escape")
    value = quote(value, safe="/%:?&=@!$'()+,;*-._~")
    return re.sub(
        r"%([0-9a-fA-F]{2})",
        lambda m: chr(int(m[1], 16)) if chr(int(m[1], 16)) in UNRESERVED else "%" + m[1].upper(),
        value,
    )


def matches(pattern, path):
    """Literal octets plus * and terminal $; no regex backtracking on untrusted rules."""
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    parts = pattern.split("*")
    if not path.startswith(parts[0]):
        return False
    position = len(parts[0])
    if len(parts) == 1:
        return not anchored or position == len(path)
    for part in parts[1:-1]:
        position = path.find(part, position)
        if position < 0:
            return False
        position += len(part)
    last = parts[-1]
    if anchored:
        return path.endswith(last) and len(path) - len(last) >= position
    return path.find(last, position) >= 0


@dataclass(frozen=True)
class RobotsRules:
    rules: tuple[tuple[bool, str], ...]
    crawl_delay: float
    selected_agent: str

    def allows(self, url):
        p = urlsplit(url)
        path = octets((p.path or "/") + ("?" + p.query if p.query else ""))
        candidates = [
            (len(pattern.replace("*", "").removesuffix("$")), allow)
            for allow, pattern in self.rules
            if matches(pattern, path)
        ]
        return max(candidates)[1] if candidates else True


def parse_robots(body: bytes) -> RobotsRules:
    if len(body) > MAX_ROBOTS_BYTES:
        raise ValueError("Robots size limit")
    text = body.decode("utf-8-sig", errors="strict")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
        raise ValueError("Invalid robots control character")
    groups, agents, rules, delays, has_directive = [], [], [], [], False
    meaningful = False

    def finish():
        nonlocal agents, rules, delays, has_directive
        if agents:
            groups.append((agents, rules, delays))
        agents, rules, delays, has_directive = [], [], [], False

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            finish()
            continue
        meaningful = True
        if len(line) > 4000 or ":" not in line:
            raise ValueError("Ambiguous robots syntax")
        name, value = (v.strip() for v in line.split(":", 1))
        name = name.casefold()
        if name == "user-agent":
            if not value or not re.fullmatch(r"[a-zA-Z0-9_.* /()-]+", value):
                raise ValueError("Invalid robots agent")
            if has_directive:
                finish()
            agents.append(value.casefold())
        elif name in {"allow", "disallow", "crawl-delay"}:
            if not agents:
                raise ValueError("Robots directive without user-agent")
            has_directive = True
            if name == "crawl-delay":
                try:
                    delay = Decimal(value)
                    if not delay.is_finite() or delay < 0 or delay > 86400:
                        raise ValueError("Invalid Crawl-delay")
                    delays.append(float(delay))
                except InvalidOperation as exc:
                    raise ValueError("Invalid Crawl-delay") from exc
            elif value:
                if not value.startswith("/"):
                    raise ValueError("Invalid robots path")
                rules.append((name == "allow", octets(value)))
                if len(rules) > 10000:
                    raise ValueError("Robots rule limit")
        # Extension directives (e.g. Sitemap) never cause another network request.
    finish()
    if meaningful and not groups:
        raise ValueError("No recognizable robots groups")
    scores = [
        (
            max(
                (len(a) for a in g[0] if a != "*" and USER_AGENT.casefold().startswith(a)),
                default=0,
            ),
            g,
        )
        for g in groups
    ]
    best = max((score for score, _ in scores), default=0)
    selected = (
        [g for score, g in scores if score == best] if best else [g for g in groups if "*" in g[0]]
    )
    agent = PRODUCT_TOKEN if best else "*" if selected else "none"
    return RobotsRules(
        tuple(rule for _, rs, _ in selected for rule in rs),
        max((d for _, _, ds in selected for d in ds), default=0),
        agent,
    )
