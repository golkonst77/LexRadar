"""Three bounded policy questions; literal search never establishes a defect."""

import re
from datetime import UTC, datetime
from html import escape
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field

from .auditors.grounding import check_quote, quote_locations
from .auditors.models import TextGrounding
from .models import Model

CHECKS = {
    "retention": (
        "Сроки хранения",
        "Какие сроки хранения установлены для каждой цели и категории данных, "
        "включая резервные копии?",
        r"срок\w*\s+(?:\w+\s+){0,3}хран|хран\w*\s+(?:до|не более|в течение)",
    ),
    "deletion": (
        "Удаление и уничтожение",
        "Кто, когда и каким способом удаляет или уничтожает данные и как фиксируется результат?",
        r"уничтож\w*|удалени\w*",
    ),
    "transfers": (
        "Передача третьим лицам",
        "Кому, для каких целей и на каком основании передаются данные "
        "и какие ограничения есть у получателей?",
        r"треть\w*\s+лиц\w*|передач\w*|передав\w*",
    ),
}


class Policy3Review(Model):
    check: Literal["retention", "deletion", "transfers"]
    status: Literal["PASS", "REVIEW", "FAIL"]
    quote: TextGrounding
    explanation: str = Field(min_length=20, max_length=2000)
    confirmed: bool = Field(default=False, strict=True)


def inspect_policy3(packet=None, review=None):
    """PASS/FAIL are explicit local judgments, never AI or missing-word inferences."""
    regions = []
    if packet is not None:
        evidence = {e.id: e for e in packet.data.evidence}
        for material in [*packet.data.pages, *packet.data.documents]:
            if (
                not evidence[material.evidence_id].available
                or material.reading.provenance != "reproduced"
            ):
                continue
            url = str(material.final_url or material.requested_url)
            title = getattr(material, "title", "") or ""
            if not (
                re.search(r"politik|privacy|personal.data.policy", urlsplit(url).path, re.I)
                or re.search(r"политик.*(?:персональн|конфиденц)", title, re.I)
            ):
                continue
            texts = getattr(material, "page_texts", None)
            for page, text in texts.items() if texts else [(None, material.text or "")]:
                regions.append((material.evidence_id, page, url, text))
    overrides = {}
    if review is not None:
        if (
            packet is None
            or review.packet_sha256 != packet.sha256
            or not review.reviewer.strip()
            or review.reviewed_at > datetime.now(UTC)
        ):
            raise ValueError("POLICY-3 review packet mismatch")
        for item in review.policy3:
            if item.check in overrides:
                raise ValueError("Duplicate POLICY-3 review")
            if (
                item.status != "REVIEW"
                and not item.confirmed
                or len(item.explanation.strip()) < 20
                or item.quote.evidence_id not in {r[0] for r in regions}
                or check_quote(packet, item.quote)["status"] != "MATCH"
            ):
                raise ValueError("POLICY-3 requires an exact policy quote and confirmed judgment")
            overrides[item.check] = item
    rows = []
    for key, (label, question, pattern) in CHECKS.items():
        citations = []
        for eid, page, _url, text in regions:
            for match in re.finditer(pattern, text, re.I):
                previous = text.rfind(". ", 0, match.start())
                start = max(
                    previous + 2 if previous >= 0 else 0, text.rfind("\n", 0, match.start()) + 1
                )
                # Bound the excerpt without rewriting its bytes or treating it as complete coverage.
                start = max(start, match.start() - 150)
                ends = [
                    i
                    for i in [text.find(". ", match.end()), text.find("\n", match.end())]
                    if i >= 0
                ]
                end = min(min(ends) + 1 if ends else len(text), match.end() + 350)
                quote = text[start:end].strip()
                locations = [
                    m
                    for m in quote_locations(packet, quote)
                    if m["evidence_id"] == eid and m["page"] == page
                ]
                if locations and not any(c["exact_quote"] == quote for c in citations):
                    citations.append(locations[0])
                if len(citations) == 3:
                    break
            if len(citations) == 3:
                break
        item = overrides.get(key)
        if item:
            checked = check_quote(packet, item.quote)
            citations = [
                m
                for m in checked["matches"]
                if m["evidence_id"] == item.quote.evidence_id
                and m["page"] == item.quote.page
                and (item.quote.start is None or item.quote.start == m["start"])
                and (item.quote.end is None or item.quote.end == m["end"])
            ]
        first = citations[0] if citations else None
        rows.append(
            dict(
                check=key,
                label=label,
                status=item.status if item else "REVIEW",
                quote=first["exact_quote"] if first else None,
                source=first["source"] if first else regions[0][2] if regions else None,
                examined_sources=list(dict.fromkeys(r[2] for r in regions)),
                citations=citations,
                question=question,
                explanation=item.explanation
                if item
                else (
                    "Найден тематический фрагмент; полнота и применимость порядка не подтверждены."
                    if citations
                    else "Доступный тематический фрагмент не извлечён; недостаток не установлен."
                ),
                independently_confirmed=bool(item and item.confirmed),
                reviewer=review.reviewer if item else None,
            )
        )
    return rows


def policy3_html(rows):
    parts = ['<li class="policy3"><strong>Политика обработки персональных данных</strong>']
    for row in rows:
        text = row["explanation"] if row["status"] == "FAIL" else row["question"]
        part = f"<p><b>{escape(row['label'])}.</b> {escape(text)}"
        if row["status"] == "FAIL":
            part += f" «{escape(row['quote'])}» Источник: {escape(row['source'])}"
        parts.append(part + "</p>")
    return "".join(parts) + "</li>"
