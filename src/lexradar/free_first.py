"""Five offline checks over verified originals; no AI provider or sending capability."""

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .auditors.evidence import load_packet
from .auditors.grounding import quote_locations
from .batch import safe_path, snapshot
from .client_proposal import render_client
from .collector import CrawlOptions, Limits, collect
from .cookie_inspection import inspect_saved
from .operator_audit import save
from .policy3 import inspect_policy3
from .primary.markup import SavedMarkup, compact

LAW_PD = "http://pravo.gov.ru/proxy/ips/?docbody=&nd=102108261"
LAW_SEPARATE = "http://publication.pravo.gov.ru/document/0001202506240021"
LAW_AD = "http://pravo.gov.ru/proxy/ips/?docbody=&nd=102105292"
BASES = {
    "WEB-01": ("152-ФЗ, ст. 5 ч. 7, ст. 18.1 ч. 2", LAW_PD),
    "WEB-02": ("152-ФЗ, ст. 6 и 9; применимость зависит от данных и основания", LAW_PD),
    "WEB-03": ("152-ФЗ, ст. 6 и 9; отсутствие чекбокса само по себе не дефект", LAW_PD),
    "WEB-04": ("152-ФЗ, ст. 9 ч. 1; 156-ФЗ, ст. 5, с 01.09.2025", LAW_SEPARATE),
    "WEB-05": ("38-ФЗ, ст. 18 ч. 1; 152-ФЗ, ст. 15", LAW_AD),
}
WORK = {
    "WEB-01": (
        "Подготовим согласованные сроки и порядок хранения в политике и инструкции ответственному."
    ),
    "WEB-02": "Подготовим текст выбора аналитики и техническое задание вебмастеру.",
    "WEB-03": "Подготовим необходимое согласие и техническое задание для формы заявки.",
    "WEB-04": "Подготовим отдельное согласие и техническое задание по его размещению в форме.",
    "WEB-05": "Подготовим рекламное согласие и техническое задание для отдельного выбора подписки.",
}
TITLES = {
    "WEB-01": "Противоречие сроков хранения в политике",
    "WEB-02": "Выбор аналитики посетителем",
    "WEB-03": "Согласие в форме заявки",
    "WEB-04": "Согласие объединено с договором",
    "WEB-05": "Согласие на рекламные сообщения",
}
RETENTION = re.compile(
    r"(Срок хранения [^.\n—:]{2,100} [—:] )(\d{1,4}) (дней|месяцев|лет)\.\s*"
    r"\1(\d{1,4}) \3\."
)
BUNDLED = (
    "Согласие на обработку персональных данных включено в текст договора и отдельно не оформляется."
)


