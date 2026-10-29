"""Declared acquisition metadata is not authentication or legal confirmation."""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ..models import Model


class MaterialOrigin(Model):
    kind: Literal["unknown", "collector", "manual_html_import"] = "unknown"
    acquisition_method: str | None = None
    declared_source: str | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    original_capture_at: AwareDatetime | None = None
    reported_capture_date: str | None = None
    imported_at: AwareDatetime | None = None
    captured_at_role: Literal["unknown", "collection_observation", "local_import"] = "unknown"
    started_at_role: Literal["unknown", "collector_run", "local_import"] = "unknown"
    http_response_at: AwareDatetime | None = None
    origin_authenticated: Literal[False] = False
    limitations: list[str] = Field(
        default_factory=lambda: ["Acquisition origin/timestamp roles unknown; not authenticated"]
    )

    @model_validator(mode="after")
    def manual_time_roles(self):
        if self.kind == "manual_html_import" and (
            self.captured_at_role != "local_import"
            or self.started_at_role != "local_import"
            or self.imported_at is None
            or self.http_response_at is not None
        ):
            raise ValueError("Manual HTML requires local-import roles, not HTTP response time")
        return self
