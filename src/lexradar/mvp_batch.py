"""CSV collection, offline replay, or separately authorized single paid site."""

import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import time
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from .audit_progress import waiting
from .auditors.budget import Budget
from .auditors.evidence import load_packet
from .auditors.models import AnalysisReport
from .auditors.orchestrator import analyze
from .auditors.preflight import authorize
from .batch import safe_path, snapshot, write_json
from .batch_paid_guard import CappedProvider
from .batch_qualification import analysis_digest, qualify
from .client_proposal import render_client
from .collector.network import CollectionError, canonical_url
from .evidence_review import review
from .policy3 import inspect_policy3
from .single_auditor import MODEL, DeepSeekAdapter, RecordedA, commercial, messages_for, profile


class BatchProgress:
    def __init__(self, rows):
        self.total = len(rows)
        self.completed = sum(row["status"] != "pending" for row in rows)
        self.counts = Counter(row["status"] for row in rows if row["status"] != "pending")
        self.started = time.monotonic()
        self.initial_completed = self.completed
        self.stage = "подготовка"
        print(f"Сайтов в пакете: {self.total}", flush=True)

    def heartbeat(self, _stage):
        elapsed = time.monotonic() - self.started
        processed = self.completed - self.initial_completed
        remaining = (
            f"{elapsed / processed * (self.total - self.completed):.0f} с"
            if processed else "неизвестно"
        )
        percent = self.completed / self.total * 100 if self.total else 100
        print(
            f"Этап: {self.stage}; завершено: {self.completed}/{self.total} "
            f"({percent:.1f}%); SUCCESS: {self.counts['SUCCESS']}; "
            f"SKIPPED: {self.counts['SKIPPED']}; прошло: {elapsed:.0f} с; "
            f"осталось примерно: {remaining}",
            flush=True,
        )

    def rows(self, rows):
        for number, row in enumerate(rows, 1):
            pending = row["status"] == "pending"
            self.stage = f"сайт {number}/{self.total}: подготовка"
            yield row
            if pending and row["status"] != "pending":
                self.completed += 1
                self.counts[row["status"]] += 1
            reason = f"; {row['reason']}" if row["status"] != "SUCCESS" else ""
            print(f"Сайт {number}/{self.total}: {row['status']}{reason}", flush=True)
        self.stage = "сохранение результатов"


def read_csv(path):
    raw = path.read_text(encoding="utf-8-sig")
    if not raw.strip():
        raise ValueError("Empty CSV")
    delimiter = ";" if ";" in raw.splitlines()[0] else ","
    reader = csv.DictReader(io.StringIO(raw), delimiter=delimiter, strict=True)
    if not reader.fieldnames or not {"url", "company_name", "email"} <= set(reader.fieldnames):
        raise ValueError("CSV requires url, company_name, email")
    if len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError("Duplicate CSV headers")
    rows, seen = [], set()
    for number, values in enumerate(reader, 2):
        row = {"line": number, "input": values, "status": "pending", "reason": ""}
        try:
            if None in values or any(v is None for v in values.values()):
                raise ValueError("Column count mismatch")
            url = canonical_url(values["url"].strip())
            row["url"] = url
            if url in seen:
                row.update(status="SKIPPED", reason="duplicate_url")
            seen.add(url)
        except (ValueError, CollectionError) as exc:
            row.update(status="ERROR", reason=str(exc))
        rows.append(row)
    return rows


def proposal(row, report, packet, path, *, demo, allowed_findings=None):
    allowed, reasons = commercial(report, packet)
    if reasons:
        return False
    if allowed_findings is not None:
        allowed = sorted(set(allowed) & set(allowed_findings))
    if not allowed:
        return False

    company = row["input"]["company_name"].strip() or urlsplit(row["url"]).hostname
    topics = {
        "consent": "Оформление согласий",
        "privacy_policy": "Политика обработки данных",
        "cookies": "Использование cookies",
        "operator_identity": "Сведения об операторе",
    }
    findings = [
        {
            "confirmed": True,
            "title": topics.get(finding.topic.value, "Содержание сохранённых материалов"),
            "quote": "\n".join(q.exact_quote for q in finding.text_grounding),
            "meaning": "Проверим контекст и подготовим документы и инструкции вебмастеру "
            "в согласованном объёме.",
        }
        for finding in report.runs[0].result.findings
        if finding.id in allowed
    ]
    if not findings or not prepare_findings(packet):
        return False
    page = render_client(
        {"name": company, "url": row["url"]},
        findings,
        {"date": row["input"].get("date", "").strip() or date.today().strftime("%d.%m.%Y")},
        policy_packet=packet,
    )
    temporary = safe_path(path.with_suffix(".html.tmp"))
    temporary.write_text(page, encoding="utf-8")
    temporary.replace(safe_path(path))
    write_json(safe_path(path.with_name("policy3.json")), inspect_policy3(packet))
    return True


