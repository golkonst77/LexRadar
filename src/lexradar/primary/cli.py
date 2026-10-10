"""Export internal matrices and questions; no client letter, remediation guide or network."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from .service import audit_saved_dossier


def write_report(report, output: Path, source: Path):
    output = output.absolute()
    if output.resolve().is_relative_to(source.resolve()) or any(
        p.is_symlink() for p in (output, *output.parents)
    ):
        raise ValueError("Output must be new and outside the source dossier")
    output.mkdir(parents=True, exist_ok=False)
    (output / "audit.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    for name, rows in (
        ("organizations", report.organizations),
        ("forms", report.forms),
        ("flows", report.flows),
        ("observations", report.observations),
        ("hypotheses", report.hypotheses),
        ("missing-checks", report.missing_checks),
        ("commercial-opportunity", report.commercial_opportunity),
    ):
        payload = {
            "audience": "internal_only",
            "notice": report.notice,
            "source_dossier_sha256": report.dossier_sha256,
            "client_release_allowed": False,
            "rows": [row.model_dump(mode="json") for row in rows],
        }
        (output / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def primary_main(argv: list[str]):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dossier", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--sector", choices=("medical", "unknown"), default="unknown")
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--max-input-bytes", type=int, default=2_000_000)
    args = parser.parse_args(argv)
    try:
        report = audit_saved_dossier(
            args.dossier,
            registry_path=args.registry,
            sector=args.sector,
            as_of=args.as_of,
            max_input_bytes=args.max_input_bytes,
        )
        write_report(report, args.output, args.dossier)
    except (OSError, ValueError, RecursionError):
        parser.error(
            "Primary audit failed safely; inspect local dossier/parameters, no legal release"
        )
    print(
        f"Internal primary audit: {len(report.stages)} working stages; "
        "legal audit incomplete, HOLD/not_confirmed, client release prohibited"
    )
