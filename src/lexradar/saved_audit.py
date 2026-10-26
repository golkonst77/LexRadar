"""Verify the saved real Doktstom audit locally, with zero new requests."""

import argparse
import csv
import hashlib
import json
import shutil
from pathlib import Path

from .auditors.evidence import load_packet
from .auditors.models import AnalysisReport
from .batch import safe_path, snapshot, write_json
from .batch_qualification import qualify
from .evidence_review import review
from .mvp_batch import run
from .single_auditor import checked_report, profile


def verify(
    output,
    *,
    dossier=Path("runs-054/doktstom-improved/dossier"),
    audit=Path("runs-064/doktstom-deepseek-chat-001"),
):
    output, dossier, audit = map(safe_path, (output, dossier, audit))
    snapshot(dossier)
    packet = load_packet(dossier, profile().max_input_bytes)
    paths = [safe_path(audit / name) for name in ("analysis.json", "run.json", "requests.jsonl")]
    analysis = AnalysisReport.model_validate_json(paths[0].read_bytes())
    checked_report(analysis, packet)
    metadata = json.loads(paths[1].read_bytes())
    logs = [json.loads(line) for line in paths[2].read_text().splitlines() if line.strip()]
    if logs != [log.model_dump(mode="json") for log in analysis.logs]:
        raise ValueError("Saved request ledger differs from analysis")
    if (
        metadata.get("mode") != "openrouter"
        or metadata.get("collector_manifest_sha256") != packet.manifest_sha256
        or metadata.get("packet_approval", {}).get("packet_sha256") != packet.sha256
        or not metadata.get("external_transfer_explicitly_permitted")
    ):
        raise ValueError("Saved run provenance mismatch")
    output.mkdir(parents=True, exist_ok=False)
    source_csv = output / "saved-input.csv"
    with source_csv.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(
            file, fieldnames=["url", "company_name", "email", "dossier", "analysis"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "url": str(packet.data.target_url),
                "company_name": "ООО «СТОМАТОЛОГИЧЕСКИЙ ЦЕНТР ДОКТОР СТОМ»",
                "email": "",
                "dossier": str(dossier),
                "analysis": str(paths[0]),
            }
        )
    state = run(source_csv, output / "real-batch", mode="saved", max_sites=1)
    if state["rows"][0]["status"] != "SUCCESS":
        raise ValueError("Saved batch failed: " + state["rows"][0]["reason"])
    real = output / "doktstom-real"
    real.mkdir()
    decision = review(
        analysis, packet, real, qualification=qualify(analysis, packet, production=True)
    )
    site = output / "real-batch" / state["rows"][0]["directory"]
    if decision["outcome"] == "GO":
        shutil.copyfile(site / "proposal.html", real / "proposal.html")
    write_json(
        real / "provenance.json",
        {
            "source_dossier": str(dossier),
            "source_audit": str(audit),
            "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
            "packet_sha256": packet.sha256,
            "manifest_sha256": packet.manifest_sha256,
            "historical_completed": analysis.completed,
            "historical_cost_usd": str(analysis.budget_charged_usd),
            "historical_usage": [log.usage.model_dump(mode="json") for log in analysis.logs],
            "historical_MVP_NO_SCREENING": metadata.get("MVP_NO_SCREENING", False),
            "new_ai_calls": 0,
            "new_cost_usd": "0",
            "automatic_send_allowed": False,
            "origins": [page.origin.model_dump(mode="json") for page in packet.data.pages],
        },
    )
    return decision


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    decision = verify(args.output)
    print(f"{decision['outcome']}; new AI calls: 0; {args.output}/real-batch/results.csv")


if __name__ == "__main__":
    main()
