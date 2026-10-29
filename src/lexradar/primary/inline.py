"""Bounded lexical patterns, not JavaScript interpretation or browser emulation."""

import re
from dataclasses import dataclass


@dataclass
class Token:
    value: str
    start: int
    end: int
    string: bool = False


def tokens(source: str):
    result = []
    i = 0
    while i < len(source) and len(result) < 4096:
        if source[i].isspace():
            i += 1
        elif source.startswith("//", i):
            end = source.find("\n", i + 2)
            i = len(source) if end < 0 else end + 1
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end < 0:
                return result, "Unclosed JS comment; suffix not inspected"
            i = end + 2
        elif source[i] in "\"'`":
            start, quote = i, source[i]
            i += 1
            escaped = False
            while i < len(source):
                if source[i] == "\\":
                    escaped = True
                    i += 2
                elif source[i] == quote:
                    break
                else:
                    i += 1
            if i >= len(source):
                return result, "Unclosed JS string; suffix not inspected"
            i += 1
            # Template/escaped strings are opaque, not computed/interpolated.
            result.append(
                Token(
                    source[start + 1 : i - 1] if not escaped and quote != "`" else "",
                    start,
                    i,
                    True,
                )
            )
        elif source[i] == "/":
            return result, "JS regex/division ambiguity; suffix not inspected"
        else:
            match = re.match(r"[$A-Za-z_][$A-Za-z_0-9]*|[0-9]+|.", source[i:], re.DOTALL)
            value = match.group()
            result.append(Token(value, i, i + len(value)))
            i += len(value)
    return result, "JS token limit; suffix not inspected" if i < len(source) else None


def resources(source: str):
    stream, limitation = tokens(source)
    found = []
    for i in range(1, len(stream) - 3):
        if (
            stream[i].value == "."
            and not stream[i - 1].string
            and re.fullmatch(r"[$A-Za-z_][$A-Za-z_0-9]*", stream[i - 1].value)
            and stream[i + 1].value == "src"
            and stream[i + 2].value == "="
            and stream[i + 3].string
            and stream[i + 3].value.startswith(("https://", "http://"))
        ):
            end = i + 4
            while end < len(stream) and stream[end].value not in {";", "}"}:
                end += 1
            literal = stream[i + 3]
            dynamic = end != i + 4
            found.append((stream[i - 1].start, stream[end - 1].end, literal.value, dynamic))
    return found, limitation


def route_literals(stream):
    found = []
    for i, item in enumerate(stream[:-2]):
        if (
            not item.string
            and item.value == "fetch"
            and stream[i + 1].value == "("
            and stream[i + 2].string
        ):
            # Bare fetch only, not an unrelated object's .fetch method.
            if i == 0 or stream[i - 1].value != ".":
                found.append(stream[i + 2])
        if (
            item.value == "url"
            and stream[i + 1].value == ":"
            and stream[i + 2].string
            and any(t.value == "ajax" and not t.string for t in stream[max(0, i - 8) : i])
        ):
            found.append(stream[i + 2])
    return found


def form_routes(source: str):
    """Only exact selector + submit callback with lexically bounded braces."""
    stream, limitation = tokens(source)
    found = []
    for i, item in enumerate(stream):
        if not item.string or not re.fullmatch(r"#[A-Za-z_][A-Za-z0-9_-]*", item.value):
            continue
        # Selector must be document.querySelector('#id') or $('#id'), not a quoted example.
        before = [t.value for t in stream[max(0, i - 4) : i]]
        valid = before == ["document", ".", "querySelector", "("] or before[-2:] == ["$", "("]
        if not valid or any(t.string for t in stream[max(0, i - 2) : i]):
            continue
        tail = stream[i + 1 : i + 8]
        values = [t.value for t in tail]
        if (
            len(tail) < 6
            or values[:2] != [")", "."]
            or values[2] not in {"on", "addEventListener"}
            or values[3] != "("
            or not tail[4].string
            or values[4:6] != ["submit", ","]
        ):
            continue
        start = i + 7
        # Require a plain function callback, not variable aliases or an arbitrary later brace.
        if start >= len(stream) or stream[start].value != "function":
            continue
        opening = next(
            (
                j
                for j in range(start, min(start + 12, len(stream)))
                if stream[j].value == "{" and not stream[j].string
            ),
            None,
        )
        if opening is None:
            continue
        depth = 1
        close = opening + 1
        while close < len(stream) and depth:
            if not stream[close].string:
                depth += (stream[close].value == "{") - (stream[close].value == "}")
            close += 1
        if depth:
            continue
        for route in route_literals(stream[opening + 1 : close - 1]):
            if route.value:
                found.append((item.value[1:], route.start, route.end, route.value))
    return found, limitation
