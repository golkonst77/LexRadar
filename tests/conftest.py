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


class FakeCrawlClock:
    """Only the new pacing clock is virtual; real socket/DNS/browser timeouts remain real."""

    def __init__(self):
        self.value = 0
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


@pytest.fixture(autouse=True)
def fast_synthetic_crawl_clock(monkeypatch):
    # All pytest collection is synthetic. This does not change CLI/public clock behavior.
    monkeypatch.setattr("lexradar.collector.crawl.Clock", FakeCrawlClock)
