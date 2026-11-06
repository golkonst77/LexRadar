"""Ten isolated synthetic CSV audits through the approved v2.4 renderer."""

import csv
import hashlib
import json
import re
import socket
from pathlib import Path

import pytest

from lexradar import client_proposal as cp
from lexradar.auditors.provider import OpenRouterTransport
from lexradar.collector.artifacts import ArtifactStore
from lexradar.collector.models import CollectedEvidence, CollectionResult, Limits, PageObservation
from lexradar.collector.reading import extract_html
from lexradar.mvp_batch import run
from lexradar.mvp_demo import create_inputs


def mask_fields(text):
    text = re.sub(r'(?<=<div class="alert">).*?(?=<p>Жалоба пациента)', "FINES", text, flags=re.S)
    text = re.sub(r'(?<=contenteditable="true">).*?(?=</span>)', "FIELD", text)
    return re.sub(
        r'(?<=<ol class="findings editable" contenteditable="true">).*?(?=</ol>)',
        "FINDINGS",
        text,
        flags=re.S,
    )


def exercise(root):
    root.mkdir(parents=True, exist_ok=False)
    seed = create_inputs(root / "seed")
    response = json.loads((seed.parent / "response-0.json").read_text())
    original = CollectionResult.model_validate_json(
        (seed.parent / "dossier-0/collection.json").read_bytes()
    )
    now = original.started_at
    rows = []
    for i in range(10):
        url = f"https://organization-{i:02d}.example/"
        dossier = root / f"dossier-{i:02d}"
        store = ArtifactStore(dossier, Limits())
        body = (
            f"<html><body>УНИКАЛЬНЫЙ_ФАКТ_{i:02d}: согласие на обработку данных "
            "включено в общий текст условий заявки.</body></html>"
        ).encode()
        text, reading = extract_html(body, 100000)
        artifacts = [
            store.save("page-0001.html", body, "html"),
            store.save("page-0001.txt", text.encode(), "text"),
            store.save("page-0001.png", b"SYNTHETIC screenshot", "screenshot"),
        ]
        evidence = CollectedEvidence(
            id="page-0001",
            source=url,
            captured_at=now,
            observed_fact="Synthetic saved text",
            available=True,
            status="complete",
            artifacts=artifacts,
            artifact_path=artifacts[0].path,
            sha256=artifacts[0].sha256,
        )
        collection = CollectionResult(
            target_url=url,
            started_at=now,
            limits=Limits(),
            evidence=[evidence],
            pages=[
                PageObservation(
                    requested_url=url,
                    final_url=url,
                    captured_at=now,
                    http_status=200,
                    text=text,
                    reading=reading,
                    evidence_id=evidence.id,
                )
            ],
        )
        (dossier / "collection.json").write_text(collection.model_dump_json(indent=2))
        local = json.loads(json.dumps(response))
        finding = local["findings"][0]
        finding.update(id=f"finding-{i:02d}", source=url, fact=text)
        finding["text_grounding"][0]["exact_quote"] = text
        if i >= 8:
            local["findings"] = []
        response_path = root / f"response-{i:02d}.json"
        response_path.write_text(json.dumps(local, ensure_ascii=False))
        rows.append(
            dict(
                url=url,
                company_name=f"DEMO Организация {i:02d}",
                email="",
                date=f"{i + 1:02d}.10.2026",
                dossier=str(dossier.absolute()),
                response=str(response_path.absolute()),
            )
        )
    csv_path = root / "input.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    output = root / "batch"
    before = cp.APPROVED_TEMPLATE.read_bytes()
    state = run(csv_path, output, max_sites=10, max_ai_calls=0, max_total_budget_usd="0.01")
    assert state["summary"] == {"SUCCESS": 10}
    assert state["qualification_summary"] == {"GO": 8, "HOLD": 2}
    assert not state["automatic_send_allowed"] and not state["client_release_allowed"]
    assert cp.APPROVED_TEMPLATE.read_bytes() == before
    assert hashlib.sha256(before).hexdigest() == cp.APPROVED_SHA256
    pages = []
    for i, row in enumerate(state["rows"]):
        assert row["ai_calls"] == 0 and row["cost_usd"] == "0"
        directory = output / row["directory"]
        report = json.loads((directory / "analysis/analysis.json").read_text())
        cookie = report["runs"][0]["result"]["cookie_analytics_review"]
        assert cookie["status"] == "NOT_VERIFIED"
        assert cookie["rationale"] and cookie["limitations"]
        assert cookie["evidence"] == []
        assert (directory / "evidence-review.json").is_file()
        if i >= 8:
            assert row["qualification"] == "HOLD" and not row.get("proposal")
            assert not (directory / "proposal.html").exists()
            continue
        page = (output / row["proposal"]).read_text()
        expected = before.decode().replace("LexRadar · КП v2.2", "LexRadar · КП v2.4", 1)
        assert mask_fields(page) == mask_fields(expected)
        assert row["input"]["company_name"] in page
        assert row["url"] in page and row["input"]["date"] in page
        assert f"УНИКАЛЬНЫЙ_ФАКТ_{i:02d}" in page
        for other in range(10):
            if other != i:
                assert f"УНИКАЛЬНЫЙ_ФАКТ_{other:02d}" not in page
                assert rows[other]["url"] not in page
                assert rows[other]["company_name"] not in page
        pages.append(page)
    assert len(set(pages)) == 8
    return state


def test_ten_organizations(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Network and paid AI prohibited")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(OpenRouterTransport, "post", forbidden)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    exercise(tmp_path / "ten")


if __name__ == "__main__":
    with pytest.MonkeyPatch.context() as patch:
        test_ten_organizations(Path("runs-085"), patch)
