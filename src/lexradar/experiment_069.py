"""One explicitly authorized experiment; immutable packet, no retries or client sending."""

import argparse
import hashlib
import json
from importlib.resources import files
from pathlib import Path

from .auditors.budget import Budget
from .auditors.evidence import load_packet
from .auditors.models import AIAuditorResult
from .auditors.orchestrator import analyze
from .auditors.preflight import authorize
from .auditors.provider import OpenRouterProvider, OpenRouterTransport, ProviderError
from .single_auditor import profile

PACKET = "42d33ebbf0e329b6ea76bb04bdcb9f844877a8f046c18610ce378ca8ab7f06f9"


def experiment_profile():
    base = profile(timeout_seconds=120)
    settings = base.auditor_a.model_copy(update={"max_output_tokens": 10000})
    return base.model_copy(
        update={
            "auditor_a": settings,
            "auditor_b": settings,
            "retries": 0,
            "max_budget_usd": base.max_budget_usd / 5,
        }
    )


def messages_for(auditor, packet):
    if auditor != "A" or packet.sha256 != PACKET:
        raise ValueError("Only approved packet and auditor A allowed")
    prompts = files("lexradar.auditors").joinpath("prompts")
    system = (
        prompts.joinpath("a-v1.txt").read_text()
        + "\n"
        + prompts.joinpath("single-a-v2.txt").read_text()
        + "\nOutput JSON schema:\n"
        + json.dumps(AIAuditorResult.model_json_schema(), ensure_ascii=False)
    )
    return (
        [{"role": "system", "content": system}, {"role": "user", "content": packet.payload}],
        "single-a-v2",
        hashlib.sha256(system.encode()).hexdigest(),
    )


class OnePostTransport:
    def __init__(self, transport=None, *, streamlake=False):
        self.transport = transport or OpenRouterTransport()
        self.calls = 0
        self.streamlake = streamlake

    def post(self, payload, key, timeout, max_bytes):
        if self.calls:
            raise ProviderError("experiment_call_limit")
        request = json.loads(payload)
        config = experiment_profile()
        messages = request["messages"]
        if (
            request["model"] != config.auditor_a.model
            or request["max_tokens"] != config.auditor_a.max_output_tokens
            or timeout != 120
            or request["n"] != 1
            or request["stream"] is not False
            or request["provider"]["allow_fallbacks"] is not False
            or request["provider"].get("order") != (["streamlake"] if self.streamlake else None)
            or request["provider"]["max_price"] != {"prompt": 1.0, "completion": 2.0}
            or hashlib.sha256(messages[1]["content"].encode()).hexdigest() != PACKET
        ):
            raise ProviderError("experiment_request_mismatch")
        Budget(config.max_budget_usd).reserve(config.auditor_a, messages)
        self.calls += 1  # Consume even if the transport fails; no second POST.
        return self.transport.post(payload, key, timeout, max_bytes)


def run(dossier, output, approval, *, streamlake=False):
    config = experiment_profile()
    packet = load_packet(dossier, config.max_input_bytes)
    messages, _, _ = messages_for("A", packet)
    authorize(packet, approval, mvp_no_screening=True)
    Budget(config.max_budget_usd).reserve(config.auditor_a, messages)
    # Output exclusivity in analyze prevents accidental reruns into this experiment.
    provider = OpenRouterProvider(
        config, transport=OnePostTransport(streamlake=streamlake), streamlake=streamlake
    )
    return analyze(
        dossier,
        output,
        config,
        mode="openrouter",
        allow_external_transfer=True,
        packet_approval=approval,
        provider=provider,
        mvp_no_screening=True,
        message_builder=messages_for,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dossier", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--packet-approval", type=Path, required=True)
    parser.add_argument("--streamlake", action="store_true", help="Experimental provider pin")
    args = parser.parse_args()
    report = run(args.dossier, args.output, args.packet_approval, streamlake=args.streamlake)
    print(
        json.dumps(
            {
                "completed": report.completed,
                "cost_usd": str(report.logs[0].usage.cost_usd),
                "attempts": len(report.logs),
            }
        )
    )


if __name__ == "__main__":
    # Keep message-builder identity stable for the strict screening gate under -m.
    from lexradar.experiment_069 import main as canonical_main

    canonical_main()
