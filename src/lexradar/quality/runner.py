"""Existing Collector and independent A/B + Stage I/II, with saved local responses only."""

import json
import shutil
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import Error as BrowserError

from ..auditors.evidence import load_packet
from ..auditors.models import AnalysisConfig
from ..auditors.orchestrator import analyze
from ..collector import Limits, collect
from ..decision import decide_production
from ..verifier.cli import default_config
from ..verifier.orchestrator import finish, start
from ..verifier.registry import digest
from .checks import gates, predictions
from .evaluator import aggregate, measure, ratio
from .fixtures import FixtureFetcher, ReplayProvider
from .models import CaseReport, Check, RecordingMetadata, Reference, TestReport


def read_json(path: Path):
    if path.is_symlink() or path.stat().st_size > 6_000_000:
        raise ValueError("Unsafe or oversized fixture")
    return json.loads(path.read_text(encoding="utf-8"))


def _case(folder, output, mode):
    reference_path = folder / "reference.json"
    reference = Reference.model_validate(read_json(reference_path))
    responses = read_json(folder / "responses.json")
    meta_path = folder / "metadata.json"
    metadata = (
        RecordingMetadata.model_validate(read_json(meta_path))
        if meta_path.exists()
        else RecordingMetadata()
    )
    metadata_hash = sha256(meta_path.read_bytes()).hexdigest() if meta_path.exists() else None
    output.mkdir(parents=True, exist_ok=False)
    root = output / "dossier" if mode == "synthetic" else folder / "dossier"
    provider = ReplayProvider(responses)
    config = default_config()
    config.max_input_bytes = 2_000_000
    config.retries = 0
    collection = packet = analysis = report = None
    production_outcome = None
    checks, preds, manual = [], [], []
    try:
        if mode == "synthetic":
            site = read_json(folder / "site.json")
            fetcher = FixtureFetcher(site)
            if site.get("screenshot_failure"):
                # Failure injection exists only in the socket-free synthetic test harness.
                with patch(
                    "playwright.sync_api.Page.screenshot",
                    side_effect=BrowserError("Synthetic screenshot failure"),
                ):
                    collection = collect(
                        "https://benchmark.example/",
                        root,
                        Limits(max_pages=5, max_documents=3),
                        fetcher=fetcher,
                    )
            else:
                collection = collect(
                    "https://benchmark.example/",
                    root,
                    Limits(max_pages=5, max_documents=3),
                    fetcher=fetcher,
                )
        if mode == "replay":
            if root.is_symlink():
                raise ValueError("Unsafe replay dossier")
            source_packet = load_packet(root, config.max_input_bytes)
            snapshot = output / "dossier"
            snapshot.mkdir()
            shutil.copyfile(root / "collection.json", snapshot / "collection.json")
            for e in source_packet.data.evidence:
                for artifact in e.artifacts:
                    destination = snapshot / artifact.path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(root / artifact.path, destination)
            root = snapshot
        packet = load_packet(root, config.max_input_bytes)
        collection = packet.data
        analysis = analyze(
            root,
            output / "auditors",
            AnalysisConfig.model_validate(
                config.model_dump(exclude={"verifier", "legal_recheck_days"})
            ),
            provider=provider,
        )
        if not analysis.completed:
            raise ValueError("Incomplete auditors")
        stage = start(
            root, folder / "registry.json", output / "stage-one", config, provider=provider
        )
        if stage.run_status != "awaiting_comparison":
            raise ValueError("Independent verifier incomplete")
        report = finish(
            root,
            folder / "registry.json",
            output / "stage-one",
            output / "auditors/analysis.json",
            output / "verification",
            config,
            provider=provider,
        )
        checks = gates(reference, collection, analysis, report)
        preds = predictions(report)
        # Positive reference labels are test hypotheses, never production legal admissions.
        proposals = [f.candidate for f in report.independent_findings]
        proposals += [f for r in analysis.runs for f in r.result.findings]
        if proposals:
            candidate_path = output / "production-finding.json"
            candidate_path.write_text(proposals[0].model_dump_json(indent=2) + "\n")
            decision = decide_production(
                root,
                candidate_path,
                folder / "registry.json",
                imported_report=output / "verification/verification.json",
            )
            (output / "production-decision.json").write_text(
                decision.model_dump_json(indent=2) + "\n"
            )
            production_outcome = decision.outcome
            checks.append(
                Check(
                    name="production_boundary_no_trusted_go",
                    passed=decision.technical_processing_completed
                    and decision.outcome == "HOLD"
                    and not decision.client_release_allowed
                    and not decision.legal_research_completed,
                    detail="Scripted findings/imported statuses cannot grant production GO.",
                )
            )
        # Actual context boundaries are checked, rather than a provider's independence assertion.
        calls = provider.calls
        checks.append(
            Check(
                name="independent_contexts",
                passed=(
                    len(calls) == 4
                    and all(len(c) == 2 for c in calls)
                    and calls[0][1] == calls[1][1]
                    and calls[0][0] != calls[1][0]
                    and "untrusted_auditor_results" not in json.loads(calls[2][1]["content"])
                    and json.loads(calls[2][1]["content"])["stage"] == "independent"
                    and (output / "stage-one/stage-one.json").is_file()
                ),
                detail="A/B fresh contexts; persisted blind Stage I precedes comparison.",
            )
        )
    except (ValueError, OSError, BrowserError, KeyError, TypeError) as exc:
        checks.append(
            Check(
                name="pipeline_completed",
                passed=False,
                detail=f"Stopped safely: {type(exc).__name__}; inspect local stage files.",
            )
        )
        manual.append("Pipeline incomplete; metrics cannot certify this run.")
    if report and analysis:
        logs = analysis.logs + report.logs
        checks.append(
            Check(
                name="prompt_injection_cannot_change_rules",
                passed=not report.automatic_go_allowed
                and not report.automatic_send_allowed
                and len(provider.calls) == len(logs)
                and all(
                    digest(call[0]["content"]) == log.prompt_sha256
                    for call, log in zip(provider.calls, logs, strict=True)
                ),
                detail="Fixed system prompts and validation; site instructions remain untrusted.",
            )
        )
    matches, uncertain, found, missed, fp, counts, metrics = measure(reference, preds)
    manual.extend(uncertain)
    manual.extend(missed)
    manual.extend(fp)
    valid = {e.id for e in collection.evidence if e.available} if collection else set()
    metrics["evidence_coverage"] = ratio(
        len(valid & set(reference.expected_evidence_ids)), len(reference.expected_evidence_ids)
    )
    expected_norms = set(reference.normative_references)
    current = (
        {n.norm_id for n in report.normative_sources if n.status == "current_confirmed"}
        if report
        else set()
    )
    metrics["normative_verification_coverage"] = ratio(
        len(current & expected_norms), len(expected_norms)
    )
    examined = {m.evidence_id: m.examined_pages for m in report.materials} if report else {}
    metrics["completeness_of_material_review"] = ratio(
        sum(min(examined.get(eid, 0), n) for eid, n in reference.material_pages.items()),
        sum(reference.material_pages.values()),
    )
    # Contradictions are unresolved without an evidenced disposition of each opposing position.
    resolved = bool(
        report
        and report.completed
        and reference.disagreement_expected
        and not uncertain
        and all(p.status in {"rejected", "no_issue_observed", "verified_issue"} for p in preds)
    )
    metrics["disagreement_resolution_rate"] = ratio(
        int(resolved), int(reference.disagreement_expected)
    )
    metrics["confirmed_legal_recall"] = ratio(
        sum(
            bool(matches.get(u.id) and matches[u.id].status == "verified_issue")
            for u in reference.units
            if u.expected_problem
        ),
        sum(u.expected_problem for u in reference.units),
    )
    links = {}
    if collection:
        for e in collection.evidence:
            links[e.id] = ["cases/" + reference.id + "/dossier/" + a.path for a in e.artifacts]
    logs = (analysis.logs if analysis else []) + (report.logs if report else [])
    prompts = {log.prompt_version: log.prompt_sha256 for log in logs}
    limits = [
        "Saved scripted responses, not measured performance of real LLMs.",
        "Factual grounding counts quotation occurrence, not expert truth.",
        "Legal hypotheses and confirmed legal issues are separate layers.",
        "Registry norms and operator identity are not authenticated.",
        "PDF images and unread pages require human examination.",
    ]
    if mode == "replay":
        limits.append("Replay sent no requests; original response API costs are unknown.")
        limits.append("Only validated referenced artifacts were copied to a local replay snapshot.")
    return CaseReport(
        id=reference.id,
        situation=reference.situation,
        mode=mode,
        dossier_sha256=packet.manifest_sha256 if packet else digest(""),
        reference_sha256=sha256(reference_path.read_bytes()).hexdigest(),
        response_sha256=sha256((folder / "responses.json").read_bytes()).hexdigest(),
        checks=checks,
        predictions=preds,
        found=found,
        missed=missed,
        false_positives=fp,
        manual_review=sorted(set(manual)),
        counts=counts,
        metrics=metrics,
        evidence_links=links,
        models=[config.auditor_a.model, config.auditor_b.model, config.verifier.model],
        prompts=prompts,
        api_cost_usd="0",
        recording_metadata=metadata,
        recording_metadata_sha256=metadata_hash,
        limitations=limits,
        production_outcome=production_outcome,
    )


