"""Operator workflow: bounded collection, packet review, one paid attempt or saved replay."""

import argparse
import html
import json
import os
import re
import shutil
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .audit_progress import Progress
from .auditors.budget import Budget
from .auditors.evidence import DossierError, load_packet
from .auditors.grounding import quote_locations
from .auditors.models import AIAuditorResult, AnalysisReport
from .auditors.orchestrator import analyze
from .auditors.preflight import PacketApproval, authorize, screen, write_preflight
from .auditors.provider import ProviderError
from .auditors.validator import validate_result
from .batch import safe_path, snapshot
from .client_proposal import document, render_client
from .collector import CrawlOptions, Limits, collect
from .collector.network import canonical_url
from .cookie_inspection import inspect_saved
from .finding_assessment import CommercialReview, evaluate, review_template
from .single_auditor import DeepSeekAdapter, messages_for, profile


def save(path, value):
    from .audit_capture import private_write

    private_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare(source, output):
    """Whole-source selection with explicit omissions; copied originals never rewritten."""
    source, output = safe_path(source), safe_path(output)
    before = snapshot(source)
    full = load_packet(source, 20_000_000)
    shutil.copytree(source, output)
    save(output.parent / "source-integrity.json", before)
    try:
        packet = load_packet(output, 200_000)
    except DossierError as exc:
        if "exceeds input limit" not in str(exc):
            raise
        data = full.data.model_dump(mode="json")
        records = [
            ("pages", p.evidence_id, str(p.final_url or p.requested_url)) for p in full.data.pages
        ]
        records += [
            ("documents", d.evidence_id, str(d.final_url or d.requested_url))
            for d in full.data.documents
        ]

        def priority(record):
            url = record[2].lower()
            for i, marker in enumerate(
                (
                    r"politik|privacy",
                    r"soglas|consent",
                    r"rekviz|operator",
                    r"licen|liczen",
                    r"zapis|appointment",
                    r"contact|kontakt",
                )
            ):
                if re.search(marker, url):
                    return i, url
            return 6, url

        chosen = set()
        data["entities"] = []
        data["warnings"].append(
            "Explicit whole-source subset; omitted pages/documents "
            "are not examined and absence is not proven. See selection.json."
        )
        manifest = output / "collection.json"
        for _, eid, _ in sorted(records, key=priority):
            candidate = chosen | {eid}
            subset = dict(data)
            subset["pages"] = [p for p in data["pages"] if p["evidence_id"] in candidate]
            subset["documents"] = [d for d in data["documents"] if d["evidence_id"] in candidate]
            subset["evidence"] = [e for e in data["evidence"] if e["id"] in candidate]
            manifest.write_text(json.dumps(subset, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                load_packet(output, 200_000)
            except DossierError as exc:
                if "exceeds input limit" not in str(exc):
                    raise
            else:
                chosen = candidate
        if not chosen:
            raise ValueError(
                "No whole source fits the packet limit; do not silently truncate"
            ) from None
        data["pages"] = [p for p in data["pages"] if p["evidence_id"] in chosen]
        data["documents"] = [d for d in data["documents"] if d["evidence_id"] in chosen]
        data["evidence"] = [e for e in data["evidence"] if e["id"] in chosen]
        manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        packet = load_packet(output, 200_000)
    selected = {e.id for e in packet.data.evidence}
    save(
        output.parent / "selection.json",
        dict(
            selected=sorted(selected),
            excluded=[e.id for e in full.data.evidence if e.id not in selected],
            full_packet_bytes=len(full.payload.encode()),
            packet_bytes=len(packet.payload.encode()),
            limitation="Whole-page legal priority; selection does not establish completeness.",
        ),
    )
    if not any(e.available for e in packet.data.evidence):
        raise ValueError("No available evidence; collection failed, paid execution prohibited")
    if snapshot(source) != before:
        raise ValueError("Source changed during preparation")
    return packet


def replay(audit, dossier, output):
    audit = safe_path(audit)
    before = snapshot(audit)
    historical = AnalysisReport.model_validate_json((audit / "analysis.json").read_bytes())
    packet = load_packet(dossier, 200_000)
    if (
        historical.evidence_packet_sha256 != packet.sha256
        or historical.collector_manifest_sha256 != packet.manifest_sha256
    ):
        raise ValueError("Saved analysis/packet binding mismatch")
    originals = sorted(audit.glob("capture-A-*/original.txt"))
    if not originals:
        raise ValueError("Original response required; normalized JSON cannot restore lost text")
    # Preserve capture and ledger; never disguise historical usage as a new charge.
    shutil.copytree(audit, output)
    content = originals[-1].read_text(encoding="utf-8")
    raw = AIAuditorResult.model_validate_json(content)
    result, notes = validate_result(
        content,
        "A",
        packet,
        require_cookie_review=historical.runs[0].prompt_version == "single-a-v3-cookie",
    )
    report = historical.model_copy(deep=True)
    report.runs[0].result = result
    report.runs[0].validation_notes = notes
    # Retain the historical bytes separately; the new derivative has new validation notes.
    (output / "historical-analysis.json").write_bytes((audit / "analysis.json").read_bytes())
    (output / "analysis.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    if snapshot(audit) != before:
        raise ValueError("Saved audit changed during replay")
    return report, raw


def clinic_name(packet, requested=""):
    # shortcut: recognize explicit ООО identity fields; extend for other legal forms when needed.
    names = {}
    official_ids = set()
    for page in packet.data.pages:
        for match in re.finditer(
            r'Общая\s+информация\s+о\s+медицинской\s+организации\s*[:—-]?\s*'
            r'(?:ООО|Общество\s+с\s+ограниченной\s+ответственностью)\s+'
            r'(?:«\s*([^»\r\n]{2,120}?)\s*»|"\s*([^"\r\n]{2,120}?)\s*")'
            r'\s+ОГРН\s*:?\s*(\d{13})(?!\d)',
            page.text or "",
            re.IGNORECASE,
        ):
            if any(m["evidence_id"] == page.evidence_id for m in quote_locations(packet, match[0])):
                name = (match[1] or match[2]).strip()
                names[name] = f"ООО «{name}»"
                official_ids.add((name, match[3]))
        for match in re.finditer(
            r"(?:Полное|Сокращ[её]нное)\s+наименование(?:\s+(?:фирмы|организации))?\s*[:—-]?\s*"
            r"((?:ООО|Общество с ограниченной ответственностью)\s+«([^»\r\n]{2,120})»)",
            page.text or "",
            re.IGNORECASE,
        ):
            if any(m["evidence_id"] == page.evidence_id for m in quote_locations(packet, match[0])):
                names[match[2]] = match[1]
    if not names:
        for page in packet.data.pages:
            text = page.text or ""
            sections = re.split(
                r"Общая\s+информация\s+о\s+медицинской\s+организации",
                text, flags=re.IGNORECASE,
            )[1:]
            for section in sections:
                ogrn = re.search(r"ОГРН\s*[:№—-]?\s*(\d{13})(?!\d)", section, re.IGNORECASE)
                if not ogrn:
                    continue
                candidates = list(re.finditer(
                    r'(?:ООО|Общество\s+с\s+ограниченной\s+ответственностью)\s*'
                    r'[:—-]?\s*[«"“]\s*([^»"”]{2,120}?)\s*[»"”]',
                    section[:ogrn.start()], re.IGNORECASE,
                ))
                if not candidates:
                    continue
                match = candidates[-1]
                if any(m["evidence_id"] == page.evidence_id
                       for m in quote_locations(packet, section[match.start():ogrn.end()])):
                    name = " ".join(match[1].split())
                    names[name] = f"ООО «{name}»"
                    official_ids.add((name, ogrn[1]))
    if len(names) != 1 or len(official_ids) > 1:
        return ""
    name, full = next(iter(names.items()))
    short = f"ООО «{name}»"
    if requested.strip():
        return (
            requested.strip()
            if requested.strip() in {name, full, short}
            and quote_locations(packet, requested.strip())
            else ""
        )
    return short if official_ids or quote_locations(packet, short) else full


def finalize(
    report,
    raw,
    packet,
    output,
    *,
    company="",
    review_path=None,
    replayed=False,
    commercial_mvp=False,
):
    if (
        report.evidence_packet_sha256 != packet.sha256
        or report.collector_manifest_sha256 != packet.manifest_sha256
    ):
        raise ValueError("Report/packet binding mismatch")
    review = None
    if review_path:
        review_path = safe_path(review_path)
        if review_path.stat().st_size > 100_000:
            raise ValueError("Oversized commercial review")
        review = CommercialReview.model_validate_json(review_path.read_bytes())
    decision = evaluate(raw, packet, report, review, commercial_mvp=commercial_mvp)
    save(output / "assessment.json", decision)
    save(output / "policy3.json", decision["policy3"])
    save(output / "commercial-review-template.json", review_template(report, packet))
    billable_logs = [log for log in report.logs if log.request_sent]
    save(
        output / "summary.json",
        dict(
            outcome=decision["outcome"],
            commercial_mode=decision["commercial_mode"],
            audit_completed=report.completed,
            audit_status=report.runs[0].status,
            request_outcomes=[log.status for log in report.logs],
            elapsed_seconds=sum(log.duration_seconds for log in report.logs),
            possible_charge_unknown=any(log.usage.cost_usd is None for log in billable_logs),
            reasons=decision["reasons"],
            allowed_findings=decision["allowed_findings"],
            proposal_findings=decision["proposal_findings"],
            supporting_findings=decision["supporting_findings"],
            new_ai_calls=0 if replayed else sum(log.request_sent for log in report.logs),
            new_ai_cost_usd="0"
            if replayed
            else (
                str(sum((log.usage.cost_usd for log in billable_logs), Decimal(0)))
                if all(log.usage.cost_usd is not None for log in billable_logs)
                else None
            ),
            historical_or_current_usage=[log.usage.model_dump(mode="json") for log in report.logs],
            reserved_or_charged_usd=str(report.budget_charged_usd),
            automatic_send_allowed=False,
            client_release_allowed=False,
            manual_approval_required=True,
            manual_approval=None,
            sent=False,
        ),
    )
    sections = []
    findings = {f.id: f for f in raw.findings} if raw else {}
    for row in decision["findings"]:
        f = findings[row["id"]]
        sections.append(
            f"## {f.id}\n\nФакт AI: {f.fact}\n\n"
            f"Факты: {row['fact_status']}; класс: {row['classification']}; "
            f"право: {row['legal_status']}.\n\n"
            f"Гипотеза AI (не правовое заключение): {f.legal_interpretation}\n\n"
            + json.dumps(row, ensure_ascii=False, indent=2)
        )
    approval_reason = (
        "commercial_mvp_criteria_met" if commercial_mvp else "documented_service_need_approved"
    )
    if decision["outcome"] == "GO":
        next_steps = "Проверить подготовленное КП перед выпуском клиенту."
    elif commercial_mvp:
        next_steps = "Подтвердить существенный факт точной цитатой и описать предлагаемую услугу."
    else:
        next_steps = (
            "Проверить ошибочные/чужие цитаты; подтвердить конкретный предмет услуги "
            "в коммерческом обзоре, проверить применимость норм и динамику cookie."
        )
    text = (
        f"# LexRadar: {decision['outcome']}\n\n"
        f"Коммерческий режим: {decision['commercial_mode']}.\n"
        f"Причины: {', '.join(decision['reasons']) or approval_reason}.\n"
        f"Результат AI: {report.runs[0].status}.\n\n"
        "Цитата доказывает текст, а не все выводы модели. Отправка клиенту запрещена.\n\n"
        + "\n\n".join(sections)
        + "\n\nСледующие действия: "
        + next_steps
    )
    from .audit_capture import private_write

    private_write(output / "report.md", text)
    private_write(output / "report.html", document("<pre>" + html.escape(text) + "</pre>"))
    selected = {row["id"]: row for row in decision["findings"]}
    if commercial_mvp:
        justification = dict(
            status="draft",
            outcome=decision["outcome"],
            policy3=decision["policy3"],
            packet_sha256=packet.sha256,
            analysis_sha256=decision["analysis_sha256"],
            findings=[
                dict(
                    finding_id=fid,
                    role=selected[fid]["proposal_role"],
                    fact_status=selected[fid]["fact_status"],
                    legal_violation_proven=False,
                    **selected[fid]["commercial_offer"],
                )
                for fid in decision["proposal_findings"]
            ],
            manual_approval_required=True,
            manual_approval=None,
            automatic_send_allowed=False,
            client_release_allowed=False,
            sent=False,
        )
        save(output / "commercial-justification.json", justification)
        parts = [
            "# Проект коммерческого обоснования",
            "Нарушение не установлено. Отправка запрещена.",
        ]
        for item in justification["findings"]:
            role = "Основание GO" if item["role"] == "primary" else "Предположение для проверки"
            parts.append(
                f"## {item['finding_id']}: {role}\n\n"
                f"Подтверждённое текстовое наблюдение:\n{item['factual_scope']}\n\n"
                f"Возможный риск: {item['risk_reason']}\n\n"
                f"Предлагаемая услуга: {'; '.join(item['service_description'])}.\n\n"
                f"Результат для клиента: {'; '.join(item['deliverables'])}."
            )
        private_write(output / "commercial-justification.md", "\n\n".join(parts) + "\n")
    if decision["outcome"] == "GO":
        cards = []
        for fid in decision["proposal_findings"]:
            row = selected[fid]
            reviewed = row["commercial_offer"] if commercial_mvp else row["operator_review"]
            indices = reviewed["quote_indices"]
            if commercial_mvp:
                title = reviewed["client_title"]
                meaning = reviewed["client_meaning"]
            else:
                title = findings[fid].subject
                meaning = (
                    (
                        "Нарушение независимо проверено. "
                        if row["legal_violation_proven"]
                        else "Нарушение не установлено; предмет дополнительной проверки. "
                    )
                    + reviewed["factual_scope"]
                    + " "
                    + reviewed["risk_reason"]
                    + " Работы: "
                    + "; ".join(reviewed["deliverables"])
                )
            cards.append(
                dict(
                    confirmed=True,
                    title=title,
                    quote="\n".join(findings[fid].text_grounding[i].exact_quote for i in indices),
                    meaning=meaning,
                    show_quote=not (commercial_mvp and row["proposal_role"] == "hypothesis"),
                )
            )
        private_write(
            output / "proposal.html",
            render_client(
                {"name": clinic_name(packet, company), "url": str(packet.data.target_url)},
                cards,
                {"date": datetime.now(UTC).strftime("%d.%m.%Y")},
                policy_packet=packet,
                policy_review=review,
            ),
        )
    return decision


def audit_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dossier", type=Path)
    parser.add_argument("--saved-audit", type=Path)
    parser.add_argument("--commercial-review", type=Path)
    parser.add_argument(
        "--commercial-mvp",
        action="store_true",
        help="Prepare a proposal from one material exact fact and service, without legal proof",
    )
    parser.add_argument("--company", default="")
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument(
        "--cookie-check",
        action="store_true",
        help="Optional saved-HTML inspection, not dynamic browser proof",
    )
    parser.add_argument("--paid", action="store_true")
    parser.add_argument("--allow-external-transfer", action="store_true")
    parser.add_argument("--mvp-no-screening", action="store_true")
    args = parser.parse_args(argv)
    if args.saved_audit and (args.paid or not args.dossier):
        parser.error("Saved replay requires --dossier and prohibits --paid")
    if args.paid and not args.allow_external_transfer:
        parser.error("--paid requires --allow-external-transfer and interactive confirmation")
    os.umask(0o077)
    output = safe_path(args.output)
    if output.exists():
        parser.error("Output must be a new directory; existing runs are never overwritten")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    progress = Progress(output, stream=sys.stdout)
    progress("started")
    try:
        url = canonical_url(args.url)
        with progress.stage("collection_or_dossier_check"):
            source = args.dossier
            if source is None:
                source = output / "collection"
                collect(
                    url,
                    source,
                    Limits(max_pages=40, max_documents=20, timeout_seconds=15),
                    crawl_options=CrawlOptions(max_requests=100, max_duration_seconds=600),
                    static_only=args.static_only,
                )
        with progress.stage("packet_preparation"):
            packet = prepare(source, output / "dossier")
            if canonical_url(str(packet.data.target_url)) != url:
                raise ValueError("Dossier target does not match requested site")
            write_preflight(packet, output / "preflight")
            config = profile()
            messages, version, _ = messages_for("A", packet)
            reserve = Budget(config.max_budget_usd).reserve(config.auditor_a, messages)
            progress(
                "packet_ready",
                packet_bytes=len(packet.payload.encode()),
                model=config.auditor_a.model,
                prompt=version,
                reservation_usd=str(reserve),
            )
        if args.cookie_check:
            with progress.stage("cookie_static_inspection"):
                save(output / "cookie-inspection.json", inspect_saved(source))
        if args.saved_audit:
            with progress.stage("saved_response_revalidation"):
                report, raw = replay(args.saved_audit, output / "dossier", output / "audit")
        elif args.paid:
            # Read only the documented environment variable; never discover secret files.
            if not os.environ.get("OPENROUTER_API_KEY"):
                raise ProviderError("missing_or_invalid_api_key")
            if screen(packet) and not args.mvp_no_screening:
                raise PermissionError("Sensitive-data screening blocks transfer; inspect preflight")
            print("Review preflight/packet.json locally. Approval binds SHA-256:", packet.sha256)
            phrase = "РАЗРЕШАЮ ОДИН ЗАПРОС 0.50 USD " + packet.sha256
            if args.mvp_no_screening:
                phrase += " БЕЗ СКРИНИНГА ПДн"
            with open("/dev/tty", "r+") as tty:
                tty.write("Имя согласующего: ")
                tty.flush()
                reviewer = tty.readline().strip()
                tty.write("Подтвердите просмотр пакета и передачу OpenRouter:\n" + phrase + "\n")
                tty.flush()
                confirmed = tty.readline().strip()
            if not reviewer or confirmed != phrase:
                raise PermissionError("Paid request cancelled")
            approval = PacketApproval(
                packet_sha256=packet.sha256,
                approved=True,
                contents_reviewed=True,
                reviewer=reviewer,
                approved_at=datetime.now(UTC),
            )
            save(output / "approval.json", approval.model_dump(mode="json"))
            authorize(packet, output / "approval.json", mvp_no_screening=args.mvp_no_screening)
            with progress.stage("ai_audit"):
                report = analyze(
                    output / "dossier",
                    output / "audit",
                    config,
                    mode="openrouter",
                    allow_external_transfer=True,
                    packet_approval=output / "approval.json",
                    provider=DeepSeekAdapter(),
                    message_builder=messages_for,
                    mvp_no_screening=args.mvp_no_screening,
                    progress=progress,
                )
            originals = sorted((output / "audit").glob("capture-A-*/original.txt"))
            raw = None
            if originals and report.runs[0].result is not None:
                raw = AIAuditorResult.model_validate_json(originals[-1].read_bytes())
        else:
            progress("prepared_no_paid_request", screening_flags=len(screen(packet)))
            return
        with progress.stage("commercial_assessment_and_report"):
            decision = finalize(
                report,
                raw,
                packet,
                output,
                company=args.company,
                review_path=args.commercial_review,
                replayed=bool(args.saved_audit),
                commercial_mvp=args.commercial_mvp,
            )
        progress("finished", outcome=decision["outcome"])
    except Exception as exc:
        # Arbitrary browser/network exception messages may contain material or credentials.
        safe_codes = {
            "missing_or_invalid_api_key",
            "model_refusal",
            "transport_error",
            "transport_timeout",
            "incomplete_completion",
            "budget_exceeded",
        }
        code = str(exc) if str(exc) in safe_codes else type(exc).__name__
        progress("failed", error_type=type(exc).__name__, code=code)
        sent = any(
            json.loads(line)["stage"] == "request_sending"
            for line in (output / "events.jsonl").read_text().splitlines()
        )
        save(
            output / "error.json",
            dict(
                error_type=type(exc).__name__,
                code=code,
                retry=False,
                outcome="HOLD",
                new_request_allowed=False,
                possible_charge_unknown=sent,
            ),
        )
        print("HOLD: см. error.json и events.jsonl. Автоматического повтора нет.", file=sys.stderr)
        raise SystemExit(1) from None
