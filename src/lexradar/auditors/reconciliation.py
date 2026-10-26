"""Explicit accounting reconciliation; never implies a usable model response.

No caller in the paid pipeline invokes this operation automatically.
The audit and charge update share one atomic ledger replacement.
"""
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal


def propose(budget, token, metadata_bytes, *, generation_id, request_id, reviewer):
    if not reviewer.strip():
        raise ValueError("Named reviewer required")
    data = json.loads(metadata_bytes, parse_float=Decimal)["data"]
    if (data["id"] != generation_id or data["request_id"] != request_id
            or data["model"] != "deepseek/deepseek-v3.2"
            or data["provider_name"] != "SiliconFlow"):
        raise ValueError("Generation identity mismatch")
    cost = Decimal(str(data["total_cost"]))
    if not cost.is_finite() or cost < 0:
        raise ValueError("Invalid cost")
    raw = budget.path.read_bytes()
    ledger = budget.validate(json.loads(raw))
    entry = next(e for e in ledger[budget.site]["entries"] if e["id"] == token)
    if entry["state"] != "unknown":
        raise ValueError("Only unknown operations may be reconciled")
    return {"ledger_sha256": hashlib.sha256(raw).hexdigest(),
            "site": budget.site, "token": token, "before": dict(entry),
            "charge": str(cost), "state": "overrun" if cost > Decimal(entry["charge"]) else "known",
            "generation_id": generation_id, "request_id": request_id,
            "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
            "reviewer": reviewer, "at": datetime.now(UTC).isoformat(),
            "result_usable": False, "finish_reason": data.get("finish_reason")}


def apply_reviewed(budget, proposal):
    """Explicit administrative call only, after review of the read-only evidence.

    Persist original reservation and evidence hashes inside the original entry;
    stale/replayed proposals fail closed. This is not a second ledger.
    """
    with budget.locked() as ledger:
        if (proposal["site"] != budget.site or hashlib.sha256(budget.path.read_bytes()).hexdigest()
                != proposal["ledger_sha256"]):
            raise ValueError("Stale reconciliation proposal")
        entry = next(e for e in ledger[budget.site]["entries"] if e["id"] == proposal["token"])
        cost = Decimal(proposal["charge"])
        expected = "overrun" if cost > Decimal(entry["charge"]) else "known"
        if (entry != proposal["before"] or entry["state"] != "unknown"
                or not cost.is_finite() or cost < 0 or proposal["state"] != expected):
            raise ValueError("Invalid reconciliation proposal")
        entry.update(charge=str(cost), state=expected, reconciliation=proposal)
        budget.write(ledger)
