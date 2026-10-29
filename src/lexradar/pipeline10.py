"""Sequential Parser CSV → existing audit → reviewed PDF → draft-only Espo export."""

import argparse
import csv
import fcntl
import hashlib
import json
import os
import sys
from collections import Counter
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from . import espo_export as espo
from .parser_import import convert


class Tee:
    def __init__(self, screen, log):
        self.screen, self.log = screen, log

    def write(self, value):
        self.screen.write(value)
        self.log.write(value)
        self.flush()
        return len(value)

    def flush(self):
        self.screen.flush()
        self.log.flush()


class CircuitAPI(espo.API):
    """A failed API request stops further requests in this invocation."""

    blocked = False

    def request(self, *args, **kwargs):
        if self.blocked:
            raise espo.Hold("CRM circuit stopped; inspect previous failure before next run")
        try:
            return super().request(*args, **kwargs)
        except Exception:
            self.blocked = True
            raise


def find_account(api, row):
    host = espo.website_domain(row["website"])
    sites = api.find("Account", "website", host, operator="contains")
    sites = [a for a in sites if espo.website_domain(a.get("website")) == host]
    names = api.find("Account", "name", row["company_name"])
    found = {a["id"]: a for a in sites + names}
    if len(found) > 1:
        raise espo.Hold("Ambiguous Account; reconcile name/domain in CRM")
    if found:
        account = next(iter(found.values()))
        if espo.website_domain(account.get("website")) != host:
            raise espo.Hold("Account name/domain conflict; manual reconciliation required")
        return account["id"]
    return ""


def pdf_check(path):
    from pypdf import PdfReader

    data = path.read_bytes()
    if not data.startswith(b"%PDF-") or len(data) > espo.MAX_PDF:
        raise espo.Hold("PDF signature/size invalid")
    reader = PdfReader(path)
    if reader.is_encrypted or not reader.pages or not any(p.extract_text() for p in reader.pages):
        raise espo.Hold("PDF unreadable, empty or encrypted")
    return hashlib.sha256(data).hexdigest()


def audit(row, approval, directory, allow_collection, *, ignore_robots=True, details_out=None):
    from .mvp_batch import run
    from .pdf_export import write_html_pdf

    saved = bool(approval.get("analysis"))
    if not saved and not allow_collection:
        raise espo.Hold("Site collection requires --allow-site-collection; no paid calls made")
    values = {"url": row["website"], "company_name": "", "email": row["email"]}
    if saved:
        if approval.get("identity_verified") is not True:
            raise espo.Hold("identity_verified required for personalized production audit")
        values.update(company_name=row["company_name"], **{
            key: approval[key] for key in ("analysis", "dossier", "legal_review")
        })
    # Different reviewed inputs get a separate derivative, never overwrite collected evidence.
    binding = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:16]
    job = directory / ("saved-" + binding if saved else "prepare")
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / (job.name + ".csv")
    if not source.exists():
        with source.open("x", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(values))
            writer.writeheader()
            writer.writerow(values)
    state = run(source, job, mode="saved" if saved else "prepare", max_sites=1,
                resume=(job / "manifest.json").exists(), max_ai_calls=0,
                allow_site_collection=not saved, ignore_robots=ignore_robots)
    result = state["rows"][0]
    details = dict(
        audit_success="evidence_packet:SUCCESS" in result.get("stages", []),
        proposal_eligible=bool(result.get("proposal")),
        html_pages_attempted=result.get("html_pages_attempted", 0),
        html_pages_captured=result.get("html_pages_captured", 0),
        audit_reason=result.get("reason", ""),
        dossier=result.get("dossier", ""),
    )
    review = job / result.get("directory", "") / "review.json"
    dossier = job / result.get("directory", "") / "dossier" / "collection.json"
    if dossier.exists():
        collected = json.loads(dossier.read_text())
        details["complete_text_pages"] = sum(
            p["reading"]["text_coverage_complete"] for p in collected["pages"]
        )
        details["collection_errors"] = list(dict.fromkeys(
            error for evidence in collected["evidence"] for error in evidence["collection_errors"]
            if not error.startswith(("screenshot:", "form_screenshot:"))
        ))
        details["reading_limitations"] = list(dict.fromkeys(
            limit for page in collected["pages"] for limit in page["reading"]["limitations"]
        ))
    if review.exists():
        details["trigger_ids"] = [f["rule_id"] for f in json.loads(review.read_text())["findings"]]
    if details_out is not None:
        details_out.update(details)
    espo.save(directory / "audit-summary.json", details)
    print("Audit: " + json.dumps(details, ensure_ascii=False), flush=True)
    if result["status"] == "ERROR":
        raise RuntimeError("Audit failed; inspect private audit manifest")
    if not result.get("proposal"):
        raise espo.Hold("Audit HOLD: " + result.get("reason", "no justified proposal"))
    html = job / result["proposal"]
    pdf = directory / (job.name + ".pdf") if saved else html.with_suffix(".pdf")
    if not pdf.exists():
        print("PDF: rendering approved HTML template", flush=True)
        write_html_pdf(html.read_text(), pdf)
    return pdf, pdf_check(pdf), saved and result.get("qualification") == "GO"


