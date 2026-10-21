"""Single legal auditor route. CLI only replays local responses; no live API switch."""

import argparse
import hashlib
import json
from importlib.resources import files
from pathlib import Path

from .auditors.evidence import load_packet
from .auditors.models import AIAuditorResult, AnalysisConfig
from .auditors.orchestrator import analyze
from .auditors.provider import OpenRouterProvider, ProviderError, ProviderReply
from .auditors.validator import validate_result
from .offline_mvp import FakeOpenRouterProposal, canonical, export_proposal, quotes_valid

# Default migrated using the saved runs-089/090; model remains configurable.
MODEL = "openai/gpt-5-mini"
MAX_INPUT_TOKENS_BOUND = 240_000


def profile(*, timeout_seconds=120, model=MODEL):
    if type(timeout_seconds) not in (int, float) or timeout_seconds not in (45, 120):
        raise ValueError("Single Auditor MVP timeout must be 45 or 120 seconds")
    settings = dict(model=model, prompt_price_cap=1, completion_price_cap=2,
                    max_output_tokens=28000 if model == MODEL else 4000)
    return AnalysisConfig(
        single_auditor=True,
        auditor_a=settings,
        auditor_b=settings,
        max_budget_usd="0.50",
        timeout_seconds=timeout_seconds,
        retries=0,
        max_backoff_seconds=1,
        max_input_bytes=200_000,
        max_response_bytes=500_000,
    )


def messages_for(auditor, packet, *, historical_schema=False):
    if auditor != "A":
        raise ValueError("Only auditor A is allowed")
    base = files("lexradar.auditors").joinpath("prompts/a-v1.txt").read_text()
    extra = files("lexradar.auditors").joinpath(
        "prompts/single-a-v1.txt" if historical_schema else "prompts/single-a-v3-cookie.txt"
    ).read_text()
    system = (
        base
        + "\n"
        + extra
        + "\nOutput JSON schema:\n"
        + (
            files("lexradar.auditors")
            .joinpath("prompts/single-a-v1-schema.txt")
            .read_text(encoding="utf-8")
            if historical_schema
            else json.dumps(AIAuditorResult.model_json_schema(), ensure_ascii=False)
        )
    )
    return (
        [{"role": "system", "content": system}, {"role": "user", "content": packet.payload}],
        "single-a-v1" if historical_schema else "single-a-v3-cookie",
        hashlib.sha256(system.encode()).hexdigest(),
    )


class DeepSeekAdapter(OpenRouterProvider):
    """Real transport/parser reused; invoked only by separately authorized paid analyze.

    Budget reservations/retries belong to analyze(mode='openrouter'), including
    packet approval. Never inject this adapter into offline runs.
    """

    def __init__(self, transport=None, *, timeout_seconds=120, model=MODEL):
        super().__init__(profile(timeout_seconds=timeout_seconds, model=model), transport=transport)

    def complete(self, settings, messages):
        if (
            self.config != profile(
                timeout_seconds=self.config.timeout_seconds, model=settings.model
            )
            or settings != profile(model=settings.model).auditor_a
        ):
            raise ProviderError("unexpected_single_auditor_configuration")
        if sum(len(m["content"].encode()) for m in messages) + 1024 > MAX_INPUT_TOKENS_BOUND:
            raise ProviderError("input_token_bound_exceeded")
        reply = super().complete(settings, messages)
        if (reply.usage.completion_tokens or 0) > settings.max_output_tokens:
            raise ProviderError("output_token_limit_exceeded")
        return reply


def checked_report(report, packet):
    if (
        report.evidence_packet_sha256 != packet.sha256
        or report.collector_manifest_sha256 != packet.manifest_sha256
    ):
        raise ValueError("Dossier/analysis hash mismatch")
    if len(report.runs) != 1 or report.runs[0].auditor != "A" or not report.runs[0].model:
        raise ValueError("Single legal auditor A required")
    report = report.model_copy(deep=True)
    if report.runs[0].result is not None:
        report.runs[0].result, _ = validate_result(
            report.runs[0].result.model_dump_json(), "A", packet
        )
    return report


def commercial(report, packet):
    report = checked_report(report, packet)
    reasons = [] if report.completed and report.runs[0].status == "success" else ["auditor_failed"]
    result = report.runs[0].result
    allowed = []
    for f in result.findings if result else []:
        if (
            f.material
            and f.fact_supported
            and f.fact_assertion == "present"
            and f.status == "potential"
            and f.legal_position == "possible_issue"
            and f.evidence_quality == "complete"
            and f.proposed_work
            and quotes_valid(f, packet)
        ):
            allowed.append(f.id)
    if not allowed:
        reasons.append("no_grounded_serviceable_material_finding")
    return sorted(allowed), reasons


class RecordedA:
    def __init__(self, response):
        self.response = json.dumps(response)

    def complete(self, settings, messages):
        if settings.model != MODEL:
            raise AssertionError("Unexpected model")
        from .auditors.models import Usage

        return ProviderReply(self.response, Usage())


def run(
    root: Path, output: Path, response: dict, *, provider=None, proposal_provider=None, pdf=False
):
    if output.exists():
        raise ValueError("Run output must be a new directory")
    packet = load_packet(root, profile().max_input_bytes)
    report = analyze(
        root,
        output / "analysis",
        profile(),
        provider=provider or RecordedA(response),
        message_builder=messages_for,
    )
    allowed, reasons = commercial(report, packet)
    result = {
        "schema_version": "050",
        "mode": "offline_recorded_or_synthetic",
        "packet_sha256": packet.sha256,
        "dossier_sha256": packet.manifest_sha256,
        "analysis": report.model_dump(mode="json"),
        "commercial": {
            "outcome": "HOLD" if reasons else "GO",
            "reasons": reasons,
            "allowed_findings": allowed,
        },
        "observations": [
            {
                "id": f.id,
                "fact_status": "established_excerpt"
                if f.fact_supported and quotes_valid(f, packet)
                else "unconfirmed_assumption",
                "legal_status": "potential_violation"
                if f.legal_position == "possible_issue"
                else "undetermined",
                "proposed_work": f.proposed_work,
            }
            for f in report.runs[0].result.findings
        ]
        if report.runs[0].result
        else [],
        "legal_production_status": "HOLD",
        "send_allowed": False,
    }
    try:
        result["proposal"] = export_proposal(
            root,
            output / "client",
            report,
            proposal_provider or FakeOpenRouterProposal(),
            pdf=pdf,
            decision=commercial,
            recheck=checked_report,
        )
    except Exception as exc:
        result["proposal"] = {"status": "blocked", "error_type": type(exc).__name__}
    (output / "run.json").write_bytes(canonical(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dossier", type=Path)
    parser.add_argument("response", type=Path, help="Recorded A result JSON")
    parser.add_argument("output", type=Path)
    parser.add_argument("--pdf", action="store_true")
    args = parser.parse_args()
    result = run(args.dossier, args.output, json.loads(args.response.read_text()), pdf=args.pdf)
    print(json.dumps(result["commercial"], ensure_ascii=False))


if __name__ == "__main__":
    main()
