"""Wire selection, reachable pooling and safe recovered static discovery."""

import json
from dataclasses import replace

import pytest
from test_primary import NOW, make_dossier

from lexradar.collector.static_renderer import StaticPage
from lexradar.primary.compact import (
    PacketOverflow,
    canonical,
    digest,
    prepare,
    unpack,
    verify_quotes,
)


def test_recovered_static_discovery_forms_and_links():
    page = StaticPage()
    page.url = "https://example.com/"
    page.fulfill(
        body=b'<p>before</span><a href="/policy">Policy</a>'
        b'<form><input name="email"><a href="/consent">Consent</a></form>'
    )
    data = page.evaluate("")
    assert {link["url"] for link in data["links"]} == {
        "https://example.com/policy",
        "https://example.com/consent",
    }
    assert data["forms"][0]["fields"][0]["name"] == "email"
    assert data["forms"][0]["links"][0]["url"] == "https://example.com/consent"


def test_pool_has_no_unreachable_entries(tmp_path):
    root, _ = make_dossier(tmp_path, "<p>Privacy policy</p>")
    packet = prepare(root, as_of=NOW)
    wire = json.loads(packet.payload)
    reached = set()

    def visit(value):
        if isinstance(value, dict):
            if set(value) == {"$"}:
                key = value["$"]
                if key not in reached:
                    reached.add(key)
                    visit(wire["dictionary"][key])
            else:
                for child in value.values():
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(wire["body"])
    assert reached == set(wire["dictionary"])
    assert unpack(packet)["primary"]


def test_irreducible_overflow_blocks(tmp_path, monkeypatch):
    root, _ = make_dossier(tmp_path, "<p>Privacy policy</p>")
    monkeypatch.setattr("lexradar.primary.compact.MAX_BYTES", 100)
    with pytest.raises(PacketOverflow):
        prepare(root, as_of=NOW)


def test_semantic_forgery_outside_region_blocked(tmp_path):
    root, _ = make_dossier(tmp_path, "<p>ООО <b>«Клиника»</b> ИНН 1234567890</p>")
    packet = prepare(root, as_of=NOW)
    # A payload and trace edited consistently still need to reproduce the original.
    traces = json.loads(packet.trace_json)
    key = next(k for k, v in traces.items() if v["quote"])
    old_quote = traces[key]["quote"]
    payload = packet.payload.replace(old_quote, "Never occurred")
    if payload == packet.payload:  # quote may be JSON-escaped
        payload = packet.payload.replace(
            json.dumps(old_quote, ensure_ascii=False), json.dumps("Never occurred")
        )
    traces[key]["quote"] = "Never occurred"
    forged = replace(packet, payload=payload, sha256=digest(payload), trace_json=canonical(traces))
    with pytest.raises(ValueError):
        verify_quotes(root, forged)


def test_ambiguous_head_link_does_not_hide_body():
    page = StaticPage()
    page.url = "https://example.com/"
    page.fulfill(
        body=b'<html><head><link rel="preload" rel="preconnect" href="/asset">'
        b'</head><body><a href="/policy">Policy</a></body></html>'
    )
    assert page.evaluate("")["links"] == [{"url": "https://example.com/policy", "text": "Policy"}]
    assert not page.parser.resources
    assert page.parser.limitations


def test_static_crawl_keeps_policy_priority(tmp_path):
    from lexradar.collector import CrawlOptions, Limits, collect
    from lexradar.collector.network import Response

    class Synthetic:
        synthetic = True

        def get(self, url, allowed_origin, *, before_request=None, on_event=None, **kwargs):
            if before_request:
                before_request(url)
            body = b"<p>content</p>"
            if url.endswith("/robots.txt"):
                body = b"User-agent: *\nDisallow:\n"
            elif url == "https://example.com/":
                body = b'<a href="/a-service">Appointment</a><a href="/z-policy">Privacy policy</a>'
            if on_event:
                on_event("fixture_read", url, 200)
            return Response(
                url, 200, "text/plain" if url.endswith("robots.txt") else "text/html", body
            )

    result = collect(
        "https://example.com/",
        tmp_path / "dossier",
        Limits(max_pages=3),
        fetcher=Synthetic(),
        static_only=True,
        crawl_options=CrawlOptions(max_requests=5),
    )
    assert [str(p.requested_url) for p in result.pages] == [
        "https://example.com/",
        "https://example.com/z-policy",
        "https://example.com/a-service",
    ]
