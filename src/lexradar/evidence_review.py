"""Local evidence review; never verifies current law or contacts a provider."""

import html

from .batch import write_json
from .single_auditor import checked_report, commercial


def review(report, packet, directory, *, qualification=None, new_ai_calls=0, new_cost_usd="0"):
    """VERIFIED describes eligibility under the supplied decision, not AI agreement."""
    checked = checked_report(report, packet)
    allowed, reasons = qualification if qualification is not None else commercial(checked, packet)
    evidence = {e.id: e for e in packet.data.evidence}
    pages = {p.evidence_id: p for p in packet.data.pages}
    docs = {d.evidence_id: d for d in packet.data.documents}
    entries = []
    for finding in checked.runs[0].result.findings if checked.runs[0].result else []:
        quotes = []
        for quote in finding.text_grounding:
            item = evidence.get(quote.evidence_id)
            page = pages.get(quote.evidence_id)
            doc = docs.get(quote.evidence_id)
            text = (page.text or "") if page and quote.page in (None, 1) else ""
            if doc:
                text = doc.page_texts.get(quote.page, "")
            matches = bool(
                item
                and item.available
                and item.source == finding.source
                and quote.exact_quote
                and quote.exact_quote in text
            )
            quotes.append({**quote.model_dump(mode="json"), "exact_match": matches})
        status = (
            "VERIFIED"
            if finding.id in allowed
            else ("PARTIAL" if quotes and all(q["exact_match"] for q in quotes) else "UNSUPPORTED")
        )
        reason = (
            "Точная цитата и критерии коммерческой квалификации подтверждены; "
            "юридическая применимость требует независимой проверки."
            if status == "VERIFIED"
            else "Цитата найдена, но не доказывает всю фактическую и юридическую гипотезу. "
            "Неполная выборка не доказывает отсутствие документов или нарушение."
            if status == "PARTIAL"
            else "Точная привязанная к источнику цитата не подтверждена."
        )
        if finding.id == "consent_form_issue":
            reason += (
                " Форма и текст согласия присутствуют. Полный сценарий отправки не исследован. "
                "Ссылка AI на ч. 4 ст. 9 не обосновывает применение требований к письменному "
                "согласию ко всякой веб-форме; основания обработки и актуальность нормы "
                "не проверены."
            )
        if finding.id == "privacy_policy_issue":
            reason += (
                " Общее утверждение о безопасности присутствует. Из него не следует отсутствие "
                "мер безопасности или обязанность раскрывать все технические меры в политике; "
                "AI не подтвердил правовую применимость своего вывода."
            )
        if finding.id == "consent_form_issue" and any(
            'Записаться Нажимая "Отправить"' in (page.text or "") for page in packet.data.pages
        ):
            reason += (
                " В сохранённом тексте перед пояснением стоит «Записаться», "
                "тогда как цитата ссылается на «Отправить». AI не подтвердил название "
                "кнопки; это текстовая несогласованность, а не доказанное нарушение. "
                "Сценарий формы требует отдельной проверки."
            )
        entries.append(
            {
                "id": finding.id,
                "assessment": status,
                "reason": reason,
                "fact": finding.fact,
                "source": str(finding.source),
                "quotes": quotes,
                "commercially_usable": finding.id in allowed,
                "legal_references_verified": False,
            }
        )
    decision = {
        "outcome": "HOLD" if reasons else "GO",
        "reasons": reasons,
        "allowed_findings": allowed,
        "automatic_send_allowed": False,
        "new_ai_calls": new_ai_calls,
        "new_cost_usd": new_cost_usd,
    }
    write_json(
        directory / "evidence-review.json",
        {
            "packet_sha256": packet.sha256,
            "manifest_sha256": packet.manifest_sha256,
            "findings": entries,
            "limitations": list(packet.limitations),
            "historical_audit_cost_usd": str(report.budget_charged_usd),
            "new_ai_calls": new_ai_calls,
            "new_cost_usd": new_cost_usd,
            "scope": "Сохранённые материалы; актуальное право и живой сайт не проверялись.",
        },
    )
    write_json(
        directory / "findings.json",
        checked.runs[0].result.model_dump(mode="json") if checked.runs[0].result else {},
    )
    write_json(directory / "decision.json", decision)
    if reasons:
        escape = html.escape
        parts = [
            '<!doctype html><html lang="ru"><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            "<title>Внутренняя проверка — HOLD</title><style>",
            "body{max-width:900px;margin:40px auto;padding:24px;font:17px/1.6 system-ui;"
            "color:#183044}article{padding:20px;background:#f1f5f9;margin:20px 0}"
            "blockquote{margin:12px 0;padding:12px;border-left:3px solid #64748b}"
            "@page{size:A4;margin:18mm}@media print{body{margin:0;padding:0} "
            "article{break-inside:avoid}} </style>",
            "<h1>Внутренняя проверка: HOLD</h1>",
            "<p>Сайт: " + escape(str(packet.data.target_url)) + "</p>",
            "<p>Клиентское предложение не подготовлено: нет доказанной пригодной находки. "
            "Успешное выполнение AI не подтверждает юридическую корректность.</p>",
        ]
        parts += [
            "<p>Модель: "
            + escape(checked.runs[0].model)
            + ". Сохранённый аудит завершён: "
            + str(checked.completed)
            + ".</p>",
            "<p>Расход сохранённого аудита: $"
            + escape(str(report.budget_charged_usd))
            + "; новые вызовы: "
            + str(new_ai_calls)
            + "; новый расход: $"
            + escape(str(new_cost_usd))
            + ".</p>",
            "<h2>Границы применимости</h2><p>Проверены сохранённые материалы: страниц — "
            + str(len(packet.data.pages))
            + ", документов — "
            + str(len(packet.data.documents))
            + ". Полнота сайта, динамика форм, актуальное право и внедрение не проверялись. "
            "Неаутентифицированный локальный импорт не доказывает состояние живого сайта.</p>",
        ]
        for entry in entries:
            parts += [
                "<article><h2>"
                + escape(entry["id"])
                + " — "
                + entry["assessment"]
                + "</h2><p>"
                + escape(entry["reason"])
                + "</p>"
            ]
            for quote in entry["quotes"]:
                parts += ["<blockquote>" + escape(quote["exact_quote"]) + "</blockquote>"]
            parts += ["<p>Источник: " + escape(entry["source"]) + "</p></article>"]
        parts += [
            "<h2>Что проверить далее</h2><p>Отдельно согласовать проверку сценария формы, "
            "ссылок на документы, оснований обработки и применимых норм. "
            "Ничего не отправлено. Текущая выборка не позволяет обвинять компанию.</p></html>"
        ]
        (directory / "report.html").write_text("\n".join(parts), encoding="utf-8")
    return decision
