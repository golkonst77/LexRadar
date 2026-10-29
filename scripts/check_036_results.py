"""Validate saved task-036 outputs without external AI; emit a reproducible inventory."""

import argparse
import hashlib
import json
from pathlib import Path

from reproduce_036 import AS_OF

from lexradar.primary.compact import prepare, unpack, verify_quotes


def check(root):
    hashes = json.loads((root / "sha256.json").read_text())
    for relative, expected in hashes.items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected, relative
    assert not list(root.rglob("*approval*"))
    packet = prepare(root / "dossier", as_of=AS_OF, sector="medical")
    assert packet == prepare(root / "dossier", as_of=AS_OF, sector="medical")
    assert packet.payload == (root / "analysis/packet.json").read_text()
    assert packet.primary_json == (root / "analysis/primary.local.json").read_text()
    verify_quotes(root / "dossier", packet)
    assert len(packet.payload.encode()) <= 40000
    analysis = json.loads((root / "analysis/compact-analysis.json").read_text())
    assert analysis["status"] == "success" and analysis["simulated"]
    assert not analysis["request_sent"] and analysis["budget_charge_usd"] == "0"
    assert "Компактный AI-анализ неполон" in (root / "proposal.html").read_text()
    primary = json.loads(packet.primary_json)
    data = json.loads((root / "dossier/collection.json").read_text())
    body = unpack(packet)
    return {
        "root": str(root),
        "packet_bytes": len(packet.payload.encode()),
        "packet_sha256": packet.sha256,
        "primary_sha256": hashlib.sha256(packet.primary_json.encode()).hexdigest(),
        "manifest_sha256": hashlib.sha256(
            (root / "dossier/collection.json").read_bytes()
        ).hexdigest(),
        "hashes_checked": len(hashes),
        "deterministic_replay": True,
        "excerpts_checked": len(body["excerpts"]),
        "quotes_verified": sum(bool(v["quote"]) for v in body["excerpts"].values()),
        "sent_evidence_ids": sorted({v["evidence_id"] for v in body["excerpts"].values()}),
        "page_attempts": len(data["pages"]),
        "forms": len(primary["forms"]),
        "documents": [
            {
                "url": d["requested_url"],
                "status": d["extraction_status"],
                "pages": d["reading"]["page_count"],
            }
            for d in data["documents"]
        ],
        "selection": body.get("selection", {"complete": True}),
        "crawl": json.loads((root / "dossier/crawl/summary.json").read_text()),
        "request_sent": False,
        "cost_usd": "0",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = [check(root) for root in args.roots]
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    for item in results:
        print(item["root"], item["packet_bytes"], "bytes: integrity, replay and quotes OK")


if __name__ == "__main__":
    main()
