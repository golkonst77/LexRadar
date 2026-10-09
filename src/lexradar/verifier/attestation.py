"""Independent review submissions are NOT authenticated source attestations in this MVP."""

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field

from ..models import Model
from .models import LegalSource
from .packets import canonical
from .registry import OFFICIAL_HOSTS, digest, load_registry


class SourceBinding(Model):
    source_id: str
    act_number: str
    act_name: str
    domain: str
    provision: str
    official_url: str
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: str
    effective_from: date | None
    effective_until: date | None


class ReviewerConfirmation(Model):
    binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: AwareDatetime
    independent_of_record_author: bool = Field(strict=True)
    official_origin_checked: bool = Field(strict=True)
    revision_and_period_checked: bool = Field(strict=True)
    acquisition_reference: str = Field(min_length=1, max_length=3000)
    rationale: str = Field(min_length=1, max_length=3000)


class AttestationSubmission(Model):
    binding: SourceBinding
    binding_sha256: str
    confirmation: ReviewerConfirmation | None = None
    status: Literal["unverified"] = "unverified"
    trusted: Literal[False] = False
    origin_authentication: Literal["unsupported"] = "unsupported"
    reviewer_authentication: Literal["unsupported"] = "unsupported"
    limitation: str = (
        "Submission records claims only; provenance and reviewer authenticity are unverified"
    )


def source_binding(source: LegalSource) -> SourceBinding:
    return SourceBinding(
        source_id=source.id,
        act_name=source.act_name,
        domain=source.domain,
        act_number=source.act_number,
        provision=source.provision,
        official_url=str(source.source_url),
        text_sha256=source.text_sha256,
        revision=source.revision,
        effective_from=source.effective_from,
        effective_until=source.effective_until,
    )


def submit(
    source: LegalSource, confirmation: ReviewerConfirmation | None = None
) -> AttestationSubmission:
    if (
        source.source_url.scheme != "https"
        or source.source_url.host not in OFFICIAL_HOSTS
        or source.source_url.port != 443
        or source.source_url.username is not None
        or source.source_url.password is not None
    ):
        raise ValueError("Official source URL required for submission")
    if digest(source.norm_text) != source.text_sha256 or not source.norm_text.strip():
        raise ValueError("Exact nonempty norm text and matching hash required")
    if source.effective_from is None:
        raise ValueError("Known revision effective date required for submission")
    binding = source_binding(source)
    binding_hash = digest(canonical(binding.model_dump(mode="json")))
    if confirmation and (
        confirmation.binding_sha256 != binding_hash
        or not confirmation.reviewer.strip()
        or confirmation.reviewed_at > datetime.now(UTC)
        or not confirmation.independent_of_record_author
        or not confirmation.official_origin_checked
        or not confirmation.revision_and_period_checked
        or not confirmation.acquisition_reference.strip()
        or not confirmation.rationale.strip()
    ):
        raise ValueError("Independent confirmation missing, stale or bound to another source")
    # Even a complete submission cannot be promoted without an authenticated trust service.
    return AttestationSubmission(
        binding=binding, binding_sha256=binding_hash, confirmation=confirmation
    )


def attestation_main(argv: list[str]):
    parser = argparse.ArgumentParser(
        description="Prepare independent source review; no trusted status granted"
    )
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--confirmation", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        registry, _ = load_registry(args.registry)
        source = next(s for s in registry.sources if s.id == args.source_id)
        confirmation = None
        if args.confirmation:
            if args.confirmation.is_symlink() or args.confirmation.stat().st_size > 10000:
                raise ValueError("Unsafe confirmation")
            confirmation = ReviewerConfirmation.model_validate_json(
                args.confirmation.read_text(encoding="utf-8")
            )
        submission = submit(source, confirmation)
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "submission.json").write_text(
            submission.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        (args.output / "norm-text.txt").write_text(source.norm_text, encoding="utf-8")
        if confirmation is None:
            template = {
                "binding_sha256": submission.binding_sha256,
                "reviewer": "",
                "reviewed_at": None,
                "independent_of_record_author": False,
                "official_origin_checked": False,
                "revision_and_period_checked": False,
                "acquisition_reference": "",
                "rationale": "",
            }
            (args.output / "confirmation.json").write_text(
                json.dumps(template, indent=2) + "\n", encoding="utf-8"
            )
    except (ValueError, OSError, StopIteration):
        parser.error("Source attestation preparation failed; inspect local inputs")
    print("Source submission unverified: origin/reviewer authentication unsupported")
