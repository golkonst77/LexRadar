#!/usr/bin/env python3
"""One authorized pilot. Default is offline dry-run; --execute permits one POST.

Never remove attempt-039.json or reset site-ledger.json to retry an uncertain call.
Run with the project's Python environment (pydantic required).
"""
import argparse
import fcntl
import json
import os
import socket
import sys
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, build_opener

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from lexradar.auditors.compact_pipeline import analyze_compact  # noqa: E402
from lexradar.auditors.compact_profile import MODEL, PROVIDER  # noqa: E402
from lexradar.auditors.models import AnalysisConfig  # noqa: E402
from lexradar.auditors.provider import NoRedirects, OpenRouterTransport  # noqa: E402
from lexradar.collector.artifacts import verify_integrity  # noqa: E402
from lexradar.collector.models import CollectionResult  # noqa: E402
from lexradar.primary.compact import prepare, verify_quotes  # noqa: E402

WORK = REPO / "work/first-pilot"
ROOT = REPO / "examples/compact-first-pilot/dossier"
APPROVAL = WORK / "approval-038.json"
LEDGER = WORK / "site-ledger.json"
ATTEMPT = WORK / "attempt-039.json"
KEY = Path("/home/golkonst/.config/lexradar/openrouter.key")
SHA = "7b15886086ccf838a14a9a974ebee722f732fa7629478e14289a745392292acd"
AS_OF = datetime.fromisoformat("2026-10-10T00:00:00+00:00")
API = "https://openrouter.ai/api/v1/"


def require(ok, message):
    if not ok:
        raise PermissionError(message)


def forbidden(*args, **kwargs):
    raise PermissionError("Network disabled during local dry-run")


def safe_paths():
    for path in (WORK, ROOT, APPROVAL, LEDGER, ATTEMPT, WORK / "run-039.lock"):
        require(not any(p.is_symlink() for p in (path, *path.parents)), "Symlink refused")


def durable_claim(path):
    # O_EXCL, fsync(file), fsync(directory) all precede the first network POST.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"state": "attempted_or_unknown_do_not_retry", "sha256": SHA,
                   "at": datetime.now(UTC).isoformat()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class OnceTransport(OpenRouterTransport):
    def post(self, payload, key, timeout, max_bytes):
        body = json.loads(payload)
        require(body["model"] == MODEL and body["max_tokens"] == 2000, "Request changed")
        require(body["provider"] == {
            "only": [PROVIDER], "allow_fallbacks": False, "require_parameters": True,
            "max_price": {"prompt": 0.259, "completion": 0.8}}, "Route changed")
        require(body["reasoning"] == {"enabled": False}, "Reasoning changed")
        require(body["response_format"] == {"type": "json_object"}, "Format changed")
        durable_claim(ATTEMPT)
        return super().post(payload, key, timeout, max_bytes)


def get_json(suffix, key=None):
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # No redirects, retries, alternate DNS or proxy overrides.
    with build_opener(NoRedirects()).open(
        Request(API + suffix, headers=headers), timeout=20
    ) as response:
        require(response.status == 200, "GET failed")
        raw = response.read(2_000_001)
        require(len(raw) <= 2_000_000, "Metadata too large")
    return json.loads(raw)["data"]


def number(value):
    result = Decimal(str(value))
    require(result.is_finite() and result >= 0, "Invalid financial metadata")
    return result


def check_remote(config, reserve, key):
    data = get_json(f"models/{MODEL}/endpoints")
    require(data["id"] == MODEL, "Model unavailable")
    endpoints = [e for e in data["endpoints"] if e.get("tag") == PROVIDER]
    require(len(endpoints) == 1, "Exact endpoint unavailable")
    e = endpoints[0]
    require(e["model_id"] == MODEL and e["status"] == 0, "Endpoint inactive")
    require({"reasoning", "response_format", "max_tokens", "temperature"}
            <= set(e["supported_parameters"]), "Parameters unavailable")
    require(e["max_completion_tokens"] >= 2000 and e["context_length"] >= 33493,
            "Context/output too small")
    require(number(e["pricing"]["prompt"]) * 1_000_000
            <= config.auditor_a.prompt_price_cap, "Input price increased")
    require(number(e["pricing"]["completion"]) * 1_000_000
            <= config.auditor_a.completion_price_cap, "Output price increased")
    account = get_json("key", key)
    require(account["is_free_tier"] is False, "Paid key required")
    if account["limit"] is not None:
        require(number(account["limit_remaining"]) >= reserve, "Key limit insufficient")
    credits = get_json("credits", key)
    require(number(credits["total_credits"]) - number(credits["total_usage"]) >= reserve,
            "Account credits insufficient")