def process(rows, output, approvals, *, allow_collection=False, check_crm=False,
            confirm=False, journal=None, ignore_robots=True):
    api, crm_error = None, ""
    if check_crm or confirm:
        try:
            api = CircuitAPI()
            espo.check_schema(api)
            crm_error = ""
        except espo.Hold as exc:
            crm_error = str(exc)
        except Exception:
            crm_error = "CRM preflight failed; inspect access/schema before next run"
    results, seen = [], set()
    for number, row in enumerate(rows, 1):
        result = {"id": row["id"], "company": row["company_name"],
                  "website": row["website"], "status": "HOLD", "reason": "",
                  "pdf": "", "duplicate": False, "audit_success": False,
                  "proposal_eligible": False, "production_qualified": False}

        def progress(stage):
            counts = Counter(r["status"] for r in results)
            print(f"[{number}/{len(rows)}] {row['company_name']} | {row['website']} | "
                  f"{stage} | {result['status']} | {result['reason']} | "
                  f"processed={len(results)} READY={counts['READY']} REVIEW={counts['REVIEW']} "
                  f"HOLD={counts['HOLD']} ERROR={counts['ERROR']} "
                  f"PDF={sum(bool(r['pdf']) for r in results)} "
                  f"audit_success={sum(r['audit_success'] for r in results)} "
                  f"proposal_eligible={sum(r['proposal_eligible'] for r in results)} "
                  f"full_PDF={sum(bool(r['pdf']) and r['proposal_eligible'] for r in results)} "
                  f"CRM_Drafts={counts['READY']} "
                  f"duplicates={sum(r['duplicate'] for r in results)}", flush=True)

        try:
            progress("deduplication")
            host = espo.website_domain(row["website"])
            if host in seen:
                result["duplicate"] = True
                raise espo.Hold("Duplicate CSV domain; reconcile branches before separate proposal")
            seen.add(host)
            approval = approvals.get(row["id"], {})
            if approval and any(approval.get(k) != row[k] for k in ("company_name", "website", "email")):
                raise espo.Hold("Approval company/website/email binding mismatch")
            if not approval.get("analysis") and not allow_collection:
                raise espo.Hold("Site collection requires --allow-site-collection; no paid calls made")
            progress("existing_audit_engine")
            pdf, sha, qualified = audit(row, approval, output / row["id"], allow_collection,
                                        ignore_robots=ignore_robots, details_out=result)
            result.update(pdf=str(pdf), pdf_sha256=sha, audit_success=True,
                          proposal_eligible=True, production_qualified=qualified)
            progress("pdf_verified")
            if api is not None and not crm_error:
                try:
                    result["account_id"] = find_account(api, row)
                    result["duplicate"] = bool(result["account_id"])
                except espo.Hold as exc:
                    result["crm_reason"] = str(exc)
                    raise
            if crm_error:
                result["crm_reason"] = crm_error
            if not qualified:
                result.update(status="REVIEW", reason="Fixed-rule proposal from approved template; manual review required "
                              "including constant block4; CRM gate requires saved analysis/legal_review")
            elif not (approval.get("identity_verified") is True
                      and approval.get("export_approved") is True
                      and approval.get("pdf_sha256") == sha):
                result.update(status="REVIEW", reason="Review this PDF, verify identity, then set "
                              "identity_verified/export_approved and exact pdf_sha256 in approvals")
            elif not confirm:
                result.update(status="REVIEW", reason="Approved locally; CRM write requires --confirm-create-drafts")
            else:
                if crm_error:
                    raise espo.Hold(crm_error)
                progress("crm_export_and_readback")
                manifest = {**row, **{k: approval[k] for k in
                            ("recipient_verified", "existing_document_id", "segment", "industry_verified")
                            if k in approval}, "pdf_path": str(pdf), "export_approved": True}
                exported = espo.run([manifest], output, journal, api=api, confirm=True)[0]
                result["crm"] = exported
                result.update(status="READY" if exported["status"] == "DRAFT_VERIFIED" else "HOLD",
                              reason=exported.get("reason", "Draft and PDF verified; sending prohibited"))
        except espo.Hold as exc:
            result.update(status="HOLD", reason=str(exc))
        except Exception as exc:
            result.update(status="ERROR", reason=type(exc).__name__ + ": inspect private stage artifacts")
        results.append(result)
        progress("finished")
        espo.save(output / "report.json", results)
    counts = Counter(r["status"] for r in results)
    summary = dict(processed=len(results), PDF=sum(bool(r["pdf"]) for r in results),
                   CRM_Drafts=counts["READY"], REVIEW=counts["REVIEW"], HOLD=counts["HOLD"],
                   audit_success=sum(r["audit_success"] for r in results),
                   proposal_eligible=sum(r["proposal_eligible"] for r in results),
                   full_PDF=sum(bool(r["pdf"]) and r["proposal_eligible"] for r in results),
                   production_qualified=sum(r["production_qualified"] for r in results),
                   ERROR=counts["ERROR"], duplicates=sum(r["duplicate"] for r in results))
    with (output / "results.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        fields = ["id", "company", "website", "status", "reason", "pdf", "duplicate", "audit_success",
                  "proposal_eligible", "production_qualified", "audit_reason",
                  "html_pages_attempted", "html_pages_captured", "dossier", "trigger_ids", "crm_reason"]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    espo.save(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"Report: {output / 'report.json'}; CSV: {output / 'results.csv'}; logs: {output}")
    return 1 if counts["HOLD"] or counts["ERROR"] or counts["REVIEW"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--output", type=Path, default=Path.home() /
                        ("lexradar-pipeline10-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")))
    parser.add_argument("--approvals", type=Path)
    parser.add_argument("--check-crm", action="store_true")
    parser.add_argument("--allow-site-collection", action="store_true")
    parser.add_argument("--ignore-robots", action="store_true", default=True,
                        help="Ignore robots.txt (default); retain request and access limits")
    parser.add_argument("--confirm-create-drafts", action="store_true")
    parser.add_argument("--journal", type=Path,
                        default=Path.home() / ".local/state/lexradar/espo-export.json")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        raw = args.csv.read_bytes()
        _, rows = convert(raw)
        if len(rows) != 10:
            raise ValueError("Exactly 10 Parser rows required")
        output = args.output.absolute()
        if any(p.is_symlink() for p in (output, *output.parents)):
            raise ValueError("Symlink output refused")
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        if output.stat().st_mode & 0o077:
            raise ValueError("Output must be private (0700)")
        with (output / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            original = output / "input.csv"
            if original.exists() and original.read_bytes() != raw:
                raise ValueError("Output already bound to different CSV")
            original.write_bytes(raw)
            approvals = json.loads(args.approvals.read_text()) if args.approvals else {}
            if not isinstance(approvals, dict):
                raise ValueError("Approvals must be an object keyed by stable Parser id")
            template = output / "approval-template.json"
            if not template.exists():
                espo.save(template, {r["id"]: {
                    **{k: r[k] for k in ("company_name", "website", "email")},
                    "identity_verified": False, "recipient_verified": False,
                    "export_approved": False, "analysis": "", "dossier": "",
                    "legal_review": "", "pdf_sha256": "",
                } for r in rows})
            log = output / ("pipeline-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f") + ".log")
            with log.open("x") as stream, redirect_stdout(Tee(sys.stdout, stream)):
                print(f"Input: {original}; live log: {log}", flush=True)
                return process(rows, output, approvals, allow_collection=args.allow_site_collection,
                               check_crm=args.check_crm, confirm=args.confirm_create_drafts,
                               journal=args.journal, ignore_robots=args.ignore_robots)
    except (OSError, ValueError, csv.Error):
        print("Pipeline HOLD: invalid input, approvals or storage; no blind retry", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
