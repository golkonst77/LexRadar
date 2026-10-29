"""Offline source preparation and independent-review journal; no acquisition or promotion."""

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path

from .legal_cards import (
    LegalSourceCard,
    SourceLegalReview,
    assess_card,
    card_hash,
    check_review,
    hash_bytes,
    read_local,
)
from .models import SourceRegistry
from .registry import assess_registry, load_registry


def read_object(path: Path) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(read_local(path), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_card(metadata: Path, text_file: Path, output: Path, event: date) -> LegalSourceCard:
    value = read_object(metadata)
    raw = read_local(text_file)
    raw.decode("utf-8")
    expected = hash_bytes(raw)
    if "text_sha256" in value and value["text_sha256"] != expected:
        raise ValueError("Supplied original hash mismatch")
    if "original_path" in value and value["original_path"] != "original.txt":
        raise ValueError("Prepared original path must be original.txt")
    value.update(text_sha256=expected, original_path="original.txt")
    card = LegalSourceCard.model_validate(value)
    if not card.norm_text.strip() or card.norm_text not in raw.decode("utf-8"):
        raise ValueError("Exact quote missing from imported text")
    if any(p.is_symlink() for p in (output.absolute(), *output.absolute().parents)):
        raise ValueError("Unsafe output")
    output.mkdir(parents=True, exist_ok=False)
    (output / "original.txt").write_bytes(raw)
    write_json(output / "card.json", card.model_dump(mode="json"))
    registry = SourceRegistry(cards=[card])
    write_json(output / "registry.json", registry.model_dump(mode="json"))
    assessment = assess_card(card, output, event, datetime.now(UTC))
    write_json(output / "report.json", assessment.model_dump(mode="json"))
    write_json(
        output / "review.json",
        {
            "schema_version": "source-legal-review-0.1",
            "card_sha256": card_hash(card),
            "original_sha256": card.text_sha256,
            "event_date": event.isoformat(),
            "reviewer": "",
            "reviewed_at": None,
            "independent_of_author": False,
            "origin_checked": False,
            "revision_checked": False,
            "legal_force_checked": False,
            "operator_reference": "",
            "activity": "",
            "applicability_rationale": "",
            "confirmation_references": [],
            "trust_status": "untrusted",
            "authenticated": False,
        },
    )
    return card


def legal_source_main(argv: list[str]):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--metadata", type=Path, required=True)
    prepare.add_argument("--text", type=Path, required=True)
    check = sub.add_parser("check")
    check.add_argument("--registry", type=Path, required=True)
    review = sub.add_parser("review")
    review.add_argument("--registry", type=Path, required=True)
    review.add_argument("--source-id", required=True)
    review.add_argument("--submission", type=Path, required=True)
    for command in (prepare, check, review):
        command.add_argument("--event-date", type=date.fromisoformat, required=True)
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "prepare":
            prepare_card(args.metadata, args.text, args.output, args.event_date)
        else:
            registry, registry_hash = load_registry(args.registry)
            assessments = assess_registry(registry, args.event_date, datetime.now(UTC), 30)
            payload = {
                "registry_sha256": registry_hash,
                "event_date": args.event_date.isoformat(),
                "status": "unverified",
                "production_go_allowed": False,
                "client_release_allowed": False,
                "legal_research_completed": False,
                "assessments": [a.model_dump(mode="json") for a in assessments.values()],
            }
            if args.operation == "review":
                card = next(c for c in registry.cards if c.id == args.source_id)
                submission_raw = read_local(args.submission)
                submission = SourceLegalReview.model_validate_json(submission_raw)
                payload["review_status"] = check_review(
                    submission, card, assessments[card.id].card_assessment, datetime.now(UTC)
                )
                payload["review_submission_sha256"] = hash_bytes(submission_raw)
                payload["review_authentication"] = "unsupported"
            if any(
                p.is_symlink() for p in (args.output.absolute(), *args.output.absolute().parents)
            ):
                raise ValueError("Unsafe output")
            args.output.mkdir(parents=True, exist_ok=False)
            write_json(args.output / "report.json", payload)
    except (OSError, ValueError, StopIteration):
        parser.error("Local source workflow failed; inspect inputs locally (no trusted status)")
    print("Local source checks completed; legal source/reviewer remain unverified/untrusted")
