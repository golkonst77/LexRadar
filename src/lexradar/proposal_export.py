"""Export a saved, grounded audit into separate client and internal artifacts."""

import argparse
import hashlib
import json
from pathlib import Path

from .auditors.evidence import load_packet
from .client_proposal import APPROVED_GIT_BLOB, APPROVED_SHA256, document, esc, render_client
from .policy3 import inspect_policy3


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
        legally_proven = item.get("legal_violation_proven") is True
        findings.append(
            dict(
                confirmed=True,
                title=(
                    "В документе стоматологии — «техническое обслуживание»"
                    if editorial
                    else item["documentary_defect"] if legally_proven else "Фрагмент сохранённого источника"
                ),
                quote=("пройти техническое обслуживание" if editorial else q["exact_quote"]),
                risk_text=(
                    None if legally_proven or editorial else item.get("documentary_defect")
                ),
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
    policy_packet = load_packet(collection_path.parent, 20_000_000)
    proposal = render_client(
        client,
        findings,
        config["commercial"],
        policy_packet=policy_packet,
    )
    state = dict(
        policy3=inspect_policy3(policy_packet),
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
        template_version="approved-kp-v2.5",
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
