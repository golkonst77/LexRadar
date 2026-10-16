"""Reproduce task 036 locally: live bounded GET or offline replay; never external AI."""

import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

from lexradar.auditors.compact_pipeline import analyze_compact
from lexradar.auditors.models import AnalysisConfig, ModelSettings
from lexradar.commercial_preview import render_mvp
from lexradar.mvp import run

AS_OF = datetime.fromisoformat("2026-10-15T12:00:00+00:00")


def replay(dossier, output):
    output.mkdir(parents=True, exist_ok=False)
    shutil.copytree(dossier, output / "dossier")
    settings = ModelSettings(
        model="synthetic/compact-a",
        prompt_price_cap="0.10",
        completion_price_cap="0.10",
        max_output_tokens=2000,
    )
    config = AnalysisConfig(
        auditor_a=settings,
        auditor_b=settings,
        single_auditor=True,
        max_budget_usd="0.05",
        retries=0,
    )
    (output / "config.json").write_text(config.model_dump_json(indent=2))
    report = analyze_compact(
        output / "dossier",
        output / "analysis",
        config,
        as_of=AS_OF,
        sector="medical",
        mode="offline",
        approval_template=False,
    )
    assert report["status"] == "success", report["status"]
    assert not report["request_sent"]
    assert report["budget_charge_usd"] == "0"
    assert report["preview"]["packet_bytes"] <= 40000
    render_mvp(output, output / "proposal.html")
    (output / "run.json").write_text(
        json.dumps(
            {
                "status": "success",
                "replay_source": str(dossier),
                "as_of": AS_OF.isoformat(),
                "ai_mode": "offline",
                "request_sent": False,
                "cost_usd": "0",
                "client_release_allowed": False,
            },
            indent=2,
        )
    )
    hashes = {
        str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(output.rglob("*"))
        if p.is_file()
    }
    (output / "sha256.json").write_text(json.dumps(hashes, indent=2))
    print(output, report["preview"]["packet_bytes"], "bytes; HOLD; USD 0")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dossier", type=Path)
    parser.add_argument("--url")
    args = parser.parse_args()
    if bool(args.dossier) == bool(args.url):
        parser.error("Choose exactly one: --dossier or --url")
    if args.dossier:
        replay(args.dossier, args.output)
    else:
        run(args.url, args.output, static_only=True, as_of=AS_OF, max_pages=12)


if __name__ == "__main__":
    main()
