"""Task 48: strictly local client-proposal rehearsal, separate from internal demo/review.

Run: python -m lexradar.offline_mvp DOSSIER RESPONSES_JSON NEW_OUTPUT [--pdf]
No credentials, transport, production approval or sending capability exists here.
"""

import argparse
import copy
import hashlib
import json
import tempfile
from html import escape
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from .auditors.disagreements import compare
from .auditors.evidence import EvidencePacket, load_packet
from .auditors.models import AnalysisConfig, AnalysisReport, Usage
from .auditors.orchestrator import analyze
from .auditors.provider import ProviderReply
from .auditors.validator import validate_result
from .models import Model
from .pdf_export import write_pdf

PAIR = ("deepseek/deepseek-v3.2", "qwen/qwen3-235b-a22b")
NOTICE = "Синтетическое офлайн-КП. Правовая оценка не подтверждена. Отправка запрещена."
SERVICE = "Проверка контекста наблюдений и применимости требований специалистом."


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def profile() -> AnalysisConfig:
    return AnalysisConfig.model_validate(
        {
            "auditor_a": {"model": PAIR[0], "prompt_price_cap": 1, "completion_price_cap": 1},
            "auditor_b": {"model": PAIR[1], "prompt_price_cap": 1, "completion_price_cap": 1},
            "max_budget_usd": 1,
            "retries": 0,
        }
    )


class RecordedAuditors:
    """Only recorded JSON; each call reads its own immutable serialized response."""

    def __init__(self, responses: dict):
        self.responses = {k: json.dumps(v) for k, v in responses.items()}

    def complete(self, settings, messages):
        label = "A" if settings.model == PAIR[0] else "B"
        return ProviderReply(self.responses[label], Usage())


class Proposal(Model):
    packet_sha256: str
    analysis_sha256: str
    finding_ids: list[str] = Field(min_length=1)
    # No free prose, amounts, deadlines or legal assertions accepted in minimal MVP.
    service: Literal["context_review"] = "context_review"


class ProposalProvider(Protocol):
    def generate(self, request: dict) -> dict: ...


class FakeOpenRouterProposal:
    """OpenRouter-stage interface rehearsal; intentionally has no HTTP adapter."""

    def __init__(self):
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return {k: request[k] for k in ("packet_sha256", "analysis_sha256", "finding_ids")}


class ReviewBinding(Model):
    model_config = {"extra": "forbid", "frozen": True}
    document_sha256: str
    reviewer: str
    authenticated: Literal[False] = False


def bind_review(bundle: Path, reviewer: str) -> ReviewBinding:
    return ReviewBinding(document_sha256=document_hash(bundle), reviewer=reviewer)


def review_matches(bundle: Path, review: ReviewBinding) -> bool:
    # Local matching is not authentication or permission to release.
    return bool(review.reviewer.strip()) and review.document_sha256 == document_hash(bundle)


def document_hash(bundle: Path) -> str:
    """Hash actual document bytes, never trust a supplied manifest hash."""
    names = ["proposal.json", "proposal.html"]
    if (bundle / "proposal.pdf").exists():
        names.append("proposal.pdf")
    values = {}
    for name in names:
        path = bundle / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Unsafe/missing proposal document")
        values[name] = digest(path.read_bytes())
    return digest(canonical(values))


def checked_report(report: AnalysisReport, packet: EvidencePacket) -> AnalysisReport:
    if (
        report.mode != "offline"
        or report.evidence_packet_sha256 != packet.sha256
        or report.collector_manifest_sha256 != packet.manifest_sha256
    ):
        raise ValueError("Dossier/analysis hash mismatch")
    if [r.model for r in report.runs] != list(PAIR):
        raise ValueError("Separate DeepSeek and Qwen required")
    # Revalidate facts against reproduced originals rather than trusting fact_supported flags.
    runs = []
    for run in report.runs:
        run = run.model_copy(deep=True)
        if run.result is not None:
            run.result, _ = validate_result(run.result.model_dump_json(), run.auditor, packet)
        runs.append(run)
    data = report.model_dump()
    data["runs"] = [r.model_dump() for r in runs]
    data["disagreements"] = [d.model_dump() for d in compare(runs[0].result, runs[1].result)]
    return AnalysisReport.model_validate(data)


def quotes_valid(finding, packet: EvidencePacket) -> bool:
    pages = {p.evidence_id: p for p in packet.data.pages}
    docs = {d.evidence_id: d for d in packet.data.documents}
    evidence = {e.id: e for e in packet.data.evidence}
    for q in finding.text_grounding:
        if (
            q.evidence_id not in finding.evidence_ids
            or q.exact_quote != finding.fact
            or evidence[q.evidence_id].source != finding.source
        ):
            return False
        if q.evidence_id in docs:
            text = docs[q.evidence_id].page_texts.get(q.page, "")
        else:
            text = (pages[q.evidence_id].text or "") if q.page in {None, 1} else ""
        if q.exact_quote not in text:
            return False
    return bool(finding.text_grounding)