def local_checks(output, config):
    data = CollectionResult.model_validate_json((ROOT / "collection.json").read_text())
    require(verify_integrity(data, ROOT) == [], "Integrity failed")
    packet = prepare(ROOT, as_of=AS_OF)
    verify_quotes(ROOT, packet)
    require(packet.sha256 == SHA, "Packet SHA mismatch")
    require(config.max_budget_usd == Decimal("0.05") and config.retries == 0
            and config.single_auditor and config.auditor_a.max_output_tokens == 2000,
            "Pilot limits changed")
    if LEDGER.exists():
        ledger = json.loads(LEDGER.read_text())
        require(not ledger.get("clinic.example", {}).get("entries"),
                "Previous site attempt exists; no further payment authorized")
    with (patch.object(socket.socket, "connect", forbidden),
          patch.object(socket, "create_connection", forbidden),
          patch.object(OpenRouterTransport, "post", forbidden)):
        report = analyze_compact(
            ROOT, output, config, as_of=AS_OF, mode="openrouter",
            allow_external_transfer=True, packet_approval=APPROVAL, ledger=LEDGER,
            dry_run=True, approval_template=False)
    require(report["status"] == "dry_run_ready" and not report["blockers"]
            and not report["request_sent"] and report["packet_sha256"] == SHA
            and not report["preview"]["sensitive_categories"], "Dry-run blocked")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="allow one paid request")
    args = parser.parse_args()
    os.umask(0o077)
    safe_paths()
    require(not ATTEMPT.exists(), "Attempt already recorded; do not retry")
    config = AnalysisConfig.model_validate_json(
        (REPO / "examples/compact-openrouter-config.json").read_text())
    if not args.execute:
        with tempfile.TemporaryDirectory(prefix="lexradar-039-") as temp:
            report = local_checks(Path(temp) / "dry", config)
        print("dry_run_ready; network disabled; reserve USD", report["preview"]["reserve_usd"])
        return
    with (WORK / "run-039.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(not ATTEMPT.exists(), "Attempt already recorded; do not retry")
        output = Path(tempfile.mkdtemp(prefix="run-039-", dir=WORK))
        report = local_checks(output / "dry", config)
        key = KEY.read_text().strip()
        require(bool(key) and not any(c.isspace() for c in key), "Invalid key")
        reserve = number(report["preview"]["reserve_usd"])
        require(reserve <= Decimal("0.05"), "Budget exceeded")
        check_remote(config, reserve, key)
        # Repeat all local checks after GETs; profile expiration remains fail-closed.
        local_checks(output / "final-dry", config)
        previous = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = key
        try:
            with patch("lexradar.auditors.provider.OpenRouterTransport", OnceTransport):
                result = analyze_compact(
                    ROOT, output / "paid", config, as_of=AS_OF, mode="openrouter",
                    allow_external_transfer=True, packet_approval=APPROVAL, ledger=LEDGER,
                    approval_template=False)
        finally:
            if previous is None:
                os.environ.pop("OPENROUTER_API_KEY", None)
            else:
                os.environ["OPENROUTER_API_KEY"] = previous
        print(result["status"], "; HOLD; results:", output)
        if result["status"] != "success":
            print("Response not available locally; generation may have been billed. "
                  "Keep ledger and attempt marker; inspect OpenRouter Logs. Do not rerun.")
        require(result["status"] == "success", "Attempt ended without success; do not retry")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never print exception bodies: network errors may contain sensitive data.
        print(f"STOP ({type(exc).__name__}); no automatic retry; inspect local state.",
              file=sys.stderr)
        sys.exit(1)
