"""Offline regression against the exact approved 074 packet."""

import json
from pathlib import Path

import pytest

from lexradar.auditors.evidence import load_packet
from lexradar.auditors.grounding import exact_locations
from lexradar.auditors.validator import validate_result
from lexradar.document_review import review_candidate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def packet():
    return load_packet(ROOT / "runs-054/doktstom-improved/dossier", 200_000)


def test_three_saved_quotes_are_text_only(packet):
    raw = (ROOT / "runs-074/analysis/capture-A-1/original.txt").read_text()
    result, _ = validate_result(raw, "A", packet)
    assert len(result.findings) == 3
    for f in result.findings:
        assert f.fact_supported
        q = f.text_grounding[0]
        assert packet.data.pages[0].text[q.start : q.end] == f.fact
        assert all(n.actuality_status == "unverified" for n in f.normative_basis)
        assert f.status != "confirmed"


@pytest.mark.parametrize(
    "quote,ids",
    [
        ("invented", ["page-0001"]),
        ("Сайт", ["page-0001"]),
        ("Оператор", ["missing"]),
        ("", ["page-0001"]),
    ],
)
def test_missing_ambiguous_unknown_rejected(packet, quote, ids):
    assert not exact_locations(packet, ids, packet.data.evidence[0].source, quote)


def test_wrong_source_and_offset_fail_closed(packet):
    raw = json.loads((ROOT / "runs-074/analysis/capture-A-1/original.txt").read_text())
    f = raw["findings"][0]
    assert not exact_locations(packet, f["evidence_ids"], "https://other.example/", f["fact"])
    f["text_grounding"] = [dict(evidence_id="page-0001", exact_quote=f["fact"], start=0, end=1)]
    f["examination_scope"] = "text_excerpt"
    result, _ = validate_result(json.dumps(raw), "A", packet)
    assert not result.findings[0].fact_supported


def test_editorial_candidate_requires_exact_proof_and_work(packet):
    args = dict(
        identifier="editorial",
        source=packet.data.evidence[0].source,
        evidence_ids=["page-0001"],
        quote="пройти техническое обслуживание",
        defect="Шаблонная фраза",
        paid_work="Редакционная переработка",
    )
    candidate = review_candidate(packet, **args)
    assert candidate["outcome"] == "REVIEW_CANDIDATE"
    assert candidate["legal_violation_proven"] is False
    assert candidate["automatic_send_allowed"] is False
    assert review_candidate(packet, **(args | {"paid_work": ""})) is None
    assert review_candidate(packet, **(args | {"quote": "invented"})) is None
