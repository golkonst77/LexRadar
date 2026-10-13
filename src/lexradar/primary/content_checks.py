"""Bounded content observations in the existing primary model, never legal verdicts."""

import re
from collections import defaultdict
from urllib.parse import unquote, urlsplit

from ..collector.extract import categories
from .markup import compact

EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
PRICE = re.compile(
    r"(?<!\d)(\d[\d\s\u00a0]*)(?:[–—-]\s*(\d[\d\s\u00a0]*))?\s*(?:₽|руб\.?|RUB)", re.I
)
UNIT = re.compile(r"(?:за\s+|1\s+)(зуб|канал|челюсть|пакет)\b", re.I)


def price_rows(parser):
    """Explicit row and currency required; preserve names, units, ranges and context."""
    for cells, span in parser.price_rows:
        if not cells:
            continue
        context = " | ".join(cells)
        match = PRICE.search(context)
        if not match:
            continue
        title = next(
            (
                cell
                for cell in cells
                if re.search(r"[а-яА-Яa-zA-Z]", cell) and not PRICE.fullmatch(cell)
            ),
            None,
        )
        if not title:
            continue
        unit = UNIT.search(context)
        low = int(re.sub(r"\s", "", match[1]))
        high = int(re.sub(r"\s", "", match[2])) if match[2] else low
        if low > high:
            continue
        yield {
            "name": title,
            "key": re.sub(r"\s+", " ", title).strip().casefold(),
            "unit": unit[1].casefold() if unit else None,
            "price": (low, high),
            "currency": "RUB",
            "original_price": match[0],
            "context": context,
            "span": span,
        }


def question(builder, stage, description, citations, scope, action):
    fact = builder.observe(stage, "static_markup", description, citations, scope)
    check = builder.task(
        stage, "content_reconciliation", action, evidence=[c.evidence_id for c in citations]
    )
    builder.hypothesis(
        stage, action, [fact], [scope, "Актуальная норма, назначение и применимость"], [check]
    )


def inspect_content(builder):
    prices = defaultdict(list)
    for eid, parser in builder.markup.items():
        for row in price_rows(parser):
            prices[row["key"]].append((eid, row))
        for link in parser.links:
            if not link.url.casefold().startswith("mailto:"):
                continue
            visible = EMAIL.fullmatch(link.text.strip())
            recipient = unquote(urlsplit(link.url).path)
            if (
                visible
                and EMAIL.fullmatch(recipient)
                and visible[0].casefold() != recipient.casefold()
            ):
                question(
                    builder,
                    "01",
                    "mailto_recipient_mismatch: видимый email и адресат ссылки различаются",
                    [
                        builder.cite(
                            eid,
                            "saved_html",
                            quote=f"{link.text} → {link.url}",
                            element="a[href^=mailto]",
                            span=link.span,
                        )
                    ],
                    "Наблюдение ссылки; отправка, доставка и утечка не проверялись",
                    "Уточнить корректный публичный email и адресат ссылки",
                )

        # Deduplicate only the URL; preserve every declaration and source range.
        if "documents" in categories(
            str(builder.evidence[eid].source) + " " + compact(parser.parts)
        ):
            images = defaultdict(list)
            for url, method, raw, span in parser.image_declarations:
                images[url].append((method, raw, span))
            page = next(p for p in builder.packet.data.pages if p.evidence_id == eid)
            for url, declarations in images.items():
                collected = next((i for i in page.document_images if i.url == url), None)
                status = collected.download_status if collected else "not_attempted"
                scope = (
                    f"reading_status=not_read; download_status={status}; "
                    "лицензия не объявляется отсутствующей; реестровый статус не проверен"
                )
                if collected:
                    scope += "; " + collected.limitation
                question(
                    builder,
                    "12",
                    "document_image_unread: объявлен кандидат изображения документа",
                    [
                        builder.cite(
                            eid,
                            "saved_html",
                            quote=f"{method}: {raw}",
                            element=f"image reference: {url}",
                            span=span,
                        )
                        for method, raw, span in declarations
                    ],
                    scope,
                    "Прочитать изображение документа, сохранить результат "
                    "и проверить его назначение",
                )

        for text, navigation, region in parser.semantic_blocks:
            if navigation:
                continue
            checks = []
            if re.search(r"(?:надзор|уполномоченн)[\s\S]*30\s*(?:рабочих\s*)?дн", text, re.I):
                checks.append(
                    (
                        "04",
                        "policy_response_period_candidate",
                        "Сопоставить опубликованный срок ответа надзорному органу "
                        "с применимой редакцией нормы",
                    )
                )
            if (
                re.search(r"актуализац|обновлен|уточнен", text, re.I)
                and re.search(r"https?://", text)
                and not EMAIL.search(text)
            ):
                checks.append(
                    (
                        "04",
                        "policy_contact_url_candidate",
                        "Уточнить предусмотренный канал актуализации данных: "
                        "в положении указан URL, не email",
                    )
                )
            if re.search(r"трансграничн", text, re.I):
                checks.append(
                    (
                        "04",
                        "cross_border_policy_review",
                        "Проверить опубликованное положение о трансграничной передаче "
                        "по применимой редакции; фактическая передача неизвестна",
                    )
                )
            if re.search(r"программ", text, re.I) and re.search(r"\b20\d{2}\b", text):
                years = [int(y) for y in re.findall(r"\b20\d{2}\b", text)]
                if years and max(years) < builder.packet.data.started_at.year:
                    archived = bool(re.search(r"архив|историческ", text, re.I))
                    checks.append(
                        (
                            "12",
                            "archived_program_reference"
                            if archived
                            else "dated_program_role_review",
                            (
                                "Уточнить назначение программы и необходимость действующей "
                                "информации; архивная маркировка учтена"
                            )
                            if archived
                            else "Уточнить: действующая программа или архив",
                        )
                    )
            for stage, code, action in checks:
                question(
                    builder,
                    stage,
                    f"{code}: содержание опубликованного положения требует оценки",
                    [
                        builder.cite(
                            eid,
                            "saved_html",
                            quote=text,
                            element="semantic text block",
                            span=parser.region_span(region),
                        )
                    ],
                    "Изучено опубликованное положение, не фактический процесс; "
                    "правовое несоответствие не установлено",
                    action,
                )

    differences = []
    citations = []
    seen = set()
    for rows in prices.values():
        for i, (left_id, left) in enumerate(rows):
            for right_id, right in rows[i + 1 :]:
                if left_id == right_id or left["price"] == right["price"]:
                    continue
                if left["unit"] and right["unit"] and left["unit"] != right["unit"]:
                    continue
                key = (left_id, right_id, left["key"], left["unit"], right["unit"])
                if key in seen:
                    continue
                seen.add(key)
                differences.append(
                    f"{left['name']}: {left['original_price']} / {right['original_price']}; "
                    f"RUB; единицы: {left['unit'] or 'не указана'} / "
                    f"{right['unit'] or 'не указана'}"
                )
                for eid, row in ((left_id, left), (right_id, right)):
                    citations.append(
                        builder.cite(
                            eid, "saved_html", quote=row["context"], element="tr", span=row["span"]
                        )
                    )
    if differences:
        question(
            builder,
            "12",
            "price_discrepancy_candidate: " + "; ".join(differences),
            citations,
            "Возможное расхождение; сопоставимость объёма, единиц и актуальности "
            "требует уточнения; диапазоны и коды номенклатуры не квалифицируются",
            "Согласовать цены и сопоставимость одинаково названных услуг между страницами",
        )
