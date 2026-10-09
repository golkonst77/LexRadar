"""Explicitly authorized, bounded collection only; never automatic external LLM transfer."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import AwareDatetime, Field, HttpUrl

from ..collector import Limits, collect
from ..models import Model
from ..verifier.packets import canonical
from ..verifier.registry import digest


class TargetApproval(Model):
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer: str = Field(min_length=1, max_length=200)
    reviewed_at: AwareDatetime
    public_collection_approved: bool = Field(strict=True)


class PilotConfig(Model):
    enabled: bool = Field(default=False, strict=True)
    target_url: HttpUrl
    limits: Limits = Field(default_factory=lambda: Limits(max_pages=5, max_documents=3))
    max_api_budget_usd: Decimal = Field(default=Decimal("1"), gt=0, le=1, allow_inf_nan=False)
    approval: TargetApproval | None = None

    def binding_sha256(self):
        return digest(canonical(self.model_dump(mode="json", exclude={"approval"})))


def pilot(config: PilotConfig, output: Path, *, allow_public_collection=False):
    if output.exists():
        raise ValueError("New pilot output directory required")
    output.mkdir(parents=True, exist_ok=False)
    journal = output / "runs.jsonl"

    def event(status):
        with journal.open("a", encoding="utf-8") as file:
            file.write(
                canonical(
                    {
                        "time": datetime.now(UTC).isoformat(),
                        "status": status,
                        "config_sha256": config.binding_sha256(),
                        "api_cost_usd": "0",
                        "external_llm_transfer": False,
                    }
                )
                + "\n"
            )

    event("requested")
    approval = config.approval
    if (
        not config.enabled
        or not allow_public_collection
        or approval is None
        or not approval.public_collection_approved
        or not approval.reviewer.strip()
        or approval.reviewed_at > datetime.now(UTC)
        or approval.config_sha256 != config.binding_sha256()
    ):
        event("permission_denied")
        raise PermissionError(
            "Pilot requires enabled config, explicit permission and exact hash approval"
        )
    # More restrictive than Collector's general limits: a single small pilot only.
    if config.limits.max_pages > 10 or config.limits.max_documents > 10:
        event("limits_rejected")
        raise ValueError("Pilot permits at most 10 pages and 10 documents")
    (output / "config.json").write_text(config.model_dump_json(indent=2) + "\n")
    event("collection_started")
    try:
        result = collect(str(config.target_url), output / "dossier", config.limits)
    except Exception:
        event("collection_failed")
        raise
    event("collection_finished_requires_review")
    (output / "next-steps.txt").write_text(
        "Collection only. No LLM transfer, legal conclusion, GO or messages.\n"
        "Inspect dossier locally. Each A/B, Stage I and Stage II packet requires a separate\n"
        "sensitive-data preflight and human approval of its exact SHA-256\n"
        "before any real request.\n"
        "Use existing v0.3/v0.4 commands manually; their configured budget must not exceed\n"
        + str(config.max_api_budget_usd)
        + " USD. This limit is not a provider account cap.\n"
        "Target approval and expert identity are unauthenticated; no cryptographic assurance.\n"
    )
    return result
