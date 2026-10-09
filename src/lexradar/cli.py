"""Offline JSON input → internal report JSON."""

import argparse
import sys
from pathlib import Path

from .models import AuditInput
from .report import build_report


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "collect":
        from .collector import Limits, collect

        parser = argparse.ArgumentParser(
            description="Collect technical facts without legal decisions"
        )
        parser.add_argument("url")
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument("--max-pages", type=int, default=10)
        parser.add_argument("--max-documents", type=int, default=10)
        parser.add_argument("--timeout", type=float, default=10)
        args = parser.parse_args(sys.argv[2:])
        result = collect(
            args.url,
            args.output,
            Limits(
                max_pages=args.max_pages,
                max_documents=args.max_documents,
                timeout_seconds=args.timeout,
            ),
        )
        print(f"Collected {len(result.pages)} pages; independent review required")
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report(AuditInput.model_validate_json(args.input.read_text(encoding="utf-8")))
    payload = report.model_dump_json(indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
