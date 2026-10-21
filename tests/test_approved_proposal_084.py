"""Offline preservation of the approved GitHub blob and its four editable fields."""

import hashlib
import re

import pytest

from lexradar import client_proposal as cp


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Proposal rendering attempted network access")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)


def render(**config):
    return cp.render_client(
        {"name": "Клиника <&>", "url": "https://clinic.test/?a=1&b=2"},
        [dict(confirmed=True, title="Факт <&>", quote="Цитата <&>", meaning="Работа <&>")] * 2
        + [dict(confirmed=False, title="UNCONFIRMED", quote="UNCONFIRMED")],
        {"date": "17.10.2026", **config},
    )


def mask_fields(text):
    text = re.sub(r'(?<=<div class="alert">).*?(?=<p>Жалоба пациента)', "FINES", text, flags=re.S)
    # Independent matcher: preserve every byte outside the four inner field values.
    text = re.sub(r'(?<=contenteditable="true">).*?(?=</span>)', "FIELD", text)
    return re.sub(
        r'(?<=<ol class="findings editable" contenteditable="true">).*?(?=</ol>)',
        "FINDINGS",
        text,
        flags=re.S,
    )


def test_original_github_blob():
    raw = cp.APPROVED_TEMPLATE.read_bytes()
    header = b"blob " + str(len(raw)).encode() + b"\0"
    assert hashlib.sha1(header + raw).hexdigest() == "028404f2de14ab9a77c442d081e5ba0cda86b334"
    assert hashlib.sha256(raw).hexdigest() == cp.APPROVED_SHA256


def test_only_four_inner_fields_change():
    original = cp.APPROVED_TEMPLATE.read_bytes()
    page = render()
    expected = original.decode().replace("LexRadar · КП v2.2", "LexRadar · КП v2.4", 1)
    assert mask_fields(page) == mask_fields(expected)
    assert "LexRadar · КП v2.4" in page and "LexRadar · КП v2.2" not in page
    assert cp.APPROVED_TEMPLATE.read_bytes() == original
    assert "Руководителю Клиника &lt;&amp;&gt;" in page
    assert "https://clinic.test/?a=1&amp;b=2" in page
    assert "17.10.2026" in page
    assert "UNCONFIRMED" not in page
    assert page.count("<li><strong>Факт &lt;&amp;&gt;") == 2
    assert "Цитата &lt;&amp;&gt;" in page
    assert "[Вставить" not in page


def test_commercial_overrides_cannot_change_approved_copy():
    assert render() == render(
        price=1, discount_percent=99, term="tomorrow", discount_deadline_inclusive="2099-01-01"
    )
    assert "01.11.2026 включительно" in render()


def test_template_tampering_fails_closed(tmp_path, monkeypatch):
    changed = tmp_path / "template.html"
    changed.write_bytes(cp.APPROVED_TEMPLATE.read_bytes() + b" ")
    monkeypatch.setattr(cp, "APPROVED_TEMPLATE", changed)
    with pytest.raises(ValueError, match="integrity"):
        render()


def test_unconfirmed_finding_rejected():
    with pytest.raises(ValueError, match="1–4"):
        cp.render_client(
            {"url": "https://clinic.test"},
            [dict(confirmed=False, quote="fact")],
            {"date": "17.10.2026"},
        )
