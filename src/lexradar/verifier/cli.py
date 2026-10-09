"""Explicit separate commands allow human review between independent/comparison stages."""

import argparse
from pathlib import Path

from pydantic import TypeAdapter

from ..auditors.provider import ProviderError
from .models import HumanFindingReview, VerifierConfig
from .orchestrator import finish, preflight, start


def default_config() -> VerifierConfig:
    settings = {"model": "offline/verifier", "prompt_price_cap": 1, "completion_price_cap": 1}
    return VerifierConfig(
        auditor_a={**settings, "model": "offline/a"},
        auditor_b={**settings, "model": "offline/b"},
        verifier=settings,
        max_budget_usd=1,
    )


def verifier_main(command: str, argv: list[str]):
    parser = argparse.ArgumentParser(
        description="Independent verifier; no automatic GO/client messages"
    )
    parser.add_argument("dossier", type=Path)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    if command != "verify":
        parser.add_argument("--stage-one", type=Path, required=command == "verify-compare")
        parser.add_argument("--analysis", type=Path, required=command == "verify-compare")
    if command != "verify-preflight":
        parser.add_argument("--mode", choices=("offline", "openrouter"), default="offline")
        parser.add_argument("--allow-external-transfer", action="store_true")
        parser.add_argument("--packet-approval", type=Path)
        if command == "verify-compare":
            parser.add_argument("--finding-reviews", type=Path)
    args = parser.parse_args(argv)
    try:
        config = (
            VerifierConfig.model_validate_json(args.config.read_text(encoding="utf-8"))
            if args.config
            else default_config()
        )
    except (ValueError, OSError):
        parser.error("Invalid verifier configuration")
    if (
        command != "verify-preflight"
        and args.mode == "openrouter"
        and (not args.config or not args.allow_external_transfer or not args.packet_approval)
    ):
        parser.error(
            "OpenRouter requires --config, --allow-external-transfer and exact --packet-approval"
        )
    try:
        if command == "verify-preflight":
            sha = preflight(
                args.dossier,
                args.registry,
                args.output,
                config,
                stage_one=args.stage_one,
                analysis_path=args.analysis,
            )
            print(f"Local verifier preflight: {sha}; review packet and unapproved template")
            return
        if command == "verify":
            report = start(
                args.dossier,
                args.registry,
                args.output,
                config,
                mode=args.mode,
                allow_external_transfer=args.allow_external_transfer,
                packet_approval=args.packet_approval,
            )
        else:
            reviews = None
            if args.finding_reviews:
                if args.finding_reviews.stat().st_size > 1_000_000:
                    raise ValueError("Oversized human finding reviews")
                reviews = TypeAdapter(list[HumanFindingReview]).validate_json(
                    args.finding_reviews.read_text(encoding="utf-8")
                )
            report = finish(
                args.dossier,
                args.registry,
                args.stage_one,
                args.analysis,
                args.output,
                config,
                mode=args.mode,
                allow_external_transfer=args.allow_external_transfer,
                packet_approval=args.packet_approval,
                human_reviews=reviews,
            )
    except (ValueError, OSError, PermissionError, ProviderError, KeyError, TypeError) as exc:
        parser.error(
            f"Verifier failed ({type(exc).__name__}); inspect local inputs, no raw contents logged"
        )
    print(f"Verifier: {report.run_status}; independent review and human approval required")
    if report.run_status not in {"awaiting_comparison", "complete"}:
        parser.exit(1, "Verifier stage did not complete; inspect verification.json\n")
