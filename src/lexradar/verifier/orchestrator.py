"""Persist blind Stage I before reading A/B; each new external packet needs approval."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from ..auditors.budget import Budget
from ..auditors.evidence import load_packet
from ..auditors.preflight import LIMITATION, authorize, write_preflight
from ..auditors.provider import OpenRouterProvider
from .materials import completeness, examined
from .models import (
    HumanFindingReview,
    IndependentResult,
    VerificationReport,
    VerifierConfig,
    VerifierRequestLog,
)
from .packets import canonical, load_analysis, prepare_comparison, prepare_independent
from .registry import assess_registry, digest
from .rules import compare_results, normalize_independent
from .runtime import OfflineVerifierProvider, request


def _write(path, value):
    path.write_text(canonical(value) + "\n", encoding="utf-8")


def _mode(mode, permission, provider):
    if mode not in {"offline", "openrouter"}:
        raise ValueError("Invalid verifier mode")
    if mode == "openrouter" and not permission:
        raise PermissionError("Explicit external transfer permission required")
    if mode == "offline" and isinstance(provider, OpenRouterProvider):
        raise PermissionError("Real provider cannot be injected into offline mode")


def _report(
    prepared,
    independent,
    assessments,
    raw,
    logs,
    budget,
    mode,
    started,
    status,
    stage_two_hash=None,
    independence_limited=True,
    recheck_days=30,
):
    now = datetime.now(UTC)
    return VerificationReport(
        mode=mode,
        started_at=started,
        stage_one_packet_sha256=prepared.packet.sha256,
        stage_two_packet_sha256=stage_two_hash,
        stage_one_result_sha256=digest(canonical(raw)),
        collector_manifest_sha256=prepared.packet.manifest_sha256,
        registry_sha256=prepared.registry_sha256,
        completed=status == "complete",
        run_status=status,
        independent_directions=raw.get("directions", []),
        independent_findings=independent,
        auditor_assessments=assessments,
        normative_sources=list(
            assess_registry(
                prepared.registry, prepared.packet.data.started_at.date(), now, recheck_days
            ).values()
        ),
        materials=prepared.materials,
        unexamined_material_ids=[
            m.evidence_id
            for m in prepared.materials
            if m.examination != "text_only" or m.visual_review_required
        ],
        completeness=completeness(prepared.materials),
        confirmed_problem_ids=[f.candidate.id for f in independent if f.status == "verified_issue"],
        potential_problem_ids=[f.candidate.id for f in independent if f.status == "potential_issue"]
        + [
            f"{a.proposal.auditor}:{a.proposal.finding_id}"
            for a in assessments
            if a.status == "potential_issue" and a.duplicate_of is None
        ],
        rejected_hypothesis_ids=[f.candidate.id for f in independent if f.status == "rejected"]
        + [
            f"{a.proposal.auditor}:{a.proposal.finding_id}"
            for a in assessments
            if a.status == "rejected"
        ],
        recommendations=list(
            dict.fromkeys(
                [
                    *(check for f in independent for check in f.additional_checks),
                    *(check for a in assessments for check in a.additional_checks),
                ]
            )
        )
        + [
            "Review unread public materials and operator applicability; "
            "no blanket requirement for private internal documents"
        ],
        limitations=[
            *prepared.packet.limitations,
            *raw.get("limitations", []),
            LIMITATION,
            "Text-review completeness is not legal certainty or whole-site coverage",
            "PDF images/layout not visually reviewed; quoted text occurrence is not semantic proof",
            "Local human attestations are trusted inputs, not authenticated signatures",
            "No automatic live legal-source verification; LLM cannot verify norm currency",
            "Empty findings never establish full legal compliance",
            "Offline placeholders perform no legal search"
            if mode == "offline"
            else "Provider price caps and model safeguards are not absolute guarantees",
        ],
        logs=logs,
        budget_charged_usd=budget.charged,
        budget_limit_usd=budget.limit,
        model_independence_limited=independence_limited,
    )


def _persist_report(output, report):
    (output / "verification.json").write_text(
        report.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (output / "requests.jsonl").write_text(
        "\n".join(log.model_dump_json() for log in report.logs) + "\n", encoding="utf-8"
    )


def start(
    root: Path,
    registry_path: Path,
    output: Path,
    config: VerifierConfig,
    *,
    mode="offline",
    allow_external_transfer=False,
    packet_approval=None,
    provider=None,
):
    _mode(mode, allow_external_transfer, provider)
    prepared = prepare_independent(root, registry_path, config)
    approval = authorize(prepared.packet, packet_approval) if mode == "openrouter" else None
    if output.exists():
        raise ValueError("New verifier output directory required")
    provider = provider or (
        OfflineVerifierProvider() if mode == "offline" else OpenRouterProvider(config)
    )
    budget = Budget(config.max_budget_usd)
    started = datetime.now(UTC)
    result, logs, status = request(
        "independent", prepared.packet, config, provider, budget, mode, packet_approval
    )
    findings = []
    raw = result.model_dump(mode="json") if result else {}
    if result:
        try:
            materials = examined(prepared.materials, result)
            prepared = replace(prepared, materials=materials)
            findings = normalize_independent(result, prepared, config, datetime.now(UTC))
        except ValueError:
            result, status = None, "invalid_evidence_response"
            raw = {}
            logs[-1].status = status
    # Saved blind result precedes any access to the A/B report, even offline.
    output.mkdir(parents=True, exist_ok=False)
    _write(output / "independent.json", raw)
    state = {
        "schema_version": "0.4",
        "mode": mode,
        "model": config.verifier.model,
        "config_sha256": digest(canonical(config.model_dump(mode="json"))),
        "started_at": started.isoformat(),
        "status": status,
        "packet_sha256": prepared.packet.sha256,
        "registry_sha256": prepared.registry_sha256,
        "manifest_sha256": prepared.packet.manifest_sha256,
        "result_sha256": digest(canonical(raw)),
        "budget_charged_usd": str(budget.charged),
        "budget_limit_usd": str(budget.limit),
        "logs": [log.model_dump(mode="json") for log in logs],
        "packet_approval": approval.model_dump(mode="json") if approval else None,
    }
    _write(output / "stage-one.json", state)
    report = _report(
        prepared,
        findings,
        [],
        raw,
        logs,
        budget,
        mode,
        started,
        "awaiting_comparison" if result else status,
        recheck_days=config.legal_recheck_days,
    )
    _persist_report(output, report)
    return report


def _resume(root, registry_path, stage_one, analysis_path, config):
    state_path, result_path = stage_one / "stage-one.json", stage_one / "independent.json"
    if any(p.is_symlink() or p.stat().st_size > 6_000_000 for p in (state_path, result_path)):
        raise ValueError("Unsafe/oversized persisted independent result")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    raw = json.loads(result_path.read_text(encoding="utf-8"))
    prepared = prepare_independent(root, registry_path, config)
    if (
        state["status"] != "success"
        or state["packet_sha256"] != prepared.packet.sha256
        or state["registry_sha256"] != prepared.registry_sha256
        or state["manifest_sha256"] != prepared.packet.manifest_sha256
        or state["result_sha256"] != digest(canonical(raw))
        or state["config_sha256"] != digest(canonical(config.model_dump(mode="json")))
    ):
        raise ValueError("Independent snapshot changed or Stage I not successful")
    result = IndependentResult.model_validate(raw)
    prepared = replace(prepared, materials=examined(prepared.materials, result))
    # This is the first place where auditor contents may be read.
    analysis = load_analysis(analysis_path, prepared.packet)
    original_packet = load_packet(root, config.max_input_bytes)
    if analysis.evidence_packet_sha256 != original_packet.sha256:
        raise ValueError("Auditor packet hash mismatch")
    packet = prepare_comparison(prepared, raw, analysis, config.max_input_bytes)
    return prepared, result, raw, state, analysis, packet


def preflight(root, registry_path, output, config, *, stage_one=None, analysis_path=None):
    if stage_one is None:
        if analysis_path is not None:
            raise ValueError("Stage I preflight must not read A/B")
        packet = prepare_independent(root, registry_path, config).packet
    else:
        if analysis_path is None:
            raise ValueError("Comparison preflight requires both A/B results")
        packet = _resume(root, registry_path, stage_one, analysis_path, config)[-1]
    write_preflight(packet, output)
    return packet.sha256


def finish(
    root: Path,
    registry_path: Path,
    stage_one: Path,
    analysis_path: Path,
    output: Path,
    config: VerifierConfig,
    *,
    mode="offline",
    allow_external_transfer=False,
    packet_approval=None,
    human_reviews: list[HumanFindingReview] | None = None,
    provider=None,
):
    _mode(mode, allow_external_transfer, provider)
    prepared, result, raw, state, analysis, packet = _resume(
        root, registry_path, stage_one, analysis_path, config
    )
    if state["mode"] != mode:
        raise ValueError("Cannot switch mode when resuming verifier")
    findings = normalize_independent(result, prepared, config, datetime.now(UTC), human_reviews)
    approval = authorize(packet, packet_approval) if mode == "openrouter" else None
    if output.exists():
        raise ValueError("New comparison output directory required")
    provider = provider or (
        OfflineVerifierProvider() if mode == "offline" else OpenRouterProvider(config)
    )
    budget = Budget(config.max_budget_usd)
    budget.charged = Decimal(state["budget_charged_usd"])
    if budget.charged < 0 or not budget.charged.is_finite():
        raise ValueError("Invalid saved budget")
    comparison, logs, status = request(
        "comparison", packet, config, provider, budget, mode, packet_approval
    )
    assessments = []
    if comparison:
        try:
            assessments = compare_results(
                comparison, analysis, findings, prepared, config, datetime.now(UTC)
            )
            status = "complete"
        except ValueError:
            status = "invalid_comparison_response"
            logs[-1].status = status
    previous_logs = [VerifierRequestLog.model_validate(log) for log in state["logs"]]
    report = _report(
        prepared,
        findings,
        assessments,
        raw,
        previous_logs + logs,
        budget,
        mode,
        datetime.fromisoformat(state["started_at"]),
        status,
        packet.sha256,
        any(r.model == config.verifier.model for r in analysis.runs),
        config.legal_recheck_days,
    )
    output.mkdir(parents=True, exist_ok=False)
    _persist_report(output, report)
    _write(
        output / "run.json",
        {
            "packet_approval": approval.model_dump(mode="json") if approval else None,
            "stage_one_result_sha256": state["result_sha256"],
        },
    )
    return report
