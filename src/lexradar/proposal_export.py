"""Export a saved, grounded audit into separate client and internal artifacts."""

import argparse
import hashlib
import json
import re
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from .client_proposal import (
    APPROVED_GIT_BLOB,
    APPROVED_SHA256,
    document,
    esc,
    render_client,
    render_neutral,
)
from .pdf_export import write_pdf


class _ProposalText(HTMLParser):
    """Extract visible proposal text for the existing accessible PDF writer."""

    BLOCKS = {"br", "div", "footer", "h1", "h2", "h3", "header", "li", "p", "section", "tr", "td"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self.skipped = []

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class", "").split()
        if self.skipped:
            if tag not in self.VOID:
                self.skipped.append(tag)
            return
        if tag in {"head", "script", "style", "button"} or any(
            name in classes for name in ("toolbar", "symbol")
        ):
            if tag not in self.VOID:
                self.skipped.append(tag)
            return
        if tag in self.BLOCKS:
            self.lines.append("\n")

    def handle_endtag(self, tag):
        if self.skipped:
            if tag == self.skipped[-1]:
                self.skipped.pop()
            return
        if tag in self.BLOCKS:
            self.lines.append("\n")

    def handle_data(self, data):
        if not self.skipped and data.strip():
            self.lines.append(re.sub(r"\s+", " ", data.strip()))


def _artifact_bound(dossier, collection, evidence_id, source_url, artifact_path, artifact_sha256):
    page_urls = {
        page.get("requested_url")
        for page in collection.get("pages", [])
        if page.get("evidence_id") == evidence_id
    }
    page_urls.update(
        page.get("final_url")
        for page in collection.get("pages", [])
        if page.get("evidence_id") == evidence_id
    )
    if source_url not in page_urls:
        return False
    evidence = next((e for e in collection.get("evidence", []) if e.get("id") == evidence_id), None)
    if not evidence or not evidence.get("available"):
        return False
    artifact = next(
        (
            a
            for a in evidence.get("artifacts", [])
            if a.get("path") == artifact_path
            and a.get("sha256") == artifact_sha256
            and a.get("kind") == "html"
        ),
        None,
    )
    if not artifact:
        return False
    relative = Path(artifact_path)
    if relative.is_absolute() or ".." in relative.parts:
        return False
    path = dossier / relative
    return path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == artifact_sha256


def _risk_finding(dossier, collection, primary, screening):
    pages = {p.get("evidence_id"): p for p in collection.get("pages", [])}
    for signal in screening.get("signals", []):
        for citation in signal.get("citations", []):
            evidence_id = citation.get("evidence_id")
            source_url = citation.get("source_url")
            page = pages.get(evidence_id, {})
            quote = citation.get("quote")
            if (
                not quote
                or source_url not in {page.get("requested_url"), page.get("final_url")}
                or quote not in (page.get("text") or "")
                or not _artifact_bound(
                    dossier,
                    collection,
                    evidence_id,
                    source_url,
                    citation.get("artifact_path"),
                    citation.get("artifact_sha256"),
                )
            ):
                continue
            return {
                "confirmed": True,
                "title": "Наблюдение требует проверки",
                "observation": "В сохранённом тексте зафиксировано: «" + quote + "»",
                "source_url": source_url,
                "evidence_id": evidence_id,
                "artifact_sha256": citation.get("artifact_sha256"),
                "meaning": "Это наблюдение само по себе не подтверждает нарушение; необходимо проверить его контекст и фактические процессы.",
                "priority": 100,
            }

    for form in primary.get("forms", []):
        citation = form.get("citation") or {}
        source_url = citation.get("url") or form.get("url")
        evidence_id = citation.get("evidence_id") or form.get("evidence_id")
        artifact_path = citation.get("artifact_path")
        artifact_sha256 = citation.get("artifact_sha256")
        parsed = urlsplit(str(source_url or ""))
        if (
            form.get("static_coverage", {}).get("status") != "complete"
            or parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or not _artifact_bound(
                dossier, collection, evidence_id, source_url, artifact_path, artifact_sha256
            )
        ):
            continue
        types = sorted(
            {
                t
                for field in form.get("fields", [])
                if (t := str(field.get("type", "")).lower())
                and re.fullmatch(r"[a-z0-9_-]{1,24}", t)
            }
        )
        description = "Статически обнаружена веб-форма"
        if types:
            description += "; типы полей: " + ", ".join(types)
        return {
            "confirmed": True,
            "title": "Форма на странице — требуется проверка",
            "observation": description,
            "source_url": source_url,
            "evidence_id": evidence_id,
            "artifact_sha256": artifact_sha256,
            "meaning": "Наличие формы не подтверждает нарушение. Требуется сверить оператора, цели, основание, текст согласия и фактический маршрут обработки.",
            "priority": 100,
        }
    return None


def build_screening_draft(
    dossier: Path,
    collection: dict,
    primary: dict,
    screening: dict,
    client: dict,
    output: Path,
    *,
    screening_note="",
):
    """Build a bound one-observation draft, otherwise a neutral offer; never release it."""
    if output.exists():
        raise FileExistsError(output)
    finding = _risk_finding(dossier, collection, primary, screening)
    draft_type = "risk_oriented" if finding else "neutral"
    config = {"date": date.today().strftime("%d.%m.%Y"), "screening_note": screening_note}
    html = (
        render_client(client, [finding], config)
        if finding
        else render_neutral(client, config)
    )
    output.mkdir(parents=True)
    html_path = output / "proposal.html"
    html_path.write_text(html, encoding="utf-8")
    pdf_path = output / "proposal.pdf"
    temporary_pdf = output / ".proposal.pdf.tmp"
    pdf_error = None
    try:
        parser = _ProposalText()
        parser.feed(html)
        write_pdf(parser.lines, temporary_pdf)
        temporary_pdf.replace(pdf_path)
    except Exception as exc:
        pdf_error = type(exc).__name__
        temporary_pdf.unlink(missing_ok=True)
    state = {
        "draft_type": draft_type,
        "screening_group": screening.get("group", "not_run"),
        "risk_observation_count": int(bool(finding)),
        "legal_status": "not_confirmed",
        "template_version": "approved-kp-v2.4" if finding else None,
        "template_sha256": APPROVED_SHA256 if finding else None,
        "base_price_rub": 45000,
        "discount_percent": 25,
        "discount_deadline_inclusive": "2026-11-01",
        "total_price_rub": 33750,
        "consultation_months": 2,
        "html_created": True,
        "pdf_created": pdf_path.is_file(),
        "pdf_error": pdf_error,
        "manual_approval_required": True,
        "manual_approval": None,
        "client_release_allowed": False,
        "automatic_send_allowed": False,
        "sent": False,
    }
    (output / "proposal-state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return state


def build(source: Path, collection_path: Path, config: dict, output: Path):
    if output.exists():
        raise FileExistsError(output)
    raw = source.read_bytes()
    audit = json.loads(raw)
    collection = json.loads(collection_path.read_text())
    pages = {p["evidence_id"]: p for p in collection["pages"]}
    findings = []
    for item in audit["candidates"]:
        if item.get("text_observation_confirmed") is not True:
            continue
        q = item["grounding"]
        text = pages[q["evidence_id"]]["text"]
        if text[q["start"] : q["end"]] != q["exact_quote"] or not q["exact_quote"].strip():
            raise ValueError("Quote does not match saved evidence")
        if item["source"] != collection["target_url"]:
            raise ValueError("Source URL mismatch")
        editorial = "пройти техническое обслуживание" in q["exact_quote"]
        findings.append(
            dict(
                confirmed=True,
                title=(
                    "В документе стоматологии — «техническое обслуживание»"
                    if editorial
                    else item["documentary_defect"]
                ),
                quote=("пройти техническое обслуживание" if editorial else q["exact_quote"]),
                meaning=(
                    "Формулировка не соответствует стоматологическим услугам. "
                    "Адаптируем текст к реальному обращению пациента."
                    if editorial
                    else item["proposed_paid_work"]
                ),
                priority=100 if editorial else 0,
            )
        )
    if len(findings) < 2:
        raise ValueError("Client export requires 2–4 confirmed findings")
    client = config["client"]
    if client["url"] != collection["target_url"]:
        raise ValueError("Client URL mismatch")
    # Optional personalization must occur in the saved primary text, not model output.
    primary_text = " ".join(
        p.get("text", "") + " " + p.get("title", "") for p in collection["pages"]
    )
    for key in ("name", "city", "industry", "recipient"):
        if client.get(key) and client[key] not in primary_text:
            raise ValueError("Unconfirmed client identity")
    proposal = render_client(client, findings, config["commercial"])
    state = dict(
        status=audit["outcome"],
        legal_production_status=audit["legal_production_status"],
        legal_go=False,
        automatic_send_allowed=False,
        client_release_allowed=False,
        manual_approval_required=True,
        manual_approval=None,
        sent=False,
        source=str(source),
        source_sha256=hashlib.sha256(raw).hexdigest(),
        collection_sha256=hashlib.sha256(collection_path.read_bytes()).hexdigest(),
        proposal_sha256=hashlib.sha256(proposal.encode()).hexdigest(),
        new_ai_calls=0,
        network_requests=0,
        template_version="approved-kp-v2.4",
        template_sha256=APPROVED_SHA256,
        template_git_blob=APPROVED_GIT_BLOB,
        reference_text_available=True,
        base_price_rub=45000,
        discount_percent=25,
        discount_deadline_inclusive="2026-11-01",
        total_price_rub=33750,
        savings_rub=11250,
        free_consultations_months=2,
        confirmed_findings_count=min(len(findings), 4),
    )
    internal = dict(audit=audit, saved_collection=collection, configuration=config, state=state)
    output.mkdir(parents=True)
    (output / "proposal-doktstom.html").write_text(proposal, encoding="utf-8")
    for name, data in [("internal-report", internal), ("proposal-state", state)]:
        (output / f"{name}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    (output / "internal-report.html").write_text(
        document(
            "<h1>Полный внутренний отчёт</h1><pre>"
            + esc(json.dumps(internal, ensure_ascii=False, indent=2))
            + "</pre>"
        ),
        encoding="utf-8",
    )
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "collection", "config", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    build(args.source, args.collection, json.loads(args.config.read_text()), args.output)


if __name__ == "__main__":
    main()
