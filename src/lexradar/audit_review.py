"""Offline export of a completed audit, including failed model attempts."""

import argparse
import hashlib
import html
import json
from pathlib import Path

from .audit_capture import Capture, scrub
from .auditors.evidence import load_packet
from .auditors.models import AnalysisReport
from .batch_qualification import qualify
from .evidence_review import review
from .single_auditor import messages_for, profile


def read_json(path):
    return json.loads(path.read_text())


def export(audit, output, *, dossier=None, qualification=None):
    audit, output = Path(audit).resolve(), Path(output).resolve()
    provenance = (
        read_json(audit / "provenance.json") if (audit / "provenance.json").exists() else {}
    )
    source = Path(provenance.get("source_audit", audit)).resolve()
    report = AnalysisReport.model_validate_json((source / "analysis.json").read_bytes())
    captures = sorted(source.glob("capture-*"))
    dossier = dossier or provenance.get("source_dossier")
    if not dossier and captures:
        dossier = read_json(captures[0] / "source.json")["dossier"]
    packet = load_packet(Path(dossier), profile().max_input_bytes) if dossier else None
    if packet and packet.sha256 != report.evidence_packet_sha256:
        raise ValueError("Packet hash mismatch")
    target = Capture(output)
    # Prevent accidental Git publication even when output is outside runs-067.
    target.write(".gitignore", "*\n")
    limitations = [
        "Локальный экспорт; новых API-вызовов нет. Право и живой сайт не проверены.",
        "Секреты известных форматов редактируются; редактирование явно отмечено.",
        "Персональные данные могут присутствовать. Передачу выполняет только оператор.",
    ]
    originals = [
        (p, (p / "original.txt").read_bytes().decode("utf-8"))
        for p in captures
        if (p / "original.txt").exists()
    ]
    original = originals[-1][1] if originals else "ORIGINAL_NOT_AVAILABLE\n"
    if any(
        read_json(p / "capture.json").get("redacted")
        for p, _ in originals
        if (p / "capture.json").exists()
    ):
        limitations.append(
            "Ответ содержит редакции секретов: экспорт не является точным оригиналом."
        )
    if not originals:
        no_response = bool(report.logs) and all(
            log.provider_diagnostics is not None
            and log.provider_diagnostics.received_bytes == 0
            and log.provider_diagnostics.http_status is None
            for log in report.logs
        )
        limitations.append(
            "Ответ модели не получен: транспорт завершился без HTTP-ответа и байтов."
            if no_response
            else "Исходный ответ утрачен: сохранена только нормализованная структура. "
            "Форматирование, исходные значения изменённых полей не восстановимы."
        )
    target.write("01-deepseek-original.txt", original)
    for index, (path, text) in enumerate(originals):
        target.write(f"attempt-{index + 1}-original.txt", text)
        for name in ("capture.json", "status.json"):
            if (path / name).exists():
                target.write(f"attempt-{index + 1}-{name}", (path / name).read_text())
    target.write("saved-structured-analysis.json", report.model_dump_json(indent=2))
    if captures:
        system = (captures[-1] / "system.txt").read_text()
        payload = (captures[-1] / "packet.txt").read_text()
    elif packet:
        messages, _, digest = messages_for("A", packet)
        if report.logs and any(log.prompt_sha256 != digest for log in report.logs):
            # Pre-offset single-a-v1 schema: accept only an exact historical hash.
            if all(log.prompt_version == "single-a-v1" for log in report.logs):
                messages, _, digest = messages_for("A", packet, historical_schema=True)
        if any(log.prompt_sha256 != digest for log in report.logs):
            raise ValueError("Reconstructed prompt hash mismatch")
        system, payload = messages[0]["content"], packet.payload
        limitations.append(
            "Промпт и пакет реконструированы текущим кодом; хеши совпали с журналом."
        )
    else:
        system = payload = "NOT_AVAILABLE\n"
        limitations.append("Промпт и пакет недоступны: укажите --dossier.")
    target.write("02-system-prompt.txt", system)
    target.write("03-evidence-packet.txt", payload)
    if packet:
        scratch = output / "qualification-details"
        scratch.mkdir(mode=0o700)
        decision = review(
            report, packet, scratch, qualification=qualify(report, packet, production=True)
        )
        entries = read_json(scratch / "evidence-review.json")["findings"]
        for path in scratch.iterdir():
            path.chmod(0o600)
    else:
        decision = {"outcome": "ERROR", "reasons": ["evidence_packet_unavailable"]}
        entries = []
    if qualification is not None:
        decision = qualification
    elif (audit / "decision.json").exists():
        saved = read_json(audit / "decision.json")
        if saved != decision:
            limitations.append(
                "Сохранённая квалификация отличается от текущего пересчёта; "
                "в итоговом решении приведена сохранённая."
            )
        decision = saved
    target.write(
        "04-qualification.json",
        json.dumps(
            {
                "decision": decision,
                "findings": entries,
                "limitations": limitations,
                "source_audit": str(source),
                "source_sha256": hashlib.sha256(
                    (source / "analysis.json").read_bytes()
                ).hexdigest(),
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    findings = [
        f.model_dump(mode="json") for run in report.runs if run.result for f in run.result.findings
    ]
    raw_findings = {}
    if originals:
        try:
            raw_findings = {f["id"]: f for f in json.loads(original)["findings"]}
        except (ValueError, KeyError, TypeError):
            limitations.append("Ответ не разобран как JSON; полный оригинал приведён отдельно.")
    sections = []
    for finding in findings:
        entry = next((e for e in entries if e["id"] == finding["id"]), {})
        text = json.dumps(raw_findings.get(finding["id"], finding), ensure_ascii=False, indent=2)
        label = (
            "Поля исходной находки (JSON переформатирован); точный ответ — 01-deepseek-original.txt"
            if finding["id"] in raw_findings
            else ("ORIGINAL_NOT_AVAILABLE: нормализованная находка")
        )
        sections.append(
            f"## {finding['id']}\n{label}\n\n{text}\n\n"
            + json.dumps(entry, ensure_ascii=False, indent=2)
            + "\nРешение: "
            + decision["outcome"]
        )
    comparison = "\n\n".join(sections)
    target.write(
        "05-findings-comparison.html",
        '<!doctype html><meta charset="utf-8">'
        "<title>Audit review</title><pre>"
        + html.escape(scrub(comparison))
        + "</pre><h2>Полный исходный ответ (если доступен)</h2><pre>"
        + html.escape(scrub(original))
        + "</pre>",
    )
    summary = "\n".join(limitations) + "\n\n" + json.dumps(decision, ensure_ascii=False, indent=2)
    cookie_reviews = [
        {"auditor": run.auditor, "cookie_analytics_review":
         run.result.cookie_analytics_review.model_dump(mode="json")}
        for run in report.runs if run.result is not None
    ]
    summary += "\n\n## Cookie и веб-аналитика\n\n" + json.dumps(
        cookie_reviews, ensure_ascii=False, indent=2
    )
    target.write("06-review-summary.md", summary)
    bundle = "# Ручная экспертиза DeepSeek\n\n" + summary + "\n\n" + comparison
    for name in (
        "01-deepseek-original.txt",
        "02-system-prompt.txt",
        "03-evidence-packet.txt",
        "04-qualification.json",
        "saved-structured-analysis.json",
    ):
        bundle += f"\n\n## [{name}]({output / name})\n\n" + (output / name).read_bytes().decode(
            "utf-8"
        )
    for path in sorted(output.glob("attempt-*")):
        bundle += f"\n\n## {path.name}\n\n" + path.read_text()
    bundle += f"\n\nИсточник: [{source / 'analysis.json'}]({source / 'analysis.json'})\n"
    bundle += "\nПолные доступные тексты включены без сокращений.\n"
    target.write("CHATGPT-REVIEW.md", bundle)
    return decision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dossier", type=Path)
    args = parser.parse_args()
    export(args.audit, args.output, dossier=args.dossier)


if __name__ == "__main__":
    main()