def inspect(dossier):
    """FAIL covers explicit document contradictions, never inferred missing content."""
    dossier = safe_path(dossier)
    packet = load_packet(dossier, 20_000_000)
    policy = inspect_policy3(packet)
    cookie = inspect_saved(dossier)
    rows = {
        code: dict(
            check_id=code,
            status="REVIEW",
            summary="Недостаточно подтверждений.",
            evidence=[],
            legal_basis=dict(reference=basis[0], source=basis[1]),
            materiality="undetermined",
            limitations=[],
            suggested_work=WORK[code],
            findings=[],
        )
        for code, basis in BASES.items()
    }
    now = datetime.now(UTC).isoformat()
    usable = []
    for page in packet.data.pages:
        ev = next(e for e in packet.data.evidence if e.id == page.evidence_id)
        if ev.available and page.reading.provenance == "reproduced" and page.text:
            usable.append((page, ev))
    policy_sources = set(policy[0]["examined_sources"])

    def fact(code, defect_type, quote, eid, explanation):
        citations = [x for x in quote_locations(packet, quote) if x["evidence_id"] == eid]
        if not citations:
            return
        evidence = [
            {**x, "context": "reproduced_static_text", "examined_at": now} for x in citations
        ]
        row = rows[code]
        row["findings"].append(
            dict(
                check_id=code,
                defect_type=defect_type,
                status="FAIL",
                confirmed=True,
                evidence_level="exact_reproduced_text",
                title=TITLES[code],
                fact=explanation,
                quote=quote,
                evidence=evidence,
                legal_basis=row["legal_basis"],
                materiality="substantial",
                suggested_work=WORK[code],
            )
        )
        row["evidence"].extend(evidence)
        row.update(status="FAIL", materiality="substantial", summary=explanation)

    rows["WEB-01"]["policy3"] = policy
    for item in policy:
        rows["WEB-01"]["evidence"].extend(item["citations"])
    rows["WEB-01"]["summary"] = (
        "Политика доступна; полнота трёх порядков требует оценки."
        if policy_sources
        else "Доступная политика не установлена; отсутствие не доказано."
    )
    materials = [*packet.data.pages, *packet.data.documents]
    for material in materials:
        if (
            str(material.final_url or material.requested_url) not in policy_sources
            or not material.reading.text_coverage_complete
        ):
            continue
        texts = getattr(material, "page_texts", None)
        for text in texts.values() if texts else [material.text or ""]:
            for match in RETENTION.finditer(text):
                if match[2] != match[4] and not re.search(r"пример|образец|цитат", text, re.I):
                    fact(
                        "WEB-01",
                        "contradictory_retention",
                        match[0],
                        material.evidence_id,
                        "Для одной категории заявок подряд указаны разные сроки хранения "
                        "без разграничения условий.",
                    )

    rows["WEB-02"]["static_inventory"] = cookie
    rows["WEB-02"]["summary"] = "Статические упоминания аналитики не доказывают запуск или выбор."
    rows["WEB-02"]["limitations"] = [cookie["limitation"]]
    forms = []
    external = False
    for page, ev in usable:
        artifact = next((a for a in ev.artifacts if a.kind == "html"), None)
        if not artifact:
            continue
        raw = (dossier / artifact.path).read_text(encoding="utf-8")
        parser = SavedMarkup(str(ev.source))
        parser.feed(raw)
        parser.close()
        external |= any(tag == "iframe" for tag, _ in parser.resources) or "<iframe" in raw.lower()
        for form in parser.forms:
            text = compact(form.parts)
            forms.append((form, text, page, ev))
            observation = dict(
                source=str(ev.source),
                evidence_id=ev.id,
                exact_quote=text,
                context="static_form_declaration",
                captured_at=page.captured_at.isoformat(),
                examined_at=now,
                fields=[f.model_dump(mode="json") for f in form.fields],
                form_index=form.index,
                submission_not_attempted=True,
            )
            rows["WEB-03"]["evidence"].append(observation)
            rows["WEB-04"]["evidence"].append(observation)
            if re.search(r"реклам|рассылк|подписк", text, re.I):
                rows["WEB-05"]["evidence"].append(observation)
            # A declaration that the consent itself is embedded is a document defect;
            # checkbox state or a mere shared submit button is insufficient.
            if (
                BUNDLED in text
                and not parser.quarantined
                and form.end_exclusive
                and page.reading.text_coverage_complete
                and not re.search(r"пример|образец|цитат|ранее|больше не", text, re.I)
            ):
                fact(
                    "WEB-04",
                    "embedded_consent_document",
                    BUNDLED,
                    ev.id,
                    "В форме прямо указано: согласие включено в договор и отдельно не оформляется.",
                )
    if forms:
        rows["WEB-03"]["summary"] = "Формы найдены; поля, исходные отметки и тексты сохранены."
        if rows["WEB-04"]["status"] != "FAIL":
            rows["WEB-04"]["summary"] = "Прямое подтверждение объединённого документа не найдено."
    else:
        rows["WEB-03"]["summary"] = (
            "Доступные формы не обнаружены; внешние и JS-формы не исключены."
        )
    if not rows["WEB-05"]["evidence"]:
        if forms and not external and all(p.reading.text_coverage_complete for p, _ in usable):
            rows["WEB-05"].update(
                status="NOT_APPLICABLE",
                materiality="none",
                summary="В обследованных статических формах нет рекламного сценария.",
            )
        else:
            rows["WEB-05"]["summary"] = "Рекламное назначение формы не установлено."
    else:
        rows["WEB-05"]["summary"] = (
            "Есть рекламный сценарий; предварительное согласие требует проверки."
        )
    for code in ("WEB-03", "WEB-04", "WEB-05"):
        rows[code]["limitations"].append(
            "Статический HTML: CSS, видимость, JS, внешние виджеты и отправка не проверены. "
            "Нет чекбокса или он отмечен — это не автоматический вывод о нарушении."
        )
    rows["WEB-01"]["limitations"].append("Проверяется явное противоречие, а не полнота политики.")
    complete = bool(usable) and any(p.reading.text_coverage_complete for p, _ in usable)
    return packet, list(rows.values()), complete


