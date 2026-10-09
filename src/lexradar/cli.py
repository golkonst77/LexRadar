"""Offline JSON input → internal report JSON."""

import argparse
from pathlib import Path

from .models import AuditInput
from .report import build_report


def main() -> None:
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
