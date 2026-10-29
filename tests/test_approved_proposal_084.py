"""Offline preservation of the approved GitHub blob and its four editable fields."""

import hashlib
import re
from types import SimpleNamespace

import pytest

from lexradar import client_proposal as cp
from lexradar.collector.models import PageObservation
from lexradar.proposal_rules import NoProposalTriggers


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Proposal rendering attempted network access")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)


def render(**config):
    page = PageObservation(
        requested_url="https://clinic.test/",
        captured_at="2026-10-27T10:00:00Z",
        evidence_id="page-1",
        forms=[
            dict(
                page_url="https://clinic.test/",
                fields=[
                    dict(tag="input", type="tel", name="phone"),
                    dict(
                        tag="input",
                        type="checkbox",
                        checked=True,
                        label="Я согласен на обработку персональных данных",
                    ),
                ],
                policy_links=[],
                consent_links=[],
                submission_context="",
            )
        ],
    )
    packet = SimpleNamespace(
        data=SimpleNamespace(
            pages=[page], evidence=[SimpleNamespace(id="page-1", available=True, artifacts=[])]
        )
    )
    return cp.render_client(
        {"name": "Клиника <&>", "url": "https://clinic.test/?a=1&b=2"},
        [],
        {"date": "17.10.2026", **config},
        policy_packet=packet,
    )


def mask_fields(text):
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
    assert hashlib.sha1(header + raw).hexdigest() == cp.APPROVED_GIT_BLOB
    assert hashlib.sha256(raw).hexdigest() == cp.APPROVED_SHA256


def test_personalization_and_commercial_tail_preserved():
    original = cp.APPROVED_TEMPLATE.read_bytes()
    page = render()
    expected = original.decode("utf-16")
    expected = re.sub(
        r"<p><strong><span[^>]*>Ориентировочное время: 2–5 минут\.</span></strong></p>",
        "", expected,
    )
    after = expected.split("Замечания", 1)[1]
    assert page.endswith(after.split("</p>", 1)[1])
    assert "Замечания предварительные" not in re.sub(r"\s+", " ", page)
    assert "окончательная юридическая квалификация" not in page
    assert "КП v2.5" in page and "КП v2.2" not in page
    assert cp.APPROVED_TEMPLATE.read_bytes() == original
    assert "Руководителю Клиника &lt;&amp;&gt;" in page
    assert "https://clinic.test/?a=1&amp;b=2" in page
    assert "17.10.2026" in page
    assert "UNCONFIRMED" not in page
    assert page.count('data-rule="3"') == 1 and page.count('data-rule="4"') == 1
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


def test_unconfirmed_findings_do_not_create_proposal():
    with pytest.raises(NoProposalTriggers):
        cp.render_client(
            {"url": "https://clinic.test"},
            [dict(confirmed=False, quote="UNCONFIRMED")],
            {"date": "17.10.2026"},
        )
