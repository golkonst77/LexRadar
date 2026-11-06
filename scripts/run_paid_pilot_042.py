#!/usr/bin/env python3
"""Second pilot preparation. Default offline; --metadata performs one read-only GET.

Paid execution requires a separately supplied second-pilot approval; none is created here.
Do not delete the first marker or alter its approval to authorize a second run.
"""
import argparse
import fcntl
import hashlib
import json
import os
import socket
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_paid_pilot_039 as first  # noqa: E402

from lexradar.auditors.reconciliation import propose  # noqa: E402
from lexradar.auditors.site_budget import SiteBudget  # noqa: E402

TOKEN = "c4574335d0f641d491b81688148e1327"
GENERATION = "gen-1792068046-9IdadKFjSEw1XxRfwum4"
REQUEST = "req-1792068046-ZfHK9ZmipMZUXgFQfTtc"
LEGACY_WORK = first.REPO / "work/second-pilot"
WORK = first.WORK / "second-pilot"


class LocalFailure(Exception):
    """Only fixed operation labels and local paths, never exception bodies."""


@contextmanager
def operation(stage, path=None):
    try:
        yield
    except LocalFailure:
        raise
    except Exception as exc:
        detail = f"stage={stage}; type={type(exc).__name__}"
        if isinstance(exc, OSError):
            detail += f"; errno={exc.errno}"
        if path is not None:
            detail += f"; path={path}"
        raise LocalFailure(detail) from None


def metadata_preflight():
    # Fixed location, never a permission-dependent fallback. Preserve legacy guards.
    with operation("metadata-paths", WORK):
        first.require(not any(p.is_symlink() for root in (WORK, LEGACY_WORK)
                              for p in (root, *root.parents)), "Symlink refused")
        first.require(not LEGACY_WORK.exists(), "Legacy work requires manual inspection")
        WORK.mkdir(mode=0o700, parents=False, exist_ok=True)
        for name in ("metadata-042-attempt.json", "metadata-042.json", "attempt-042.json"):
            path = WORK / name
            if path.exists() or path.is_symlink():
                raise LocalFailure(f"stage=existing-evidence; path={path}; do not retry")
    with operation("metadata-write-probe", WORK):
        with tempfile.TemporaryFile(dir=WORK):
            pass
    with operation("metadata-key-read", first.KEY):
        key = first.KEY.read_text().strip()
        first.require(bool(key) and not any(c.isspace() for c in key), "Invalid key")
    return key



class SecondOnceTransport(first.OnceTransport):
    """Prepared transport for a future explicitly authorized runner, not this CLI."""
    def post(self, payload, key, timeout, max_bytes):
        body = json.loads(payload)
        first.require(body.get("stream") is False, "Non-streaming required")
        # Parent checks exact model, route, prices, output and JSON constraints.
        # Use a distinct permanent marker, while retaining the same site ledger.
        with patch.object(first, "ATTEMPT", WORK / "attempt-042.json"):
            return super().post(payload, key, timeout, max_bytes)


def prepare():
    first.safe_paths()
    config = first.AnalysisConfig.model_validate_json(
        (first.REPO / "examples/compact-openrouter-config.json").read_text())
    first.require(config.timeout_seconds == 30 and config.retries == 0
                  and config.max_budget_usd == first.Decimal("0.05")
                  and config.single_auditor and config.auditor_a.max_output_tokens == 2000,
                  "Configuration changed")
    before = first.LEDGER.read_bytes()
    budget = SiteBudget(first.LEDGER, "clinic.example")
    ledger = budget.validate(json.loads(before))
    entries = ledger[budget.site]["entries"]
    first.require(len(entries) == 1 and entries[0]["id"] == TOKEN, "Unexpected history")
    # Run the real pipeline with the real ledger. Unknown must remain a blocker.
    with (tempfile.TemporaryDirectory(prefix="lexradar-042-") as tmp,
          patch.object(socket.socket, "connect", first.forbidden),
          patch.object(socket, "create_connection", first.forbidden),
          patch.object(first.OpenRouterTransport, "post", first.forbidden)):
        data = first.CollectionResult.model_validate_json(
            (first.ROOT / "collection.json").read_text())
        first.require(first.verify_integrity(data, first.ROOT) == [], "Integrity failed")
        packet = first.prepare(first.ROOT, as_of=first.AS_OF)
        first.verify_quotes(first.ROOT, packet)
        first.require(packet.sha256 == first.SHA, "Packet changed")
        report = first.analyze_compact(
            first.ROOT, Path(tmp) / "dry", config, as_of=first.AS_OF, mode="openrouter",
            allow_external_transfer=True, packet_approval=first.APPROVAL,
            ledger=first.LEDGER, dry_run=True, approval_template=False)
    first.require(first.LEDGER.read_bytes() == before and not report["request_sent"],
                  "Read-only invariant failed")
    held = sum((first.Decimal(e["charge"]) for e in entries), first.Decimal(0))
    return {"status": "prepared_blocked", "dry_run_status": report["status"],
            "blockers": report["blockers"], "ledger_sha256": hashlib.sha256(before).hexdigest(),
            "held_usd": str(held), "second_reserve_usd": "0.009756687",
            "combined_held_usd": str(held + first.Decimal("0.009756687")),
            "limit_usd": "0.05", "new_user_authorization_required": True,
            "paid_execution_enabled": False, "request_sent": False}


