import pytest

from lexradar.models import AuditInput


@pytest.fixture
def data():
    finding = {
        "id": "f1",
        "source": "https://clinic.example/form",
        "evidence_ids": ["e1"],
        "fact_description": "Synthetic observed fact",
        "legal_basis": "Synthetic legal applicability reviewed by human",
        "legal_interpretation": "Synthetic material issue, not a real legal conclusion",
        "confidence": 0.95,
        "status": "confirmed",
        "material": True,
    }
    return AuditInput.model_validate(
        {
            "target": {
                "id": "demo",
                "url": "https://clinic.example",
                "legal_entity": {"name": "Synthetic clinic", "identity_verified": True},
            },
            "evidence": [
                {
                    "id": "e1",
                    "source": "https://clinic.example/form",
                    "captured_at": "2026-01-01T00:00:00Z",
                    "observed_fact": "Synthetic observation",
                }
            ],
            "auditors": [
                {"auditor": label, "completed": True, "findings": [finding]} for label in ("A", "B")
            ],
            "verification": {
                "completed": True,
                "findings": [
                    {
                        "finding_id": "f1",
                        "status": "confirmed",
                        "evidence_checked": True,
                        "legal_basis_checked": True,
                        "reviewer_kind": "human",
                        "reviewer_id": "demo-reviewer",
                        "reviewed_at": "2026-01-01T01:00:00Z",
                        "rationale": "Synthetic review only",
                    }
                ],
            },
        }
    )