def confirmed_findings(checks):
    selected = {}
    for row in checks:
        if row["status"] != "FAIL":
            continue
        for f in row["findings"]:
            if (
                f["status"] == "FAIL"
                and f["confirmed"] is True
                and f["materiality"] == "substantial"
                and f["evidence"]
            ):
                selected.setdefault((f["defect_type"], f["quote"]), f)
    return list(selected.values())[:4]


def run(dossier, output):
    dossier, output = safe_path(dossier), safe_path(output)
    if output.exists():
        raise ValueError("Output must be a new directory")
    before = snapshot(dossier)
    packet, checks, complete = inspect(dossier)
    findings = confirmed_findings(checks)
    # A complete reproduced defect is sufficient; a fragment alone never creates GO.
    outcome = "GO" if complete and findings else "AI_PENDING_APPROVAL" if complete else "HOLD"
    output.mkdir(parents=True, mode=0o700)
    result = dict(
        outcome=outcome,
        route="FREE_GO"
        if outcome == "GO"
        else "AI_CANDIDATE"
        if complete
        else "INSUFFICIENT_MATERIAL",
        reason="Подтверждён существенный недостаток."
        if outcome == "GO"
        else "Типовые правила не установили существенный недостаток; нужен отдельный разбор."
        if complete
        else "Недостаточно воспроизведённого полного текста; GO запрещён.",
        packet_sha256=packet.sha256,
        checks=checks,
        findings=findings,
        manual_approval_required=True,
        client_release_allowed=False,
        automatic_send_allowed=False,
        sent=False,
        new_ai_calls=0,
        new_ai_cost_usd="0",
        ai_approval_required=True,
        ai_approved=False,
    )
    if outcome == "GO":
        cards = [
            dict(
                confirmed=True,
                title=f["title"],
                quote=f["quote"],
                meaning=f["fact"] + " " + f["suggested_work"],
            )
            for f in findings
        ]
        page = render_client(
            {"recipient": "Руководителю организации", "url": str(packet.data.target_url)},
            cards,
            {"date": datetime.now(UTC).strftime("%d.%m.%Y")},
            policy_packet=packet,
        )
        (output / "proposal.html").write_text(page, encoding="utf-8")
    if snapshot(dossier) != before:
        raise ValueError("Source changed during examination")
    save(output / "free-first.json", result)
    save(output / "policy3.json", checks[0]["policy3"])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?")
    parser.add_argument("--dossier", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.dossier and not args.url:
        parser.error("Supply --dossier or a public URL")
    if args.output.exists():
        parser.error("Output must be a new directory")
    if args.dossier:
        dossier = args.dossier
    else:
        dossier = args.output.with_name(args.output.name + "-dossier")
        collect(args.url, dossier, Limits(), crawl_options=CrawlOptions(), static_only=True)
    result = run(dossier, args.output)
    print(
        json.dumps({k: result[k] for k in ("outcome", "route", "new_ai_calls")}, ensure_ascii=False)
    )