def prepare_findings(packet):
    """Only confirmed deficiency triggers from the saved evidence."""
    from .proposal_rules import evaluate_rules

    return evaluate_rules(packet)


def write_prepared_proposal(row, packet, directory):
    """Offline fixed-rule export; no trigger means no client files."""
    from playwright.sync_api import Error as PDFError

    from .pdf_export import write_html_pdf
    from .operator_audit import clinic_name
    from .policy5 import inspect_policy5
    from .proposal_rules import inspect_form_signals

    findings = prepare_findings(packet)
    review = dict(
        qualification="HOLD", reason="no_confirmed_triggers_1_2_3_5",
        policy5=inspect_policy5(packet),
        form_signals=inspect_form_signals(packet),
        packet_sha256=packet.sha256, findings=findings, manual_approval=None,
        analysis_limitations=list(packet.limitations),
        automatic_send_allowed=False, client_release_allowed=False, sent=False,
        block4_without_evidence=bool(findings), pdf_ready=False,
    )
    if not findings:
        write_json(directory / "review.json", review)
        return []
    name = clinic_name(packet)
    review["clinic_name"] = name
    review["identity_verification_required"] = not bool(name)
    page = render_client(
        {"name": name, "url": row["url"],
         "recipient": "Руководителю " + (name or "клиники")},
        findings,
        {"date": row["input"].get("date", "").strip() or date.today().strftime("%d.%m.%Y")},
        policy_packet=packet,
    )
    (directory / "proposal.html").write_text(page, encoding="utf-8")
    review["reason"] = "manual_review_required_including_unverified_constant_block4"
    try:
        write_html_pdf(page, directory / "proposal.pdf")
        review["pdf_ready"] = True
    except (PDFError, OSError) as exc:
        review["pdf_error"] = str(exc)
    write_json(directory / "review.json", review)
    return findings


def collection_skip_reason(collection):
    errors = dict.fromkeys(
        error
        for evidence in collection.evidence
        for error in evidence.collection_errors
    )
    return "; ".join(errors) or "No artifacts captured"


def export(root, state):
    columns = [
        "line",
        "url",
        "company_name",
        "email",
        "status",
        "qualification",
        "reason",
        "directory",
        "dossier",
        "response",
        "proposal",
        "cost_usd",
        "budget_charge_usd",
        "cost_known",
        "historical_cost_usd",
        "ai_calls",
        "mode",
        "html_pages_attempted",
        "html_pages_captured",
    ]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for row in state["rows"]:
        writer.writerow({k: row.get(k, row["input"].get(k, "")) for k in columns})
    temporary = safe_path(root / "results.csv.tmp")
    temporary.write_text(buffer.getvalue(), encoding="utf-8-sig")
    temporary.replace(safe_path(root / "results.csv"))
    counts = dict(Counter(r["status"] for r in state["rows"]))
    state["summary"] = counts
    state["qualification_summary"] = dict(
        Counter(r.get("qualification", "unevaluated") for r in state["rows"])
    )
    write_json(root / "manifest.json", state)
    page_stats = (
        f"HTML pages attempted/captured: {sum(r.get('html_pages_attempted', 0) for r in state['rows'])}/"
        f"{sum(r.get('html_pages_captured', 0) for r in state['rows'])}. Express screening targets 5 pages and caps at 7.\n\n"
        if state["mode"] == "prepare"
        else ""
    )
    safe_path(root / "report.md").write_text(
        f"# {state['mode'].upper()} batch\n\n"
        f"Model: {MODEL}; Qwen B disabled.\n\n{json.dumps(counts)}\n\n"
        f"Qualification: {json.dumps(state['qualification_summary'])}\n\n"
        f"{page_stats}"
        "See results.csv and sites/. GO is commercial qualification, not legal clearance.\n"
        "Automatic sending prohibited. Offline cost: 0 USD. Paid limit: 0.50 USD.\n",
        encoding="utf-8",
    )


