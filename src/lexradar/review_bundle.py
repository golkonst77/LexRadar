"""Offline, evidence-checked commercial review of an existing MVP dossier.

No collector, AI transport, legal scoring, sending or deployment is invoked.
"""

import argparse
import hashlib
import json
from pathlib import Path

from .auditors.evidence import load_packet
from .client_proposal import render_client
from .commercial_preview import esc, safe_link
from .policy3 import inspect_policy3


def form_groups(forms):
    """Group equal declarations, preserving occurrences and coverage differences.

    These are markup families, never a count of actual processing operations.
    Search-only and hidden-only forms are technical inventory, not lead forms.
    """
    groups = {}
    for form in forms:
        fields = form["fields"]
        visible = [f for f in fields if f["type"] != "hidden"]
        kind = (
            "technical"
            if not visible or all(f["type"] in {"search", "submit", "button"} for f in visible)
            else "contact_candidate"
        )
        signature = json.dumps(
            {
                "fields": fields,
                "policy_links": sorted(form["policy_links"]),
                "consent_links": sorted(form["consent_links"]),
                "advertising_links": sorted(form.get("advertising_links", [])),
                "context": form.get("submission_context"),
                "action": form.get("action_reference"),
                "method": form.get("mechanism", {}).get("declared_method"),
                "coverage": form.get("static_coverage", {}).get("status", "unknown"),
                "kind": kind,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        group = groups.setdefault(signature, {"kind": kind, "representative": form, "members": []})
        group["members"].append(
            {"id": form["id"], "url": form["url"], "citation": form["citation"]}
        )
    return list(groups.values())


def read_artifact(dossier, artifact):
    path = dossier / artifact["path"]
    if path.is_symlink() or not path.resolve().is_relative_to(dossier.resolve()):
        raise ValueError("Unsafe artifact path")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
        raise ValueError("Artifact integrity mismatch")
    return data


def build(source: Path, brief: dict, output: Path):
    """Fail closed on changed evidence or unsupported quotes, before writing output."""
    if output.exists():
        raise FileExistsError(output)
    dossier = source / "dossier"
    collection = json.loads((dossier / "collection.json").read_text())
    primary = json.loads((source / "analysis/primary.local.json").read_text())
    if collection["target_url"] != brief["url"]:
        raise ValueError("Brief URL mismatch")
    evidence = {item["id"]: item for item in collection["evidence"]}
    # Verify every registered original, not only the selected snippets.
    originals = {}
    for item in evidence.values():
        for artifact in item["artifacts"]:
            originals[artifact["path"]] = read_artifact(dossier, artifact)
    findings = []
    for index, finding in enumerate(brief["findings"], 1):
        item = evidence[finding["evidence_id"]]
        artifact = next(a for a in item["artifacts"] if a["kind"] == finding["kind"])
        raw = originals[artifact["path"]]
        page_number = finding.get("page")
        if finding["kind"] == "html":
            text = raw.decode("utf-8")
            if page_number is not None:
                raise ValueError("HTML citation cannot have a PDF page")
            basis = "saved_html"
        elif finding["kind"] == "pdf":
            from io import BytesIO

            from pypdf import PdfReader

            if not isinstance(page_number, int) or page_number < 1:
                raise ValueError("PDF citation requires a positive page")
            text = PdfReader(BytesIO(raw)).pages[page_number - 1].extract_text()
            basis = "pdf_text_extraction"
        else:
            raise ValueError("Unsupported citation basis")
        quote = finding["quote"]
        if not quote.strip() or quote not in text:
            raise ValueError("Quote is not present in the original artifact")
        offset = text.index(quote)
        findings.append(
            dict(
                finding,
                id=f"F{index:02}",
                source_url=item["source"],
                artifact_path=artifact["path"],
                artifact_sha256=artifact["sha256"],
                basis=basis,
                line=text[:offset].count("\n") + 1 if basis == "saved_html" else None,
                status="observed_not_legal_violation",
            )
        )
    groups = form_groups(primary["forms"])
    summary = {
        "raw_forms": len(primary["forms"]),
        "markup_families": len(groups),
        "technical_families": sum(g["kind"] == "technical" for g in groups),
        "contact_candidate_families": sum(g["kind"] == "contact_candidate" for g in groups),
    }
    result = {
        "schema_version": 1,
        "client": brief["client"],
        "url": brief["url"],
        "collected_at": collection["started_at"],
        "review_date": "2026-10-15",
        "mode": "offline_saved_evidence",
        "cost_usd": "0",
        "legal_violations_established": False,
        "client_release": "HOLD",
        "summary": summary,
        "findings": findings,
        "form_groups": groups,
        "limitations": list(dict.fromkeys(brief["limitations"])),
        "source_collection_sha256": hashlib.sha256(
            (dossier / "collection.json").read_bytes()
        ).hexdigest(),
    }
    cards = ""
    evidence_cards = ""
    for f in findings:
        cards += (
            f"<section><h3>{esc(f['id'])}. {esc(f['title'])}</h3>"
            f"<p>{esc(f['fact'])}</p><p><b>Что уточнить:</b> {esc(f['question'])}</p>"
            f"<p><b>Результат работы:</b> {esc(f['deliverable'])}</p>"
            f'<a href="evidence.html#{f["id"]}">Источник и точная цитата</a></section>'
        )
        locator = f"страница PDF {f['page']}" if f.get("page") else f"строка HTML {f['line']}"
        evidence_cards += (
            f'<section id="{f["id"]}"><h2>{esc(f["id"])}. {esc(f["title"])}</h2>'
            f"<p>{safe_link(f['source_url'])} · {esc(locator)}</p>"
            f"<pre>{esc(f['quote'])}</pre><p>{esc(f['basis'])}; "
            f"{esc(f['artifact_path'])}<br>SHA256: {esc(f['artifact_sha256'])}</p>"
            "<p>Цитата сверена с сохранённым оригиналом. Извлечение PDF не заменяет "
            "визуальную проверку. Текущее состояние сайта не подтверждено.</p></section>"
        )
    limits = "".join(f"<li>{esc(x)}</li>" for x in result["limitations"])
    # Legacy bundles may have only verified originals, without a Collector text packet.
    policy_packet = load_packet(dossier, 20_000_000) if "pages" in collection else None
    result["policy3"] = inspect_policy3(policy_packet)
    proposal = render_client(
        {"name": brief["client"], "url": brief["url"]},
        [
            dict(
                confirmed=True,
                title=f["title"],
                quote=f["quote"],
                meaning=f["fact"] + " " + f["deliverable"],
            )
            for f in findings
        ],
        {"date": result["review_date"], **brief.get("commercial", {})},
        policy_packet=policy_packet,
    )
    inventory = ""
    for i, group in enumerate(groups, 1):
        f = group["representative"]
        fields = ", ".join(
            x.get("label") or x.get("name") or x["type"]
            for x in f["fields"]
            if x["type"] != "hidden"
        )
        label = (
            "поисковая / техническая форма"
            if group["kind"] == "technical"
            else "контактная форма; назначение уточняется"
        )
        inventory += (
            f"<li>Семейство {i}: {esc(label)}; "
            f"{len(group['members'])} вхождений; {esc(fields)}. "
            f"Пример: {safe_link(f['url'])}.</li>"
        )
    audit = f"""<h1>Краткий аудит: {esc(brief["client"])}</h1>
<p>Сохранённая выборка от {esc(collection["started_at"])}; повторный анализ 15.10.2026.
Юридические нарушения не установлены. Платных API-запросов: 0.</p>
<p>{summary["raw_forms"]} статических вхождений форм → {summary["markup_families"]} семейств
разметки, из них технических: {summary["technical_families"]}.
Это не число уникальных процессов обработки данных. Поисковые формы не выдаются за заявки.</p>
<ul>{inventory}</ul><h2>Находки и задачи</h2>{cards}
<h2>Ограничения</h2><ul>{limits}</ul>
<p>Полный инвентарь со всеми URL и координатами в <a href="review.json">review.json</a>.
Скрытые поля и технические повторы убраны из основного текста, оригиналы не изменены.</p>"""
    output.mkdir(parents=True)
    (output / "review.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for name, body in [("audit", audit), ("evidence", evidence_cards)]:
        (output / f"{name}.html").write_text(document(brief["client"], body), encoding="utf-8")
    (output / "proposal.html").write_text(proposal, encoding="utf-8")
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir())}
    (output / "sha256.json").write_text(json.dumps(hashes, indent=2) + "\n")
    return result


def document(title, body):
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · ИП Голубев</title><style>
*{{box-sizing:border-box}}
body{{margin:0;background:#eef3f5;color:#182c36;font:17px/1.65 system-ui,Arial}}
main{{max-width:940px;margin:35px auto;padding:42px;background:white;border-top:7px solid #14766a}}
h1{{font-size:36px;line-height:1.2}}h2{{margin-top:36px}}h3{{font-size:21px}}
section{{border-bottom:1px solid #d9e4e6;padding:12px 0 24px}}
a{{color:#086b62;overflow-wrap:anywhere}}
.notice{{padding:16px;background:#fff4df}}.lead{{font-size:21px}}.eyebrow,.muted{{color:#526d76}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5f6;padding:16px;font-size:14px}}
p,li{{overflow-wrap:anywhere}}@media(max-width:600px){{main{{margin:0;padding:22px}}h1{{font-size:28px}}}}
@media print{{body{{background:white}}main{{margin:0;padding:15px}}details{{display:block}}}}
</style></head><body><main>{body}</main></body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--brief", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.source, json.loads(args.brief.read_text()), args.output)


if __name__ == "__main__":
    main()
