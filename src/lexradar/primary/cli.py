"""Export internal matrices and questions; no client letter, remediation guide or network."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from .qualification import preliminary_outline, qualify_primary
from .service import audit_saved_dossier


def write_report(report, output: Path, source: Path):
    output = output.absolute()
    if output.resolve().is_relative_to(source.resolve()) or any(
        p.is_symlink() for p in (output, *output.parents)
    ):
        raise ValueError("Output must be new and outside the source dossier")
    assessment = qualify_primary(report)
    outline = preliminary_outline(assessment)
    output.mkdir(parents=True, exist_ok=False)
    (output / "audit.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    for name, rows in (
        ("organizations", report.organizations),
        ("forms", report.forms),
        ("flows", report.flows),
        ("declarations", report.declarations),
        ("popup-relations", report.popup_relations),
        ("integration-groups", report.integration_groups),
        ("observations", report.observations),
        ("hypotheses", report.hypotheses),
        ("missing-checks", report.missing_checks),
        ("commercial-opportunity", report.commercial_opportunity),
    ):
        payload = {
            "audience": "internal_only",
            "notice": report.notice,
            "source_dossier_sha256": report.dossier_sha256,
            "source_origin": report.source_origin.model_dump(mode="json"),
            "materials": [
                {
                    "evidence_id": material.evidence_id,
                    "origin": material.origin.model_dump(mode="json"),
                    "static_inspection": material.static_inspection.model_dump(mode="json"),
                }
                for material in report.materials
            ],
            "client_release_allowed": False,
            "rows": [row.model_dump(mode="json") for row in rows],
        }
        if name == "forms":
            # Empty rows can mean the parser never reached a form. Preserve limitations
            # even when there is no FormRow in which to carry them.
            payload["inventory_scope"] = "observed_forms_only_not_proof_of_absence"
            payload["limitations"] = report.limitations
        (output / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    for name, payload in (
        (
            "legal-findings",
            {"rows": [f.model_dump(mode="json") for f in assessment.legal_findings]},
        ),
        (
            "commercial-work",
            {"rows": [w.model_dump(mode="json") for w in assessment.commercial_work]},
        ),
    ):
        payload.update(
            {
                "audience": "internal_only",
                "notice": assessment.notice,
                "source_primary_sha256": assessment.source_primary_sha256,
                "source_primary_hash_scope": assessment.source_primary_hash_scope,
                "source_dossier_sha256": assessment.source_dossier_sha256,
                "source_origin": assessment.source_origin.model_dump(mode="json"),
                "production_outcome": "HOLD",
                "legal_status": "not_confirmed",
                "client_release_allowed": False,
                "automatic_send_allowed": False,
            }
        )
        (output / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    (output / "internal-assessment.json").write_text(
        assessment.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (output / "preliminary-client-summary.json").write_text(
        outline.model_dump_json(indent=2) + "\n", encoding="utf-8"
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
