"""Compact offline acceptance: synthetic artifacts only, transports cannot run."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_primary import NOW, make_dossier
from test_reading_integrity import pdf_dossier

from lexradar.auditors.preflight import screen
from lexradar.primary.compact import (
    MAX_BYTES,
    PacketOverflow,
    approve,
    canonical,
    digest,
    preflight,
    prepare,
    unpack,
    verify_quote,
    verify_quotes,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Offline compact workflow attempted network access")

    monkeypatch.setattr("lexradar.auditors.provider.OpenRouterTransport.post", blocked)
    monkeypatch.setattr("lexradar.collector.network.Fetcher.get", blocked)
    monkeypatch.setattr("socket.socket.connect", blocked)


@pytest.mark.parametrize(
    "relative",
    [
        "primary-audit/dossier",
        "primary-audit/partial/dossier",
        "primary-audit/incomplete/dossier",
        "analysis-dossier",
    ],
)
def test_deterministic_lossless_evidence(relative, tmp_path):
    root = EXAMPLES / relative
    packet = prepare(root, as_of=NOW)
    assert packet == prepare(root, as_of=NOW)
    assert len(packet.payload.encode()) <= MAX_BYTES
    assert digest(packet.payload) == packet.sha256
    body = unpack(packet)
    original = json.loads(packet.primary_json)
    assert len(body["primary"]["stages"]) == 13
    assert body["primary"]["coverage"] == original["coverage"]
    assert body["primary"]["limitations"] == original["limitations"]
    traces = json.loads(packet.trace_json)

    def restore(value):
        if isinstance(value, dict):
            if set(value) == {"excerpt"}:
                return traces[value["excerpt"]]
            return {k: restore(v) for k, v in value.items()}
        if isinstance(value, list):
            return [restore(v) for v in value]
        return value

    restored = restore(body["primary"])
    restored["commercial_opportunity"] = original["commercial_opportunity"]
    for got, expected in zip(restored["stages"], original["stages"], strict=True):
        for key in ("possible_paid_legal_work", "paid_work_rationale"):
            got[key] = expected[key]
    for got, expected in zip(restored["materials"], original["materials"], strict=True):
        got["artifacts"] = expected["artifacts"]
    for got, expected in zip(restored["forms"], original["forms"], strict=True):
        for key in ("screenshot", "screenshot_scope"):
            got[key] = expected[key]
    assert restored == original
    preflight(root, packet, tmp_path / "review")
    assert (tmp_path / "review/packet.json").read_text() == packet.payload
    assert not json.loads((tmp_path / "review/preflight.json").read_text())["transfer_allowed"]


def test_forms_organizations_and_conflicts():
    packet = prepare(EXAMPLES / "primary-audit/dossier", as_of=NOW)
    body = unpack(packet)["primary"]
    assert len(body["forms"]) == 2
    assert len(body["organizations"]) >= 2
    assert body["operator_situation"] == "multiple_roles_unestablished"
    assert body["hypotheses"]
    assert body["legal_status"] == "not_confirmed"
    assert body["production_outcome"] == "HOLD"


def test_partial_html_and_pdf(tmp_path):
    packet = prepare(EXAMPLES / "primary-audit/partial/dossier", as_of=NOW)
    assert any(
        m["static_inspection"]["status"] != "complete"
        for m in unpack(packet)["primary"]["materials"]
    )
    root, _ = pdf_dossier(tmp_path, ["Privacy policy", "Unread tail"], pages=1)
    packet = prepare(root, as_of=NOW)
    reading = unpack(packet)["primary"]["materials"][0]["reading"]
    assert not reading["text_coverage_complete"]
    assert reading["unread_page_ranges"] or any(
        p["extraction_state"] == "unread" for p in reading["pages"]
    )


def test_quotes_and_forged_sent_excerpt():
    root = EXAMPLES / "primary-audit/dossier"
    packet = prepare(root, as_of=NOW)
    excerpts = unpack(packet)["excerpts"]
    key, excerpt = next((k, v) for k, v in excerpts.items() if v["quote"])
    verify_quote(packet, key, excerpt["quote"])
    with pytest.raises(ValueError):
        verify_quote(packet, key, "Fabricated material assertion")
    # A real original quote in a different excerpt cannot support this excerpt.
    other = next(
        v["quote"] for v in excerpts.values() if v["quote"] and v["quote"] not in excerpt["quote"]
    )
    with pytest.raises(ValueError):
        verify_quote(packet, key, other)
    traces = json.loads(packet.trace_json)
    traces[key]["quote"] = "Forged"
    with pytest.raises(ValueError):
        verify_quotes(root, replace(packet, trace_json=canonical(traces)))


def test_tamper_blocks_preflight(tmp_path):
    root, data = make_dossier(tmp_path, "<p>Privacy policy</p>")
    packet = prepare(root, as_of=NOW)
    artifact = next(a for a in data.evidence[0].artifacts if a.kind == "html")
    (root / artifact.path).write_text("changed")
    with pytest.raises(ValueError):
        preflight(root, packet, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_oversize_no_truncation(tmp_path):
    # Many unique declared forms cannot be discarded to make a package fit.
    body = "".join(f'<form action="/route{i}"><input name="field{i}"></form>' for i in range(60))
    root, _ = make_dossier(tmp_path, body)
    with pytest.raises(PacketOverflow, match="review_required"):
        prepare(root, as_of=NOW)


def test_approval_exact_hash_and_sensitive_blocking(tmp_path, capsys):
    root, _ = make_dossier(tmp_path, "<p>Privacy policy</p>")
    packet = prepare(root, as_of=NOW)
    with pytest.raises(PermissionError):
        approve(root, packet, None)
    approval = tmp_path / "approval.json"
    record = dict(
        packet_sha256="0" * 64,
        approved=True,
        contents_reviewed=True,
        reviewer="Local reviewer",
        approved_at=NOW.isoformat(),
    )
    approval.write_text(json.dumps(record))
    with pytest.raises(PermissionError):
        approve(root, packet, approval)
    record["packet_sha256"] = packet.sha256
    approval.write_text(json.dumps(record))
    # Generated schema names/hashes are not personal data; public text may pass.
    assert not screen(packet)
    assert approve(root, packet, approval).approved
    sensitive = tmp_path / "sensitive"
    sensitive.mkdir()
    root, _ = make_dossier(
        sensitive,
        '<form><label>Contact private@example.org<input name="email" type="email"></label></form>',
    )
    packet = prepare(root, as_of=NOW)
    assert screen(packet)
    record["packet_sha256"] = packet.sha256
    approval.write_text(json.dumps(record))
    with pytest.raises(PermissionError, match="sensitive"):
        approve(root, packet, approval)
    assert "private@example.org" not in capsys.readouterr().out


def test_compact_cli_and_output_boundaries(tmp_path, monkeypatch):
    from lexradar.cli import main

    root = EXAMPLES / "analysis-dossier"
    output = tmp_path / "review"
    monkeypatch.setattr(
        "sys.argv",
        [
            "lexradar",
            "compact-preflight",
            str(root),
            "--output",
            str(output),
            "--as-of",
            NOW.isoformat(),
        ],
    )
    main()
    assert (output / "trace-map.local.json").is_file()
    packet = prepare(root, as_of=NOW)
    with pytest.raises(ValueError, match="outside"):
        preflight(root, packet, root / "must-not-write")
    with pytest.raises(FileExistsError):
        preflight(root, packet, output)
