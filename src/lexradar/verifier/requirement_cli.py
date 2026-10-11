"""Offline normative matrix, separate from legal approval and client release."""

import argparse
from datetime import UTC, date, datetime
from pathlib import Path

from ..primary.models import PrimaryAudit
from ..primary.qualification_models import InternalAssessment
from .legal_cards import read_local
from .requirements import export_normative, verify_normative


def normative_main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dossier", type=Path)
    parser.add_argument("--primary", type=Path, required=True)
    parser.add_argument("--assessment", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--event-date", type=date.fromisoformat, required=True)
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--max-input-bytes", type=int, default=2_000_000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        primary = PrimaryAudit.model_validate_json(read_local(args.primary, 20_000_000))
        assessment = InternalAssessment.model_validate_json(read_local(args.assessment, 20_000_000))
        result = verify_normative(
            args.dossier,
            primary,
            assessment,
            args.registry,
            event_date=args.event_date,
            evaluated_at=args.as_of or datetime.now(UTC),
            max_input_bytes=args.max_input_bytes,
        )
        export_normative(result, args.output, args.dossier)
    except (OSError, ValueError, RecursionError):
        parser.error("Normative verification failed safely; no legal/client confirmation")
    print(
        f"Normative candidates: {len(result.requirements)}; "
        f"questions: {len(result.question_summaries)}; HOLD/not_confirmed; revision unverified"
    )
