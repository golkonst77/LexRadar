"""Local decision/review commands never invoke networking or legacy GO."""

import argparse
from pathlib import Path

from .service import decide_production, prepare_review


def decision_main(command: str, argv: list[str]):
    parser = argparse.ArgumentParser(description="Production legal decision: HOLD until trusted")
    parser.add_argument("dossier", type=Path)
    parser.add_argument("--finding", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--client-text", type=Path)
    parser.add_argument("--imported-report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    if command == "decide":
        parser.add_argument("--review", type=Path)
    args = parser.parse_args(argv)
    try:
        if command == "review-decision":
            prepare_review(
                args.dossier,
                args.finding,
                args.registry,
                args.output,
                client_text=args.client_text,
                imported_report=args.imported_report,
            )
            print("Review template is unapproved/untrusted; matching hashes cannot grant GO")
        else:
            report = decide_production(
                args.dossier,
                args.finding,
                args.registry,
                review_path=args.review,
                client_text=args.client_text,
                imported_report=args.imported_report,
            )
            with args.output.open("x", encoding="utf-8") as file:
                file.write(report.model_dump_json(indent=2) + "\n")
            print("Production HOLD / not confirmed; client release and sending prohibited")
            if not report.technical_processing_completed:
                parser.exit(1, "Input verification failed; inspect local decision report\n")
    except (ValueError, OSError, TypeError, RecursionError):
        parser.error("Local decision preparation failed; no legal confirmation granted")
