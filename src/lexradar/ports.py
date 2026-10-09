"""Future adapters must implement these interfaces; no browser or LLM calls yet."""

from typing import Protocol

from .models import AuditorResult, AuditTarget, Evidence


class Collector(Protocol):
    def collect(self, target: AuditTarget) -> list[Evidence]: ...


class Auditor(Protocol):
    def audit(self, target: AuditTarget, evidence: tuple[Evidence, ...]) -> AuditorResult: ...
