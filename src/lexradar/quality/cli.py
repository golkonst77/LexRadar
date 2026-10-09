"""Free local benchmarks and an explicitly enabled collection-only public pilot."""

import argparse
from pathlib import Path

from ..collector.network import CollectionError
from .pilot import PilotConfig, pilot
from .report import write_html
from .runner import benchmark


def quality_main(command, argv):
    parser = argparse.ArgumentParser(description=__doc__)
    if command == "benchmark":
        parser.add_argument("--suite", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument("--mode", choices=("synthetic", "replay"), default="synthetic")
    elif command == "quality-report":
        parser.add_argument("root", type=Path)
        parser.add_argument("--format", choices=("html",), default="html")
        parser.add_argument("--expert-labels", type=Path)
    else:
        parser.add_argument("--config", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument("--allow-public-collection", action="store_true")
    args = parser.parse_args(argv)
    try:
        if command == "benchmark":
            report = benchmark(args.suite, args.output, args.mode)
            print(f"Scenarios: {len(report.cases)}; quality status: {report.overall_status}")
            if report.overall_status == "failed":
                parser.exit(1, "Quality gates failed; inspect test-report.json\n")
        elif command == "quality-report":
            print(write_html(args.root, args.expert_labels))
        else:
            if args.config.is_symlink() or args.config.stat().st_size > 100_000:
                raise ValueError("Unsafe pilot config")
            config = PilotConfig.model_validate_json(args.config.read_text())
            pilot(config, args.output, allow_public_collection=args.allow_public_collection)
            print("Collection complete; independent review required; no external LLM transfer")
    except (ValueError, OSError, PermissionError, CollectionError) as exc:
        parser.error(f"Stopped safely ({type(exc).__name__}); inspect local inputs and run journal")