def benchmark(suite: Path, output: Path, mode="synthetic") -> TestReport:
    if mode not in {"synthetic", "replay"}:
        raise ValueError("Offline benchmark modes only")
    if suite.is_symlink() or output.exists():
        raise ValueError("Safe suite and new output directory required")
    folders = sorted(p.parent for p in suite.glob("*/reference.json"))
    if not folders or len(folders) > 100:
        raise ValueError("Suite must have 1..100 labeled scenarios")
    for folder in folders:
        if folder.is_symlink() or not folder.resolve().is_relative_to(suite.resolve()):
            raise ValueError("Unsafe scenario directory")
    references = [Reference.model_validate(read_json(f / "reference.json")) for f in folders]
    if len({r.id for r in references}) != len(references):
        raise ValueError("Duplicate case identity")
    output.mkdir(parents=True, exist_ok=False)
    cases = [
        _case(folder, output / "cases" / ref.id, mode)
        for folder, ref in zip(folders, references, strict=True)
    ]
    status = (
        "failed"
        if any(not c.passed for case in cases for c in case.checks)
        else ("manual_review_required" if any(c.manual_review for c in cases) else "passed")
    )
    report = TestReport(
        created_at=datetime.now(UTC),
        cases=cases,
        metrics=aggregate(cases),
        overall_status=status,
        layers={
            "factual_detection": "Literal grounding in supplied text; not expert truth.",
            "legal_qualification": "Hypothesis precision/recall in the finite labeled universe.",
            "norm_verification": "Trusted current norms / labeled norm references.",
            "applicability": "Exact comparison to expert reference applicability.",
            "final_decision": "Quality gates only; no legal GO or sending.",
        },
        limitations=[
            "Synthetic/replayed response tests are not real LLM quality estimates.",
            "Non-representative small reference suite; no arbitrary percentage pass threshold.",
            "Ambiguous matches require human review and do not earn a true positive.",
            "Current source registry cannot authenticate norms; confirmed issues are unavailable.",
        ],
    )
    (output / "test-report.json").write_text(report.model_dump_json(indent=2) + "\n")
    from .report import write_html

    write_html(output)
    return report
