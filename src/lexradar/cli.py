"""Local production decision or explicitly labeled legacy/demo JSON processing."""

import argparse
import sys
from pathlib import Path

from .models import AuditInput
from .report import build_report


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in {"decide", "review-decision"}:
        from .decision.cli import decision_main

        decision_main(sys.argv[1], sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] in {"benchmark", "quality-report", "pilot"}:
        from .quality.cli import quality_main

        quality_main(sys.argv[1], sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "attest-source":
        from .verifier.attestation import attestation_main

        attestation_main(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] in {"verify", "verify-compare", "verify-preflight"}:
        from .verifier.cli import verifier_main

        verifier_main(sys.argv[1], sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "preflight":
        from .auditors.evidence import load_packet
        from .auditors.preflight import write_preflight

        parser = argparse.ArgumentParser(description="Local packet review; no external transfer")
        parser.add_argument("dossier", type=Path)
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument("--max-input-bytes", type=int, default=200000)
        args = parser.parse_args(sys.argv[2:])
        try:
            write_preflight(load_packet(args.dossier, args.max_input_bytes), args.output)
        except (ValueError, OSError):
            parser.error("Preflight failed; inspect dossier locally")
        print("Review exact packet.json and preflight.json; approval template is NOT approved")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "analyze":
        from .auditors.models import AnalysisConfig
        from .auditors.orchestrator import analyze
        from .auditors.provider import ProviderError

        parser = argparse.ArgumentParser(
            description="Independent review of a saved Collector dossier"
        )
        parser.add_argument("dossier", type=Path)
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument("--mode", choices=("offline", "openrouter"), default="offline")
        parser.add_argument("--config", type=Path)
        parser.add_argument("--allow-external-transfer", action="store_true")
        parser.add_argument("--packet-approval", type=Path)
        args = parser.parse_args(sys.argv[2:])
        if args.mode == "openrouter" and (
            not args.config or not args.allow_external_transfer or not args.packet_approval
        ):
            parser.error(
                "OpenRouter requires --config, --allow-external-transfer and --packet-approval"
            )
        try:
            if args.config:
                config = AnalysisConfig.model_validate_json(args.config.read_text(encoding="utf-8"))
            else:
                config = AnalysisConfig.model_validate(
                    {
                        "auditor_a": {
                            "model": "offline/a",
                            "prompt_price_cap": 1,
                            "completion_price_cap": 1,
                        },
                        "auditor_b": {
                            "model": "offline/b",
                            "prompt_price_cap": 1,
                            "completion_price_cap": 1,
                        },
                        "max_budget_usd": 1,
                    }
                )
        except (ValueError, OSError):
            parser.error("Invalid or unreadable analysis configuration")
        try:
            report = analyze(
                args.dossier,
                args.output,
                config,
                mode=args.mode,
                allow_external_transfer=args.allow_external_transfer,
                packet_approval=args.packet_approval,
            )
        except (ValueError, PermissionError, OSError, ProviderError) as exc:
            # No input contents or credentials in CLI errors.
            parser.error(f"Analysis preflight failed ({type(exc).__name__})")
        print(f"Analysis mode: {report.mode}; independent legal review required")
        if not report.completed:
            parser.exit(1, "One or both auditors did not complete; review analysis.json\n")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "collect":
        from .collector import CrawlOptions, Limits, collect

        parser = argparse.ArgumentParser(
            description="Collect technical facts without legal decisions"
        )
        parser.add_argument("url")
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument("--max-pages", type=int, default=10)
        parser.add_argument("--max-documents", type=int, default=10)
        parser.add_argument("--timeout", type=float, default=10)
        parser.add_argument("--min-delay", type=float, default=2)
        parser.add_argument("--max-requests", type=int, default=64)
        parser.add_argument("--max-duration", type=float, default=300)
        args = parser.parse_args(sys.argv[2:])
        result = collect(
            args.url,
            args.output,
            Limits(
                max_pages=args.max_pages,
                max_documents=args.max_documents,
                timeout_seconds=args.timeout,
            ),
            crawl_options=CrawlOptions(
                min_delay_seconds=args.min_delay,
                max_requests=args.max_requests,
                max_duration_seconds=args.max_duration,
            ),
        )
        import json

        summary = json.loads((args.output / "crawl/summary.json").read_text())
        print(
            f"Recorded {len(result.pages)} page attempts; "
            f"stop: {summary['stop_reason']}; requests sent: {summary['requests_sent']}; "
            "independent review required"
        )
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    legacy_args = sys.argv[2:] if len(sys.argv) > 1 and sys.argv[1] == "demo" else sys.argv[1:]
    args = parser.parse_args(legacy_args)
    print("DEMO/LEGACY: no production legal GO or client release", file=sys.stderr)
    report = build_report(AuditInput.model_validate_json(args.input.read_text(encoding="utf-8")))
    payload = report.model_dump_json(indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
