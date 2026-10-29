"""Independent fresh requests; optional parallel execution is offline only."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from .budget import Budget, BudgetExceeded
from .disagreements import compare
from .evidence import load_packet
from .models import (
    AnalysisConfig,
    AnalysisReport,
    AuditorRun,
    ProviderDiagnostics,
    RequestLog,
    Usage,
)
from .preflight import LIMITATION, MVP_NO_SCREENING_NOTICE, authorize
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
    packet_approval: Path | None = None,
    provider: Provider | None = None,
    parallel: bool = False,
    mvp_no_screening: bool = False,
    sleep=time.sleep,
    message_builder=messages_for,
    progress=None,
) -> AnalysisReport:
    def emit(stage, **details):
        if progress is not None:
            progress(stage, **details)

    emit("packet_check_started")
    if type(mvp_no_screening) is not bool:
        raise ValueError("MVP_NO_SCREENING must be an explicit boolean")
    if mvp_no_screening:
        from ..experiment_069 import experiment_profile
        from ..experiment_069 import messages_for as experiment_messages
        from ..single_auditor import messages_for as single_messages
        from ..single_auditor import profile

        legacy = (
            config == profile(timeout_seconds=config.timeout_seconds)
            and message_builder is single_messages
        )
        # The same Single A route may tighten cost and retry limits for an MVP.
        base = profile(timeout_seconds=config.timeout_seconds, model=config.auditor_a.model)
        tightened = (
            message_builder is single_messages
            and config.auditor_a == config.auditor_b
            and config.auditor_a.model == base.auditor_a.model
            and config.auditor_a.max_output_tokens == base.auditor_a.max_output_tokens
            and config.auditor_a.prompt_price_cap <= base.auditor_a.prompt_price_cap
            and config.auditor_a.completion_price_cap <= base.auditor_a.completion_price_cap
            and config.max_budget_usd <= base.max_budget_usd
            and config.retries <= base.retries
            and config
            == base.model_copy(
                update={
                    "auditor_a": config.auditor_a,
                    "auditor_b": config.auditor_b,
                    "max_budget_usd": config.max_budget_usd,
                    "retries": config.retries,
                }
            )
        )
        experiment = config == experiment_profile() and message_builder is experiment_messages
        if not (legacy or tightened or experiment):
            raise ValueError("MVP_NO_SCREENING requires the Single A profile and prompt")
    if mode not in {"offline", "openrouter"}:
        raise ValueError("Unsupported analysis mode")
    if mode == "openrouter" and not allow_external_transfer:
        raise PermissionError("Explicit permission to transfer materials to OpenRouter required")
    if mode == "offline" and isinstance(provider, OpenRouterProvider):
        raise PermissionError("Real OpenRouter provider cannot be injected into offline mode")
    if parallel and mode != "offline":
        raise PermissionError("Parallel execution is offline only; paid budget stays sequential")
    packet = load_packet(root, config.max_input_bytes)
    emit("packet_check_completed", packet_bytes=len(packet.payload.encode()))

    def approve():
        if mvp_no_screening:
            return authorize(packet, packet_approval, mvp_no_screening=True)
        return authorize(packet, packet_approval)

    approval = approve() if mode == "openrouter" else None
    output = output.resolve()
    if output.exists():
        raise ValueError("Analysis output must be a new directory")
    # Instantiating real provider requires a key, but no network call occurs here.
    provider = provider or (OfflineProvider() if mode == "offline" else OpenRouterProvider(config))
    from ..audit_capture import Capture

    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    budget = Budget(config.max_budget_usd)
    started = datetime.now(UTC)
    logs = []
    runs = []
    auditors = (
        (("A", config.auditor_a),)
        if config.single_auditor
        else (("A", config.auditor_a), ("B", config.auditor_b))
    )

    def execute(item):
        auditor, settings = item
        local_logs = []
        messages, version, prompt_hash = message_builder(auditor, packet)
        emit("model_and_prompt", model=settings.model, prompt=version)
        run = AuditorRun(
            auditor=auditor, model=settings.model, prompt_version=version, status="provider_error"
        )
        for attempt in range(1 if config.single_auditor else config.retries + 1):
            stamp, timer = datetime.now(UTC), time.monotonic()
            usage = Usage()
            reserved = charge = 0
            status = "provider_error"
            retry = False
            sent = False
            diagnostics = None
            capture = Capture(output / f"capture-{auditor}-{attempt + 1}")
            capture.write("system.txt", messages[0]["content"])
            capture.write("packet.txt", packet.payload)
            capture.write("source.json", json.dumps({"dossier": str(root.resolve())}))
            if isinstance(provider, OpenRouterProvider):
                provider.audit_capture = capture
            try:
                if mode == "openrouter":
                    approve()
                    reserved = charge = budget.reserve(settings, messages)
                # Reconstruct fresh messages even if an injected test provider mutates its input.
                sent = mode == "openrouter"
                emit("request_sending", attempt=attempt + 1)
                if progress is not None:
                    from ..audit_progress import waiting

                    with waiting(emit):
                        reply = provider.complete(settings, [dict(m) for m in messages])
                else:
                    reply = provider.complete(settings, [dict(m) for m in messages])
                emit("response_received", duration_seconds=round(time.monotonic() - timer, 3))
                if not (capture.root / "original.txt").exists():
                    capture.response(reply.content)
                usage = reply.usage
                emit(
                    "usage",
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    actual_cost_usd=str(usage.cost_usd) if usage.cost_usd is not None else None,
                )
                if mode == "openrouter":
                    charge = budget.settle(reserved, usage)
                    if budget.overrun:
                        raise BudgetExceeded("Unexpected provider cost; no further calls permitted")
                emit("structure_and_evidence_check_started")
                run.result, run.validation_notes = validate_result(
                    reply.content,
                    auditor,
                    packet,
                    require_cookie_review=version == "single-a-v3-cookie",
                )
                emit("structure_and_evidence_check_completed")
                run.status, status = "success", "success"
            except BudgetExceeded:
                run.status, status = "budget_exceeded", "budget_exceeded"
            except ProviderError as exc:
                run.status, status = "provider_error", exc.code
                diagnostics = ProviderDiagnostics.sanitized(exc.diagnostics)
                retry = exc.retryable and attempt < config.retries
            except ResultError as exc:
                run.status, status = "invalid_response", str(exc)
                # Invalid evidence/schema results are not sent back into either auditor's context.
            except Exception:
                if mode != "offline":
                    raise
                run.status, status = "provider_error", "offline_provider_error"
            emit(
                "attempt_completed",
                status=status,
                duration_seconds=round(time.monotonic() - timer, 3),
                cost_uncertain=sent and usage.cost_usd is None,
            )
            capture.write("status.json", json.dumps({"status": status}))
            local_logs.append(
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
                    provider_diagnostics=diagnostics,
                )
            )
            if not retry:
                break
            sleep(min(config.max_backoff_seconds, 2**attempt))
        return run, local_logs

    if parallel:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(execute, auditors))
    else:
        outcomes = [execute(item) for item in auditors]
    for run, local_logs in outcomes:
        runs.append(run)
        logs.extend(local_logs)
    report = AnalysisReport(
        mode=mode,
        collector_manifest_sha256=packet.manifest_sha256,
        evidence_packet_sha256=packet.sha256,
        started_at=started,
        completed=all(run.status == "success" for run in runs),
        runs=runs,
        disagreements=compare(runs[0].result, runs[1].result) if len(runs) == 2 else [],
        logs=logs,
        budget_charged_usd=budget.charged,
        budget_limit_usd=budget.limit,
        same_model_independence_limited=(
            not config.single_auditor and config.auditor_a.model == config.auditor_b.model
        ),
        limitations=[
            *packet.limitations,
            *([MVP_NO_SCREENING_NOTICE] if mvp_no_screening else []),
            LIMITATION,
            "Agreement is not independent verification of legal correctness",
            "Prompt safeguards cannot guarantee absence of model-level injection effects",
            "Single auditor: no independent second-model review"
            if config.single_auditor
            else (
                "Same model reduces independence"
                if config.auditor_a.model == config.auditor_b.model
                else "Different models do not guarantee independent training or legal accuracy"
            ),
            "Offline outputs are placeholders only"
            if mode == "offline"
            else "Budget depends on price/token caps; unexpected charges stop further calls",
        ],
    )
    emit("saving_analysis_started")
    (output / "analysis.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (output / "requests.jsonl").write_text(
        "\n".join(log.model_dump_json() for log in logs) + "\n", encoding="utf-8"
    )
    (output / "run.json").write_text(
        json.dumps(
            {
                "mode": mode,
                "schema_version": "0.3",
                "timeout_seconds": config.timeout_seconds,
                "MVP_NO_SCREENING": mvp_no_screening,
                "personal_data_transfer_check": (
                    MVP_NO_SCREENING_NOTICE if mvp_no_screening else "enabled"
                ),
                "packet_approval": approval.model_dump(mode="json") if approval else None,
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
    emit("saving_analysis_completed")
    return report
