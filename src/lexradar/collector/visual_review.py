"""Local, hash-bound self-declared PDF viewing records. No rendering, OCR, auth or trust."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ..auditors.evidence import load_packet
from ..decision.service import json_object, read_bytes, sha256
from ..models import Model


class VisualObservation(Model):
    schema_version: Literal["visual-observation-0.1"] = "visual-observation-0.1"
    evidence_id: str = Field(min_length=1, max_length=200)
    original_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    page: int = Field(ge=1)
    viewed: bool = Field(default=False, strict=True)
    reviewer_id: str = Field(default="", max_length=200)
    viewed_at: AwareDatetime | None = None
    note: str = Field(default="", max_length=3000)
    observations: list[str] = Field(default_factory=list, max_length=50)
    trust_status: Literal["untrusted"] = "untrusted"
    origin_authentication: Literal["unsupported"] = "unsupported"

    @model_validator(mode="after")
    def declared_view(self):
        if self.viewed and (
            not self.reviewer_id.strip()
            or self.viewed_at is None
            or not self.note.strip()
            or not self.observations
            or any(not o.strip() or len(o) > 3000 for o in self.observations)
        ):
            raise ValueError("Claimed view requires reviewer, aware time and concrete observations")
        return self


def reading_inventory(packet):
    return {
        "schema_version": "reading-inventory-0.1",
        "dossier_sha256": packet.manifest_sha256,
        "pages": [
            {"evidence_id": p.evidence_id, "reading": p.reading.model_dump(mode="json")}
            for p in packet.data.pages
        ],
        "documents": [
            {
                "evidence_id": d.evidence_id,
                "source_url": str(
                    next(e.source for e in packet.data.evidence if e.id == d.evidence_id)
                ),
                "original_artifact": next(
                    (
                        a.path
                        for e in packet.data.evidence
                        if e.id == d.evidence_id
                        for a in e.artifacts
                        if a.kind == "pdf"
                    ),
                    None,
                ),
                "reading": d.reading.model_dump(mode="json"),
                "page_texts": {str(k): v for k, v in d.page_texts.items()},
            }
            for d in packet.data.documents
        ],
        "legal_research_completed": False,
    }


def write_inventory(
    root: Path,
    output: Path,
    *,
    evidence_id: str | None = None,
    page: int | None = None,
    submission: Path | None = None,
) -> dict:
    """New sidecar artifacts; original manifest/PDF files are never rewritten."""
    if output.exists() or output.resolve().is_relative_to(root.resolve()):
        raise ValueError("Use a new output directory outside the original dossier")
    if any(p.is_symlink() for p in [output, *output.parents]):
        raise ValueError("Symlink output blocked")
    packet = load_packet(root, 2_000_000)
    documents = {d.evidence_id: d for d in packet.data.documents}
    observation = None
    submission_raw = None
    if submission:
        submission_raw = read_bytes(submission, 200_000)
        json_object(submission_raw)
        observation = VisualObservation.model_validate_json(submission_raw)
        evidence_id, page = observation.evidence_id, observation.page
    elif evidence_id is not None:
        document = documents.get(evidence_id)
        if document is None or document.reading.source_sha256 is None or page is None:
            raise ValueError("Existing original PDF and page required")
        if document.reading.page_count is None or not 1 <= page <= document.reading.page_count:
            raise ValueError("Page unavailable or outside PDF")
        observation = VisualObservation(
            evidence_id=evidence_id, original_sha256=document.reading.source_sha256, page=page
        )
    state = "not_submitted"
    if observation:
        document = documents.get(observation.evidence_id)
        valid = document is not None and (
            document.reading.source_sha256 == observation.original_sha256
            and document.reading.page_count is not None
            and 1 <= observation.page <= document.reading.page_count
            and (observation.viewed_at is None or observation.viewed_at <= datetime.now(UTC))
        )
        state = (
            "invalidated"
            if not valid
            else "declared_untrusted"
            if observation.viewed
            else "pending"
        )
    if load_packet(root, 2_000_000).manifest_sha256 != packet.manifest_sha256:
        raise ValueError("Dossier changed while preparing sidecar")
    inventory = reading_inventory(packet)
    inventory_raw = json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2).encode()
    observations_raw = observation.model_dump_json(indent=2).encode() if observation else b"null"
    receipt = {
        "dossier_sha256": packet.manifest_sha256,
        "state": state,
        "trust_status": "untrusted",
        "origin_authentication": "unsupported",
        "inventory_sha256": sha256(inventory_raw),
        "observations_sha256": sha256(observations_raw),
        "submission_sha256": sha256(submission_raw) if submission_raw is not None else None,
        "legal_research_completed": False,
        "production_go_allowed": False,
        "client_release_allowed": False,
        "automatic_send_allowed": False,
        "limitation": "Self-declared viewing; hashes are not authentication or expertise",
    }
    output.mkdir(parents=True, exist_ok=False)
    (output / "inventory.json").write_bytes(inventory_raw)
    (output / "observations.json").write_bytes(observations_raw)
    if submission_raw is not None:
        (output / "submission.json").write_bytes(submission_raw)
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def verify_sidecar(root: Path, output: Path) -> dict:
    """Check bytes and reproduce the technical inventory; no review authentication."""
    receipt = json_object(read_bytes(output / "receipt.json", 20_000))
    inventory_raw = read_bytes(output / "inventory.json", 6_000_000)
    observations_raw = read_bytes(output / "observations.json", 200_000)
    if sha256(inventory_raw) != receipt.get("inventory_sha256") or sha256(
        observations_raw
    ) != receipt.get("observations_sha256"):
        raise ValueError("Sidecar artifact hash mismatch")
    if (
        receipt.get("trust_status") != "untrusted"
        or receipt.get("origin_authentication") != "unsupported"
    ):
        raise ValueError("Sidecar cannot self-authenticate")
    if any(
        receipt.get(key) is not False
        for key in (
            "legal_research_completed",
            "production_go_allowed",
            "client_release_allowed",
            "automatic_send_allowed",
        )
    ):
        raise ValueError("Sidecar cannot grant legal/client approval")
    packet = load_packet(root, 2_000_000)
    if receipt.get("dossier_sha256") != packet.manifest_sha256 or json_object(
        inventory_raw
    ) != reading_inventory(packet):
        raise ValueError("Stale or non-reproducible reading inventory")
    if observations_raw != b"null":
        observation = VisualObservation.model_validate_json(observations_raw)
        if receipt.get("submission_sha256") is not None:
            raw_submission = read_bytes(output / "submission.json", 200_000)
            if sha256(raw_submission) != receipt["submission_sha256"]:
                raise ValueError("Original submission hash mismatch")
            json_object(raw_submission)
            if VisualObservation.model_validate_json(raw_submission) != observation:
                raise ValueError("Observation does not match saved submission")
    return receipt


def visual_main(command: str, argv: list[str]):
    parser = argparse.ArgumentParser(
        description="Local reading inventory / untrusted visual journal"
    )
    parser.add_argument("dossier", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    if command == "record-visual-review":
        parser.add_argument("--submission", type=Path, required=True)
    else:
        parser.add_argument("--evidence")
        parser.add_argument("--page", type=int)
    args = parser.parse_args(argv)
    try:
        receipt = write_inventory(
            args.dossier,
            args.output,
            **(
                {"submission": args.submission}
                if command == "record-visual-review"
                else {"evidence_id": args.evidence, "page": args.page}
            ),
        )
    except (ValueError, OSError):
        parser.error("Reading inventory failed; no legal or client approval granted")
    print(f"Local journal: {receipt['state']}; untrusted; no legal/client approval")
