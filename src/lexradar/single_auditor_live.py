"""Explicit paid Single legal auditor A entrypoint; never sends results to clients."""

import argparse
import json
from pathlib import Path

from .auditors.orchestrator import analyze
from .single_auditor import DeepSeekAdapter, messages_for, profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dossier", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--packet-approval", type=Path, required=True)
    parser.add_argument("--allow-external-transfer", action="store_true", required=True)
    parser.add_argument(
        "--mvp-no-screening",
        action="store_true",
        help="MVP_NO_SCREENING: disable personal-data transfer screening only",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        choices=(45, 120),
        default=120,
        help="Single Auditor MVP request timeout (default: 120)",
    )
    parser.add_argument("--model", default=profile().auditor_a.model)
    args = parser.parse_args()
    report = analyze(
        args.dossier,
        args.output,
        profile(timeout_seconds=args.timeout_seconds, model=args.model),
        mode="openrouter",
        allow_external_transfer=args.allow_external_transfer,
        packet_approval=args.packet_approval,
        provider=DeepSeekAdapter(timeout_seconds=args.timeout_seconds, model=args.model),
        message_builder=messages_for,
        mvp_no_screening=args.mvp_no_screening,
    )
    print(json.dumps({"completed": report.completed, "automatic_send_allowed": False}))


if __name__ == "__main__":
    main()
