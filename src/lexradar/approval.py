"""Mandatory human review record. Sending remains unavailable even after approval."""

from .models import AuditReport


def send_client_message(report: AuditReport) -> None:
    if not report.human_approval.approved:
        raise PermissionError("Human approval is required")
    raise NotImplementedError("Client message delivery is disabled in MVP v0.1")
