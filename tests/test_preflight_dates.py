"""Calendar screening regressions; no real personal records."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lexradar.auditors.preflight import authorize, screen
from lexradar.primary.compact import digest, prepare

ROOT = Path(__file__).resolve().parents[1] / "examples/compact-first-pilot/dossier"


@pytest.fixture(scope="module")
def packet():
    return prepare(ROOT, as_of=datetime(2026, 10, 10, tzinfo=UTC))


def with_text(packet, text):
    payload = json.dumps({"text": text})
    return replace(packet, payload=payload, sha256=digest(payload))


@pytest.mark.parametrize("text", ["31.02.2020", "29/02/2023", "00.12.2000", "12.13.2000"])
def test_impossible_calendar_date_is_not_birth_date(packet, text):
    candidate = with_text(packet, text)
    assert "birth_date" not in {v["category"] for v in screen(candidate)}
    # This correction does not authorize a packet without review.
    with pytest.raises(PermissionError, match="approval"):
        authorize(candidate, None)


@pytest.mark.parametrize(
    "text",
    [
        "29.02.2024",
        "1/1/2000",
        "31.12.1999",
        "31.02.2020 then 01.01.2000",
        "%32%39%2E%30%32%2E%32%30%32%34",
        "２９.０２.２０２４",
        "29.\u200b02.2024",
    ],
)
def test_valid_dates_still_block(packet, text):
    candidate = with_text(packet, text)
    assert "birth_date" in {v["category"] for v in screen(candidate)}
    with pytest.raises(PermissionError, match="sensitive"):
        authorize(candidate, None)


@pytest.mark.parametrize(
    "text,category",
    [
        ("31.02.2020 private@example.org", "email"),
        ("31.02.2020 +7 (999) 123-45-67", "phone_or_identifier"),
        ("31.02.2020 patient diagnosis", "identity_or_health_context"),
        ("31.02.2020 Иванов Иван Иванович", "person_name"),
    ],
)
def test_impossible_date_does_not_hide_other_categories(packet, text, category):
    assert category in {v["category"] for v in screen(with_text(packet, text))}
