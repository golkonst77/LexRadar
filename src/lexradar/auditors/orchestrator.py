"""Independent fresh requests; sequential execution shares only the audit budget."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from .budget import Budget, BudgetExceeded
from .disagreements import compare
from .evidence import load_packet
from .models import AnalysisConfig, AnalysisReport, AuditorRun, RequestLog, Usage
from .prompts import messages_for
from .provider import OfflineProvider, OpenRouterProvider, Provider, ProviderError
from .validator import ResultError, validate_result


def analyze(
    root: Path,
    output: Path,
    config: AnalysisConfig,
    *,
    mode: str = "offline",
    allow_external_transfer: bool = False,
    provider: Provider | None = None,
    sleep=time.sleep,
) -> AnalysisReport:
    if mode not in {"offline", "openrouter"}:
        raise ValueError("Unsupported analysis mode")
    if mode == "openrouter" and not allow_external_transfer:
        raise PermissionError("Explicit permission to transfer materials to OpenRouter required")
    if mode == "offline" and isinstance(provider, OpenRouterProvider):
        raise PermissionError("Real OpenRouter provider cannot be injected into offline mode")
    packet = load_packet(root, config.max_input_bytes)
    output = output.resolve()
    if output.exists():
        raise ValueError("Analysis output must be a new directory")
    # Instantiating real provider requires a key, but no network call occurs here.
    provider = provider or (OfflineProvider() if mode == "offline" else OpenRouterProvider(config))
    budget = Budget(config.max_budget_usd)
    started = datetime.now(UTC)
    logs = []
    runs = []
    for auditor, settings in (("A", config.auditor_a), ("B", config.auditor_b)):
        messages, version, prompt_hash = messages_for(auditor, packet)
        run = AuditorRun(
            auditor=auditor, model=settings.model, prompt_version=version, status="provider_error"
        )
        for attempt in range(config.retries + 1):
            stamp, timer = datetime.now(UTC), time.monotonic()
            usage = Usage()
            reserved = charge = 0
            status = "provider_error"
            retry = False
            sent = False
            try:
                if mode == "openrouter":
                    reserved = charge = budget.reserve(settings, messages)
                # Reconstruct fresh messages even if an injected test provider mutates its input.
                sent = mode == "openrouter"
                reply = provider.complete(settings, [dict(m) for m in messages])
                usage = reply.usage
                if mode == "openrouter":
                    charge = budget.settle(reserved, usage)
                    if budget.overrun:
                        raise BudgetExceeded("Unexpected provider cost; no further calls permitted")
                run.result, run.validation_notes = validate_result(reply.content, auditor, packet)
                run.status, status = "success", "success"
            except BudgetExceeded:
                run.status, status = "budget_exceeded", "budget_exceeded"
            except ProviderError as exc:
                run.status, status = "provider_error", exc.code
                retry = exc.retryable and attempt < config.retries
            except ResultError as exc:
                run.status, status = "invalid_response", str(exc)
                # Invalid evidence/schema results are not sent back into either auditor's context.
            logs.append(
                RequestLog(
                    auditor=auditor,
                    model=settings.model,
                    started_at=stamp,
                    duration_seconds=max(0, time.monotonic() - timer),
                    prompt_version=version,
                    prompt_sha256=prompt_hash,
                    evidence_packet_sha256=packet.sha256,
                    attempt=attempt + 1,
                    request_sent=sent,
                    status=status,
                    usage=usage,
                    budget_charge_usd=charge,
                )
            )
            if not retry:
                break
            sleep(min(config.max_backoff_seconds, 2**attempt))
        runs.append(run)
    report = AnalysisReport(
        mode=mode,
        collector_manifest_sha256=packet.manifest_sha256,
        evidence_packet_sha256=packet.sha256,
        started_at=started,
        completed=all(run.status == "success" for run in runs),
        runs=runs,
        disagreements=compare(runs[0].result, runs[1].result),
        logs=logs,
        budget_charged_usd=budget.charged,
        budget_limit_usd=budget.limit,
        same_model_independence_limited=config.auditor_a.model == config.auditor_b.model,
        limitations=[
            *packet.limitations,
            "Agreement is not independent verification of legal correctness",
            "Prompt safeguards cannot guarantee absence of model-level injection effects",
            "Same model reduces independence"
            if config.auditor_a.model == config.auditor_b.model
            else "Different models do not guarantee independent training or legal accuracy",
            "Offline outputs are placeholders only"
            if mode == "offline"
            else "Budget depends on price/token caps; unexpected charges stop further calls",
        ],
    )
    output.mkdir(parents=True, exist_ok=False)
    (output / "analysis.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (output / "requests.jsonl").write_text(
        "\n".join(log.model_dump_json() for log in logs) + "\n", encoding="utf-8"
    )
    (output / "run.json").write_text(
        json.dumps(
            {
                "mode": mode,
                "schema_version": "0.3",
                "external_transfer_explicitly_permitted": allow_external_transfer
                if mode == "openrouter"
                else False,
                "collector_manifest_sha256": packet.manifest_sha256,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return report
