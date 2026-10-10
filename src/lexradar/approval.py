"""Mandatory human review record. Sending remains unavailable even after approval."""

from .decision.models import ProductionDecision
from .models import AuditReport


def send_client_message(report: AuditReport | ProductionDecision) -> None:
    if isinstance(report, ProductionDecision):
        raise PermissionError("Production client release and sending are unavailable")
    if not report.human_approval.approved:
        raise PermissionError("Human approval is required")
    raise NotImplementedError("Client message delivery is disabled in MVP v0.1")