def run(
    csv_path,
    root,
    *,
    mode="offline",
    max_sites=5,
    resume=False,
    allow_paid_single=False,
    allow_external_transfer=False,
    packet_approval=None,
    mvp_no_screening=False,
    allow_site_collection=False,
    ignore_robots=True,
    max_ai_calls=1,
    max_total_budget_usd="0.50",
    timeout_seconds=45,
):
    config = profile(timeout_seconds=timeout_seconds)
    try:
        total_budget = Decimal(str(max_total_budget_usd))
    except InvalidOperation as exc:
        raise ValueError("Invalid total budget") from exc
    if not total_budget.is_finite() or not 0 < total_budget <= Decimal("0.50"):
        raise ValueError("Total budget must be in (0, 0.50]")
    if type(max_ai_calls) is not int or not 0 <= max_ai_calls <= 2:
        raise ValueError("max-ai-calls must be 0..2")
    if mode not in {"offline", "saved", "paid", "prepare"} or not 1 <= max_sites <= 1000:
        raise ValueError("Invalid mode or site limit")
    if mode == "paid" and not (
        allow_paid_single and allow_external_transfer and packet_approval and max_sites == 1
    ):
        raise PermissionError(
            "NEEDS_APPROVAL: paid batch requires single-site limit and separate packet permission"
        )
    if mode == "prepare" and not allow_site_collection:
        raise PermissionError("Collection requires explicit --allow-site-collection")
    if mode != "paid" and mvp_no_screening:
        raise ValueError("Screening override only applies to explicit paid execution")
    root = safe_path(root)
    plan = {
        "csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "mode": mode,
        "max_sites": max_sites,
        "model": MODEL,
        "mvp_no_screening": mvp_no_screening,
        "allow_site_collection": allow_site_collection,
        "collection_profile": "express-screening-144" if mode == "prepare" else None,
        "max_ai_calls": max_ai_calls,
        "timeout_seconds": timeout_seconds,
        "max_total_budget_usd": str(total_budget),
    }
    if ignore_robots:
        plan["ignore_robots"] = True
    if resume:
        state = json.loads(safe_path(root / "manifest.json").read_text())
        if state["plan"] != plan:
            raise ValueError("Resume input/options changed; use a new output directory")

        original = read_csv(csv_path)
        if len(original) != len(state["rows"]):
            raise ValueError("Resume row count changed")
        for row, expected in zip(state["rows"], original, strict=True):
            if any(row.get(k) != expected.get(k) for k in ("input", "line", "url")):
                raise ValueError("Resume row identity changed")
            if mode == "paid" and row["status"] == "pending" and row.get("directory"):
                raise PermissionError("Paid resume prohibited for uncertain attempt; inspect costs")
            if row.get("directory") and not re.fullmatch(
                rf"sites/row-{row['line']:06d}(?:-retry-[1-9][0-9]*)?", row["directory"]
            ):
                raise ValueError("Unsafe resume directory")
            if row.get("files") and snapshot(safe_path(root / row["directory"])) != row["files"]:
                raise ValueError("Saved output integrity changed")
    else:
        rows = read_csv(csv_path)
        root.mkdir(parents=True, exist_ok=False)
        state = {
            "schema": "mvp-batch-063",
            "DEMO": mode == "offline",
            "plan": plan,
            "mode": mode,
            "rows": rows,
            "automatic_send_allowed": False,
            "client_release_allowed": False,
        }
    # A lock also prevents simultaneous resumptions from duplicating work.
    import fcntl

    progress = BatchProgress(state["rows"])
    with safe_path(root / ".lock").open("a") as lock, waiting(progress.heartbeat, interval=30):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if resume and json.loads((root / "manifest.json").read_text()) != state:
            raise ValueError("Resume state changed while acquiring lock; retry safely")
        used = sum(
            r["status"] != "pending" and bool(r.get("directory")) for r in state["rows"]
        )
        export(root, state)
        for row in progress.rows(state["rows"]):
            if row["status"] != "pending":
                continue
            if used >= max_sites:
                row.update(status="SKIPPED", reason="site_limit")
                continue
            used += 1
            directory = root / "sites" / f"row-{row['line']:06d}"
            # Never overwrite files left by an interrupted attempt.
            attempt = 1
            while directory.exists():
                directory = root / "sites" / f"row-{row['line']:06d}-retry-{attempt}"
                attempt += 1
            directory.mkdir(parents=True)
            for key in ("proposal", "qualification", "files", "cost_known", "budget_charge_usd"):
                row.pop(key, None)
            row.update(
                directory=str(directory.relative_to(root)), mode=mode, stages=[], cost_usd="0"
            )
            export(root, state)
            paid_provider = None
            try:
                if mode == "prepare":
                    from .collector import CrawlOptions, Limits, collect

                    progress.stage = "сбор материалов"
                    collection = collect(
                        row["url"],
                        directory / "dossier",
                        Limits(max_pages=7, max_documents=2),
                        static_only=True,
                        screening_mode=True,
                        crawl_options=CrawlOptions(
                            max_requests=16, max_duration_seconds=60, ignore_robots=ignore_robots
                        ),
                    )
                    evidence_by_id = {e.id: e for e in collection.evidence}
                    row["html_pages_attempted"] = len(collection.pages)
                    row["html_pages_captured"] = sum(
                        any(a.kind == "html" for a in evidence_by_id[p.evidence_id].artifacts)
                        for p in collection.pages
                    )
                    if not any(evidence.available for evidence in collection.evidence):
                        row.update(
                            status="SKIPPED",
                            qualification="HOLD",
                            reason=collection_skip_reason(collection),
                        )
                        row["files"] = snapshot(directory)
                        export(root, state)
                        continue
                else:
                    progress.stage = "проверка и копирование досье"
                    source = safe_path(Path(row["input"].get("dossier", "")))
                    if root.resolve().is_relative_to(source.resolve()):
                        raise ValueError("Output must be outside the source dossier")
                    snapshot(source)  # Reject symlinks and oversized trees before copying.
                    packet = load_packet(source, profile().max_input_bytes)
                    if canonical_url(str(packet.data.target_url)) != row["url"]:
                        raise ValueError("Dossier URL mismatch")
                    shutil.copytree(source, directory / "dossier")
                progress.stage = "проверка пакета доказательств"
                packet = load_packet(directory / "dossier", 20_000_000 if mode == "prepare" else profile().max_input_bytes)
                if not any(e.available for e in packet.data.evidence):
                    raise ValueError("No usable evidence; not proof of absence")
                row["stages"] += ["collection:local_verified", "evidence_packet:SUCCESS"]
                write_json(
                    directory / "packet.json",
                    {
                        "sha256": packet.sha256,
                        "manifest_sha256": packet.manifest_sha256,
                        "payload": packet.payload,
                    },
                )
                row["dossier"] = str((directory / "dossier").absolute())
                if mode == "prepare":
                    progress.stage = "подготовка КП и PDF"
                    findings = write_prepared_proposal(row, packet, directory)
                    if not findings:
                        row.update(status="SKIPPED", qualification="HOLD",
                                   reason="no_confirmed_triggers_1_2_3_5")
                        row["stages"].append("proposal:SKIPPED")
                        row["files"] = snapshot(directory)
                        export(root, state)
                        continue
                    row["proposal"] = str((directory / "proposal.html").relative_to(root))
                    row["stages"].append("proposal:SUCCESS")
                    row.update(
                        status="SUCCESS",
                        qualification="HOLD",
                        reason="manual_review_required_including_unverified_constant_block4",
                    )
                    row["files"] = snapshot(directory)
                    export(root, state)
                    continue
                progress.stage = "подготовка анализа"
                kwargs = {}
                saved_report = None
                if mode == "saved":
                    source_report = safe_path(Path(row["input"]["analysis"]))
                    saved_report = AnalysisReport.model_validate_json(source_report.read_bytes())
                    from .single_auditor import checked_report

                    checked_report(saved_report, packet)
                    if (
                        saved_report.mode != "openrouter"
                        or not saved_report.logs
                        or any(
                            log.model != MODEL or log.auditor != "A" or not log.request_sent
                            for log in saved_report.logs
                        )
                    ):
                        raise ValueError("Saved mode requires a recorded real Single A analysis")
                    (directory / "analysis").mkdir()
                    write_json(
                        directory / "analysis" / "analysis.json",
                        saved_report.model_dump(mode="json"),
                    )
                    row["response"] = str(source_report)
                if mode == "offline":
                    response = json.loads(safe_path(Path(row["input"]["response"])).read_text())
                    kwargs["provider"] = RecordedA(response)
                elif mode == "paid":
                    if not Path(packet_approval).is_file():
                        raise PermissionError("NEEDS_APPROVAL: packet approval file missing")
                    authorize(packet, packet_approval, mvp_no_screening=mvp_no_screening)
                    if max_ai_calls == 0:
                        raise PermissionError("NEEDS_APPROVAL: max-ai-calls is zero")
                    Budget(total_budget).reserve(profile().auditor_a, messages_for("A", packet)[0])
                    adapter = (
                        DeepSeekAdapter()
                        if timeout_seconds == 45
                        else DeepSeekAdapter(timeout_seconds=timeout_seconds)
                    )
                    paid_provider = CappedProvider(adapter, directory, max_ai_calls, total_budget)
                    kwargs["provider"] = paid_provider
                    kwargs.update(
                        mode="openrouter",
                        allow_external_transfer=True,
                        packet_approval=packet_approval,
                        mvp_no_screening=mvp_no_screening,
                    )
                progress.stage = "анализ"
                report = saved_report or analyze(
                    directory / "dossier",
                    directory / "analysis",
                    config,
                    message_builder=messages_for,
                    **kwargs,
                )
                row["signals"] = sum(
                    len(run.result.findings) for run in report.runs if run.result is not None
                )
                row["stages"].append("DeepSeek_A:" + ("SUCCESS" if report.completed else "ERROR"))
                local_legal_review = None
                if mode in {"paid", "saved"} and row["input"].get("legal_review"):
                    legal_source = safe_path(Path(row["input"]["legal_review"]))
                    if legal_source.stat().st_size > 100_000:
                        raise ValueError("Oversized legal review")
                    local_legal_review = directory / "independent-legal-review.json"
                    write_json(local_legal_review, json.loads(legal_source.read_bytes()))
                progress.stage = "проверка результатов"
                allowed, reasons = qualify(
                    report,
                    packet,
                    production=mode in {"paid", "saved"},
                    legal_review=local_legal_review,
                )
                if mode in {"paid", "saved"}:
                    write_json(
                        directory / "legal-review-template.json",
                        {
                            "packet_sha256": packet.sha256,
                            "analysis_sha256": analysis_digest(report),
                            "reviewer": "",
                            "reviewed_at": None,
                            "findings": [],
                        },
                    )
                if not allowed and not reasons:
                    reasons.append("no_grounded_serviceable_material_finding")
                if not report.completed:
                    reasons += [run.status for run in report.runs]
                row["historical_cost_usd"] = str(
                    sum((log.usage.cost_usd or 0) for log in report.logs)
                )
                row["cost_usd"] = "0" if mode == "saved" else row["historical_cost_usd"]
                row["budget_charge_usd"] = (
                    "0" if mode == "saved" else str(report.budget_charged_usd)
                )
                row["ai_calls"] = paid_provider.calls if paid_provider else 0
                if paid_provider:
                    row["budget_charge_usd"] = str(paid_provider.budget.charged)
                row["cost_known"] = mode in {"offline", "saved"} or all(
                    log.usage.cost_usd is not None for log in report.logs
                )
                if not row["cost_known"]:
                    row["cost_usd"] = "unknown"
                row.update(
                    status="SUCCESS" if report.completed else "ERROR",
                    qualification="HOLD" if reasons else "GO",
                    reason="; ".join(reasons),
                )
                review(
                    report,
                    packet,
                    directory,
                    qualification=(allowed, reasons),
                    new_ai_calls=row["ai_calls"],
                    new_cost_usd=row["cost_usd"],
                )
                row["stages"].append("qualification:" + row["qualification"])
                progress.stage = "подготовка КП"
                if not reasons and proposal(
                    row,
                    report,
                    packet,
                    directory / "proposal.html",
                    demo=mode == "offline",
                    allowed_findings=allowed,
                ):
                    row["proposal"] = str((directory / "proposal.html").relative_to(root))
                    row["stages"].append("proposal:SUCCESS")
            except Exception as exc:
                if paid_provider and paid_provider.calls:
                    row.update(
                        cost_usd="unknown",
                        cost_known=False,
                        ai_calls=paid_provider.calls,
                        budget_charge_usd=str(paid_provider.budget.charged),
                    )
                row.update(
                    status="NEEDS_APPROVAL" if isinstance(exc, PermissionError) else "ERROR",
                    qualification="HOLD",
                    reason=f"{type(exc).__name__}: {str(exc)[:500]}",
                )
            row["files"] = snapshot(directory)
            export(root, state)
        export(root, state)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=["offline", "saved", "paid", "prepare"], default="offline"
    )
    parser.add_argument("--max-sites", type=int, default=5)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, choices=(45, 120), default=45)
    parser.add_argument("--max-ai-calls", type=int, default=1)
    parser.add_argument("--max-total-budget-usd", default="0.50")
    parser.add_argument("--allow-paid-single", action="store_true")
    parser.add_argument("--allow-external-transfer", action="store_true")
    parser.add_argument("--packet-approval", type=Path)
    parser.add_argument("--mvp-no-screening", action="store_true")
    parser.add_argument("--allow-site-collection", action="store_true")
    parser.add_argument(
        "--ignore-robots", action="store_true", default=True,
        help="Skip robots.txt gate (default); retain request and access limits",
    )
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        state = run(
            args.csv,
            args.output,
            mode=args.mode,
            max_sites=args.max_sites,
            resume=args.resume,
            max_ai_calls=args.max_ai_calls,
            timeout_seconds=args.timeout_seconds,
            max_total_budget_usd=args.max_total_budget_usd,
            allow_paid_single=args.allow_paid_single,
            allow_external_transfer=args.allow_external_transfer,
            packet_approval=args.packet_approval,
            mvp_no_screening=args.mvp_no_screening,
            allow_site_collection=args.allow_site_collection,
            ignore_robots=args.ignore_robots,
        )
    except (ValueError, OSError, PermissionError, csv.Error) as exc:
        parser.exit(1, f"Batch stopped safely: {exc}\n")
    selected = [row for row in state["rows"] if row.get("directory")]
    successful = [row for row in selected if row["status"] == "SUCCESS"]
    skipped = [row for row in state["rows"] if row["status"] == "SKIPPED"]
    errors = [row for row in selected if row["status"] == "ERROR"]
    skip_reasons = Counter(row.get("reason") or "unspecified" for row in skipped)
    signals = (
        str(sum(row.get("signals", 0) for row in selected))
        if any("signals" in row for row in selected)
        else "не оценивались"
    )
    print(json.dumps(state["summary"], ensure_ascii=False), flush=True)
    print(
        f"Выбрано: {len(selected)}; "
        f"успешно обследовано: {len(successful)}; "
        f"пропущено: {len(skipped)}; ошибок: {len(errors)}; "
        f"сигналов: {signals}; "
        f"черновиков: {sum(bool(row.get('proposal')) for row in selected)}; "
        f"время: {time.monotonic() - started:.1f} с",
        flush=True,
    )
    print(
        "Причины пропуска: "
        + (json.dumps(skip_reasons, ensure_ascii=False) if skip_reasons else "нет"),
        flush=True,
    )
    print(
        f"Отчёты: {args.output}/report.md; {args.output}/results.csv; "
        f"{args.output}/manifest.json",
        flush=True,
    )


if __name__ == "__main__":
    main()
