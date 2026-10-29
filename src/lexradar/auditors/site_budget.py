"""Durable per-site reservations. Pending/unknown costs forbid further spending."""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .budget import Budget, BudgetExceeded


class SiteBudget:
    def __init__(self, path: Path, site: str, limit=Decimal("0.05")):
        self.path, self.site, self.limit = path.resolve(), site, Decimal(limit)
        if not self.limit.is_finite() or not 0 < self.limit <= Decimal("0.50"):
            raise ValueError("Site limit must be positive and at most 0.50 USD")

    def validate(self, data):
        # Corruption must never create a refund or erase an unresolved reservation.
        for record in data.values():
            stored_limit = Decimal(record["limit"])
            if not stored_limit.is_finite() or not 0 < stored_limit <= Decimal("0.50"):
                raise ValueError("Invalid stored budget limit")
            ids = set()
            for entry in record["entries"]:
                charge = Decimal(entry["charge"])
                if (
                    not charge.is_finite()
                    or charge < 0
                    or entry["state"] not in {"pending", "known", "unknown", "overrun"}
                    or entry["id"] in ids
                ):
                    raise ValueError("Invalid stored reservation")
                ids.add(entry["id"])
        return data

    def preview(self, settings, messages):
        estimate = Budget(self.limit).reserve(settings, messages)
        data = self.validate(json.loads(self.path.read_text()) if self.path.exists() else {})
        site = data.get(self.site, {"limit": str(self.limit), "entries": []})
        entries = site["entries"]
        spent = sum((Decimal(e["charge"]) for e in entries), Decimal(0))
        if any(e["state"] != "known" for e in entries) or spent + estimate > min(
            self.limit, Decimal(site["limit"])
        ):
            raise BudgetExceeded("Site budget exhausted or unresolved expense")
        return estimate

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self.validate(json.loads(self.path.read_text()) if self.path.exists() else {})
            yield data

    def write(self, data):
        fd, name = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def reserve(self, settings, messages):
        estimate = Budget(self.limit).reserve(settings, messages)
        with self.locked() as data:
            site = data.setdefault(self.site, {"limit": str(self.limit), "entries": []})
            limit = min(self.limit, Decimal(site["limit"]))
            entries = site["entries"]
            spent = sum((Decimal(e["charge"]) for e in entries), Decimal(0))
            if any(e["state"] != "known" for e in entries) or spent + estimate > limit:
                raise BudgetExceeded("Site budget exhausted or unresolved expense")
            token = uuid4().hex
            entries.append({"id": token, "charge": str(estimate), "state": "pending"})
            site["limit"] = str(limit)
            self.write(data)
            return token

    def settle(self, token, usage):
        with self.locked() as data:
            entry = next(e for e in data[self.site]["entries"] if e["id"] == token)
            if entry["state"] != "pending":
                raise ValueError("Reservation already settled")
            reserved = Decimal(entry["charge"])
            entry["state"] = "unknown" if usage.cost_usd is None else "known"
            if usage.cost_usd is not None:
                entry["charge"] = str(usage.cost_usd)
                if usage.cost_usd > reserved:
                    entry["state"] = "overrun"
            self.write(data)
            return Decimal(entry["charge"]), entry["state"]