def commercial(report: AnalysisReport, packet: EvidencePacket) -> tuple[list[str], list[str]]:
    report = checked_report(report, packet)
    reasons = []
    if not report.completed:
        reasons.append("auditor_failed_or_missing")
    if any(e.status != "complete" or not e.available for e in packet.data.evidence):
        reasons.append("incomplete_evidence")
    if (
        not packet.data.evidence
        or any(
            not p.reading.text_coverage_complete or p.reading.provenance != "reproduced"
            for p in [*packet.data.pages, *packet.data.documents]
        )
        or packet.data.warnings
    ):
        reasons.append("incomplete_dossier")
    if any(d.kind != "full_agreement" for d in report.disagreements):
        reasons.append("comparison_requires_review")
    if any(
        c.status != "reviewed" or c.limitations
        for r in report.runs
        if r.result
        for c in r.result.coverage
    ):
        reasons.append("incomplete_topic_coverage")
    allowed = []
    for d in report.disagreements:
        if d.kind != "full_agreement":
            continue
        pair = [
            next(f for f in r.result.findings if f.id == ids[0])
            for r, ids in zip(report.runs, [d.finding_ids_a, d.finding_ids_b], strict=True)
        ]
        if (
            all(
                f.material
                and f.fact_supported
                and f.status == "potential"
                and f.legal_position == "possible_issue"
                and f.evidence_quality == "complete"
                and quotes_valid(f, packet)
                for f in pair
            )
            and pair[0].fact == pair[1].fact
        ):
            allowed.append(pair[0].id)
    if not allowed:
        reasons.append("no_grounded_commercial_basis")
    return sorted(allowed), reasons


def export_proposal(
    root: Path,
    output: Path,
    report: AnalysisReport,
    provider: ProposalProvider,
    *,
    pdf=False,
    font_path=None,
) -> dict:
    """Single guarded generation/export boundary; HOLD cannot invoke provider."""
    report = report.model_copy(deep=True)
    packet = load_packet(root, 200_000)
    allowed, reasons = commercial(report, packet)
    if reasons:
        return {"status": "not_requested", "reasons": reasons}
    request = {
        "packet_sha256": packet.sha256,
        "analysis_sha256": digest(canonical(report.model_dump(mode="json"))),
        "finding_ids": allowed,
    }
    # GO is derived above before constructing any provider messages or client files.
    proposal = Proposal.model_validate(provider.generate(copy.deepcopy(request)))
    if (
        proposal.packet_sha256 != request["packet_sha256"]
        or proposal.analysis_sha256 != request["analysis_sha256"]
        or sorted(proposal.finding_ids) != allowed
    ):
        raise ValueError("Proposal hash or allowed finding mismatch")
    # Detect changes made during generation, before export.
    current = load_packet(root, 200_000)
    if current.sha256 != packet.sha256 or current.manifest_sha256 != packet.manifest_sha256:
        raise ValueError("Dossier changed during generation")
    checked = checked_report(report, current)
    findings = {f.id: f for f in checked.runs[0].result.findings}
    lines = [NOTICE, SERVICE]
    for fid in proposal.finding_ids:
        f = findings[fid]
        lines.append(f"Наблюдение {fid}: {f.fact}")
        for q in f.text_grounding:
            lines.append(f"{q.evidence_id}; {f.source}; стр. {q.page or 1}: {q.exact_quote}")
    # Stage the whole validated bundle, so PDF failures leave no client files.
    with tempfile.TemporaryDirectory(prefix="lexradar048-") as staging:
        stage = Path(staging)
        (stage / "proposal.json").write_bytes(canonical(proposal.model_dump()))
        (stage / "proposal.html").write_text(
            '<!doctype html><meta charset="utf-8"><title>Офлайн-КП</title>'
            + "".join(f"<p>{escape(line)}</p>" for line in lines),
            encoding="utf-8",
        )
        if pdf:
            write_pdf(lines, stage / "proposal.pdf", font_path=font_path)
        binding = document_hash(stage)
        if output.exists():
            raise ValueError("Proposal output must be a new directory")
        output.mkdir(parents=True)
        for path in stage.iterdir():
            (output / path.name).write_bytes(path.read_bytes())
    return {
        "status": "validated_synthetic",
        "document_sha256": binding,
        "approval_status": "pending_untrusted",
        "release_allowed": False,
    }


def run(
    root: Path,
    output: Path,
    responses: dict,
    *,
    pdf=False,
    font_path=None,
    proposal_provider: ProposalProvider | None = None,
) -> dict:
    if output.exists():
        raise ValueError("Run output must be a new directory")
    config = profile()
    packet = load_packet(root, config.max_input_bytes)
    report = analyze(
        root, output / "analysis", config, provider=RecordedAuditors(responses), parallel=True
    )
    allowed, reasons = commercial(report, packet)
    contract = {
        "schema_version": "048",
        "mode": "offline_recorded_or_synthetic",
        "dossier_sha256": packet.manifest_sha256,
        "packet_sha256": packet.sha256,
        "analysis": report.model_dump(mode="json"),
        "commercial": {
            "outcome": "HOLD" if reasons else "GO",
            "reasons": reasons,
            "allowed_findings": allowed,
        },
        "legal_production_status": "HOLD",
        "send_allowed": False,
    }
    try:
        contract["proposal"] = export_proposal(
            root,
            output / "client",
            report,
            proposal_provider or FakeOpenRouterProposal(),
            pdf=pdf,
            font_path=font_path,
        )
    except Exception as exc:
        contract["proposal"] = {"status": "blocked", "error_type": type(exc).__name__}
    (output / "run.json").write_bytes(canonical(contract))
    return contract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dossier", type=Path)
    parser.add_argument("responses", type=Path, help="JSON object with recorded A and B results")
    parser.add_argument("output", type=Path)
    parser.add_argument("--pdf", action="store_true")
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    result = run(
        args.dossier,
        args.output,
        json.loads(args.responses.read_text()),
        pdf=args.pdf,
        font_path=args.font,
    )
    print(
        json.dumps(
            {"commercial": result["commercial"], "proposal": result["proposal"]},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
