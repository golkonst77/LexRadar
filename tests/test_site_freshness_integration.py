"""Small offline collector -> auditor -> operator proposal replay."""

import json
import socket
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_auditors import auditor_response, finding
from test_site_freshness_passive import ReplayFetcher
from test_policy5 import PARTS, PADDING

from lexradar.auditors.evidence import load_packet, policy_material_status
from lexradar.auditors.models import AIAuditorResult, Usage
from lexradar.auditors.orchestrator import analyze
from lexradar.auditors.provider import ProviderReply
from lexradar.collector import CrawlOptions, Limits, collect
from lexradar.collector.freshness import prioritize_policy, site_freshness
from lexradar.collector.models import FreshnessSignal
from lexradar.collector.network import Response
from lexradar.operator_audit import finalize
from lexradar.single_auditor import messages_for, profile


@pytest.mark.parametrize("scenario,defect,priority", [
    ("old_defect", True, "high"), ("old_clean", False, "high"),
    ("no_date", False, None), ("fresh", False, None),
    ("unavailable", False, "high"), ("missing", False, "high"),
    ("technical", False, "high"),
])
def test_local_scenario(tmp_path, monkeypatch, scenario, defect, priority):
    def forbidden(*args, **kwargs):
        raise AssertionError("Live network prohibited")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    year = datetime.now(UTC).year
    footer = "" if scenario in {"no_date", "technical"} else f"<footer>© 2018–{year if scenario == 'fresh' else year - 1}</footer>"
    url = "https://example.test/"
    quote = "Обработка персональных данных осуществляется без ограничения срока."
    policy = "\n".join(v for k, v in PARTS.items() if not (defect and k == "retention")) + "\n" + PADDING
    if defect:
        policy += "\n" + quote
    home = footer + ('' if scenario == 'missing' else '<a href="/privacy">Privacy policy</a>')
    fetcher = ReplayFetcher({
        url: Response(url, 200, "text/html", home.encode(),
                      "Wed, 01 Jan 2020 00:00:00 GMT" if scenario == "technical" else None),
        url + "privacy": Response(url + "privacy", 403 if scenario == "unavailable" else 200,
                                  "text/html", ("<title>Политика конфиденциальности</title><h1>Политика конфиденциальности</h1><p>" + policy + "</p>").encode()),
    })
    root = tmp_path / "dossier"
    collected = collect(url, root, Limits(max_pages=2, max_documents=0), fetcher=fetcher,
                        static_only=True, crawl_options=CrawlOptions(ignore_robots=True))
    assert collected.site_freshness.policy_priority == priority
    packet = load_packet(root, 200000)
    expected_status = {"missing": "not_collected", "unavailable": "unavailable"}.get(scenario, "available")
    assert policy_material_status(packet.data) == expected_status
    assert f"policy_material_status={expected_status}" in packet.payload
    assert json.loads(packet.payload)["untrusted_collector_data"]["site_freshness"].get("policy_priority") == priority
    findings = []
    if defect:
        page = next(p for p in packet.data.pages if str(p.requested_url).endswith("/privacy"))
        f = finding()
        f.update(source=url + "privacy", evidence_ids=[page.evidence_id], fact=quote,
                 fact_assertion="present", examination_scope="text_excerpt",
                 proposed_work=["legal_documents"],
                 text_grounding=[dict(evidence_id=page.evidence_id, exact_quote=quote)])
        findings.append(f)
    raw = auditor_response("A", findings)
    raw["cookie_analytics_review"] = dict(status="NOT_VERIFIED", rationale="Offline replay")
    requests = list(fetcher.requests)
    calls = []

    def complete(settings, messages):
        calls.append(messages)
        assert "SITE_FRESHNESS" in str(messages)
        return ProviderReply(json.dumps(raw), Usage())

    report = analyze(root, tmp_path / "analysis", profile(), message_builder=messages_for,
                     provider=SimpleNamespace(complete=complete))
    assert report.completed and len(calls) == 1
    output = tmp_path / "final"
    output.mkdir()
    decision = finalize(report, AIAuditorResult.model_validate(raw), packet, output,
                        company="Synthetic clinic", replayed=True, commercial_mvp=True)
    assert decision["outcome"] == ("GO" if defect else "HOLD")
    assert (output / "proposal.html").exists() == defect
    if defect:
        assert decision["findings"][0]["fact_status"] == "CONFIRMED"
        assert not decision["findings"][0]["legal_violation_proven"]
    summary = json.loads((output / "summary.json").read_text())
    assert summary["policy_material_status"] == expected_status
    assert summary["site_freshness"]["policy_priority"] == priority
    assert not summary["sent"] and not summary["client_release_allowed"]
    assert fetcher.requests == requests
    assert len(requests) == (1 if scenario == "missing" else 2)


@pytest.mark.parametrize("kind", ["http_last_modified", "pdf_creation_date", "pdf_modification_date"])
def test_technical_year_priority(kind):
    current = datetime.now(UTC).year
    for value, expected in [(f"{current - 1}-01-01T00:00:00+00:00", "high"),
                            (f"{current}-01-01T00:00:00+00:00", None), ("invalid", None)]:
        freshness = site_freshness(b"")
        freshness.signals = [FreshnessSignal(kind=kind, value=value, confidence="server_declared")]
        prioritize_policy(freshness)
        assert freshness.policy_priority == expected
    assert site_freshness(b"", "Wed, 01 Jan 2020 00:00:00 GMT").policy_priority == "high"
    assert site_freshness(b"", etag='"2020"').policy_priority is None
