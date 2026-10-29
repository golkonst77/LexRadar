"""Independent local verification of saved primary outputs; never a client release."""

import argparse
from pathlib import Path

from ..primary.models import PrimaryAudit
from ..primary.qualification_models import InternalAssessment
from .legal_cards import read_local
from .offline import export_verification, verify_saved_assessment


def independent_main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dossier", type=Path)
    parser.add_argument("--primary", type=Path, required=True)
    parser.add_argument("--assessment", type=Path, required=True)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-input-bytes", type=int, default=2_000_000)
    args = parser.parse_args(argv)
    try:
        primary = PrimaryAudit.model_validate_json(read_local(args.primary, 20_000_000))
        assessment = InternalAssessment.model_validate_json(read_local(args.assessment, 20_000_000))
        result = verify_saved_assessment(
            args.dossier,
            primary,
            assessment,
            registry_path=args.registry,
            max_input_bytes=args.max_input_bytes,
        )
        export_verification(result, args.output, args.dossier)
    except (OSError, ValueError, RecursionError):
        parser.error("Independent verification failed safely; no legal/client release")
    print(
        f"Independent local verification: {result.technical_fact_count} reproduced facts; "
        f"{result.contradiction_count} contradictions; {result.gap_count} gaps; HOLD/not_confirmed"
    )