def execute(approval):
    first.require(approval is not None, "Separate second-pilot authorization required")
    with operation("legacy-work-check", LEGACY_WORK):
        first.require(not LEGACY_WORK.exists() and not LEGACY_WORK.is_symlink(),
                      "Legacy work requires manual inspection")
    approval = approval.resolve()
    first.require(approval != first.APPROVAL.resolve(),
                  "First approval cannot authorize second pilot")
    auth = json.loads(approval.read_text())
    first.require(auth.get("pilot_id") == "042" and auth.get("approved") is True
                  and auth.get("contents_reviewed") is True
                  and auth.get("packet_sha256") == first.SHA
                  and auth.get("site_limit_usd") == "0.05"
                  and auth.get("previous_generation_id") == GENERATION,
                  "Explicit second-pilot approval required")
    first.require(not any(p.is_symlink() for p in (WORK, *WORK.parents)), "Symlink refused")
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "run-042.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        first.require(not (WORK / "attempt-042.json").exists(), "Second attempt already claimed")
        prepare()
        budget = SiteBudget(first.LEDGER, "clinic.example")
        entry = budget.validate(json.loads(first.LEDGER.read_bytes()))[budget.site]["entries"][0]
        first.require(entry["state"] == "known"
                      and entry["charge"] == "0.002864425"
                      and entry.get("reconciliation", {}).get("generation_id") == GENERATION,
                      "First cost must be explicitly reconciled, with audit")
        config = first.AnalysisConfig.model_validate_json(
            (first.REPO / "examples/compact-openrouter-config.json").read_text())
        output = Path(tempfile.mkdtemp(prefix="run-042-", dir=WORK))

        def dry(name):
            with (patch.object(socket.socket, "connect", first.forbidden),
                  patch.object(socket, "create_connection", first.forbidden),
                  patch.object(first.OpenRouterTransport, "post", first.forbidden)):
                report = first.analyze_compact(
                    first.ROOT, output / name, config, as_of=first.AS_OF, mode="openrouter",
                    allow_external_transfer=True, packet_approval=approval,
                    ledger=first.LEDGER, dry_run=True, approval_template=False)
            first.require(report["status"] == "dry_run_ready" and not report["blockers"]
                          and not report["request_sent"], "Dry-run blocked")
            return first.number(report["preview"]["reserve_usd"])

        reserve = dry("dry")
        key = first.KEY.read_text().strip()
        first.require(bool(key) and not any(c.isspace() for c in key), "Invalid key")
        first.check_remote(config, reserve, key)
        metadata = first.get_json("generation?id=" + GENERATION, key)
        first.require(metadata["id"] == GENERATION and metadata["request_id"] == REQUEST
                      and first.number(metadata["total_cost"]) == first.Decimal(entry["charge"]),
                      "Fresh first-generation metadata mismatch")
        dry("final-dry")
        previous = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = key
        try:
            with patch("lexradar.auditors.provider.OpenRouterTransport", SecondOnceTransport):
                result = first.analyze_compact(
                    first.ROOT, output / "paid", config, as_of=first.AS_OF, mode="openrouter",
                    allow_external_transfer=True, packet_approval=approval,
                    ledger=first.LEDGER, approval_template=False)
        finally:
            if previous is None:
                os.environ.pop("OPENROUTER_API_KEY", None)
            else:
                os.environ["OPENROUTER_API_KEY"] = previous
        print(result["status"], "; HOLD; results:", output)
        first.require(result["status"] == "success", "Possible charge; keep marker and reconcile")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", action="store_true",
                        help="one GET; never POST or ledger write")
    parser.add_argument("--check-metadata", action="store_true",
                        help="local metadata preflight only; no GET or claim")
    parser.add_argument("--execute", action="store_true", help="requires separate user approval")
    parser.add_argument("--approval", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    first.require(sum((args.execute, args.metadata, args.check_metadata)) <= 1,
                  "Choose one mode")
    if args.execute:
        execute(args.approval)
        return
    with operation("prepare-local"):
        result = prepare()
    if args.metadata or args.check_metadata:
        key = metadata_preflight()
        if args.check_metadata:
            result["metadata_preflight"] = "ready; no GET or claim"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
    if args.metadata:
        # O_EXCL makes this evidence capture one-shot; failure is retained too.
        with operation("metadata-claim", WORK / "metadata-042-attempt.json"):
            first.durable_claim(WORK / "metadata-042-attempt.json")
        # Open evidence before GET: unwritable/existing output must stop locally.
        with operation("metadata-output-create", WORK / "metadata-042.json"):
            stream = (WORK / "metadata-042.json").open("x")
        try:
            metadata = first.get_json("generation?id=" + GENERATION, key)
            raw = json.dumps({"data": metadata}).encode()
            proposal = propose(SiteBudget(first.LEDGER, "clinic.example"), TOKEN, raw,
                               generation_id=GENERATION, request_id=REQUEST,
                               reviewer="UNREVIEWED: read-only capture, do not apply")
            evidence = {"metadata": metadata, "proposal_unreviewed": proposal}
        except Exception as exc:
            evidence = {"error_type": type(exc).__name__, "metadata_verified": False,
                        "stage": "metadata-get-or-proposal"}
            if isinstance(exc, OSError):
                evidence["errno"] = exc.errno
            reason = getattr(exc, "reason", None)
            if isinstance(reason, OSError):
                evidence["reason_type"] = type(reason).__name__
                evidence["reason_errno"] = reason.errno
        with stream, operation("metadata-output-write", WORK / "metadata-042.json"):
            json.dump(evidence, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        result["metadata_verified"] = "metadata" in evidence
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        detail = str(exc) if isinstance(exc, LocalFailure) else f"type={type(exc).__name__}"
        print(f"STOP ({detail}); no automatic retry", file=sys.stderr)
        sys.exit(1)
