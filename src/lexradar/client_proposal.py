"""Offline client copy from confirmed observations; never grants release permission.

Callers retain evidence validation and release gates. Commercial configuration is
explicit: tariffs and existing-client discounts are never selected implicitly.
"""

import hashlib
import re
from decimal import Decimal
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from .proposal_rules import BLOCKS, HEALTH_FIELD_BLOCK, NoProposalTriggers, evaluate_rules


def esc(value):
    return escape(str(value), quote=True)


def pricing(config):
    tariff = config.get("tariffs", {}).get(config.get("selected_tariff"), {})
    amount = tariff.get("price", config.get("price"))
    term = tariff.get("term", config.get("term"))
    if amount is None:
        price = "Стоимость согласуем после уточнения объёма работ."
    else:
        amount = Decimal(str(amount))
        if not amount.is_finite() or amount < 0:
            raise ValueError("Invalid price")
        discount = config.get("existing_client_discount_percent", 0)
        if config.get("apply_existing_client_discount"):
            if not config.get("existing_client_confirmed"):
                raise ValueError("Discount requires confirmed existing client")
            discount = Decimal(str(discount))
            if not discount.is_finite() or not 0 <= discount <= 100:
                raise ValueError("Invalid discount")
            amount *= 1 - discount / 100
        price = f"Стоимость: {amount:,.2f} ₽".replace(",", " ")
    return price, f"Срок: {term}" if term else "Срок согласуем вместе с объёмом работ."


APPROVED_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "approved-templates" / "КП LexRadar__v2_5.html"
)
APPROVED_SHA256 = "b7ad845dfb4db7dbbf3dc9b6316ce76d1c0e41be0262a6b49bf2dbffeb2afc33"
APPROVED_GIT_BLOB = "9f129eb3a617832e7ea9fa1004f5123d2a79321f"
PERSONALIZATION = re.compile(r"(<span\s+contenteditable\s*=\s*true\s*>)(.*?)(</span>)", re.I | re.S)


def proposal_recipient(client, packet):
    if client.get("recipient"):
        return client["recipient"]
    name = (client.get("name") or "").strip()
    if not name or name.startswith("Сайт ") or "не подтверждена" in name:
        name = ""
        if packet is not None:
            from .primary.service import ENTITY_NAMES

            available = {e.id for e in packet.data.evidence if e.available}
            names = {
                match.group() for page in packet.data.pages
                if page.evidence_id in available and page.reading.provenance == "reproduced"
                and page.reading.text_coverage_complete
                for match in ENTITY_NAMES.finditer(page.text or "")
            }
            if len(names) == 1:
                name = names.pop()
    return "Руководителю " + (name or "клиники")


def render_client(client, findings, config, *, policy_packet=None, policy_review=None, pricelist_review=None):
    """Personalize the byte-pinned approved original, entirely offline."""
    if config.get("sender", "ИП Голубев") != "ИП Голубев":
        raise ValueError("Seller must be ИП Голубев")
    raw = APPROVED_TEMPLATE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != APPROVED_SHA256:
        raise ValueError("Approved proposal template integrity mismatch")
    selected = evaluate_rules(policy_packet)
    if pricelist_review is not None:
        from .pricelist import review_supported

        if review_supported(pricelist_review):
            selected.append(dict(rule_id=8, evidence=pricelist_review["positions"]))
    if not selected:
        raise NoProposalTriggers("no_confirmed_triggers_1_2_3_5")
    numbers = {item["rule_id"] for item in selected}
    numbers.add(4)
    numbers = sorted(numbers)
    observations = []
    for number in numbers:
        if number == 8:
            from .pricelist import proposal_block

            block = proposal_block(pricelist_review)
        elif number == 7:
            block = {
                "title": "ПРОВЕРКА ФОРМУЛИРОВОК ПОЛИТИКИ ОБРАБОТКИ ДАННЫХ",
                "paragraphs": list(dict.fromkeys(
                    proof["explanation"] for rule in selected if rule["rule_id"] == 7
                    for proof in rule["evidence"]
                )),
            }
        else:
            block = HEALTH_FIELD_BLOCK if number == 6 else BLOCKS[number]
        paragraphs = "".join(
            '<p class="MsoNormal" style="font:11.5pt Arial,sans-serif;margin:6pt 0">'
            + esc(text)
            + "</p>"
            for text in block["paragraphs"]
        )
        observations.append(
            f'<div class="motivation-block" data-rule="{number}">'
            '<h3 style="font:bold 11.5pt Arial,sans-serif;margin:12pt 0 6pt">'
            + esc(block["title"])
            + "</h3>"
            + paragraphs
            + "</div>"
        )
    values = iter(
        (
            esc(config["date"]),
            esc(proposal_recipient(client, policy_packet)),
            esc(client["url"]),
        )
    )
    template = raw.decode("utf-16")
    if len(PERSONALIZATION.findall(template)) != 3:
        raise ValueError("Unexpected v2.5 recipient, site, or date fields")
    template = PERSONALIZATION.sub(lambda match: match[1] + next(values) + match[3], template)
    # The four Word-exported paragraphs have fixed category labels; replace them
    # together with their slots so page order can never assign a legal category.
    slot_paragraph = re.compile(
        r"<p\b[^>]*>(?:(?!</p>).)*?\[Вставить[^\]]*\](?:(?!</p>).)*?</p>", re.S | re.I
    )
    slots = list(slot_paragraph.finditer(template))
    if len(slots) != 4:
        raise ValueError("Unexpected v2.5 observation fields")
    for index, match in reversed(list(enumerate(slots))):
        replacement = "".join(observations) if index == 0 else ""
        template = template[: match.start()] + replacement + template[match.end() :]
    template = template.replace("КП v2.2", "КП v2.5")
    template = re.sub(
        r"и выявили ряд обстоятельств,\s*требующих внимания с точки зрения "
        r"законодательства о персональных данных\s*\(152-ФЗ\) и надзорной практики "
        r"<span class=SpellE>Роскомнадзора</span>\.",
        "и подготовили предложение по проверке документов и процессов "
        "обработки персональных данных.",
        template,
    )
    # Remove editing UI from client exports; keep the approved original byte-pinned.
    template = re.sub(
        r"<p\b[^>]*>(?:(?!</p>).)*?Нажмите на текст,(?:(?!</p>).)*?</p>", "", template, flags=re.S
    )
    template = re.sub(r"<button\b[^>]*>.*?</button>", "", template, flags=re.S | re.I)
    template = re.sub(
        r"<p\b[^>]*>(?:(?!</p>).)*?Ориентировочное время: 2–5 минут\."
        r"(?:(?!</p>).)*?</p>", "", template, flags=re.S
    )
    template = re.sub(
        r"<p\b[^>]*>(?:(?!</p>).)*?Замечания\s+предварительные: "
        r"окончательная юридическая квалификация зависит от документов и\s+"
        r"фактических процессов организации\.(?:(?!</p>).)*?</p>",
        "", template, flags=re.S,
    )
    return "\ufeff" + template


class _ProposalText(HTMLParser):
    """Extract our Word-exported proposal, ignoring CSS and print/edit controls."""

    def __init__(self):
        super().__init__()
        self.parts = []
        self.excluded = None

    def handle_starttag(self, tag, attrs):
        if tag in {"head", "style", "script", "button"} and self.excluded is None:
            self.excluded = tag
        if not self.excluded and tag in {"p", "h1", "h2", "tr", "br"}:
            self.parts.append("\n")
        if not self.excluded and tag in {"td", "th"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == self.excluded:
            self.excluded = None
        if not self.excluded and tag in {"p", "h1", "h2", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.excluded:
            self.parts.append(re.sub(r"\s+", " ", data))


def proposal_text(page):
    parser = _ProposalText()
    parser.feed(page)
    # The existing plain-text PDF font covers BMP only; section emoji are decoration.
    text = "".join(c for c in "".join(parser.parts) if ord(c) <= 65535 and c != "\ufeff")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def document(body):
    return (
        """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';
form-action 'none'; base-uri 'none'">
<title>Коммерческое предложение · ИП Голубев</title><style>
*{box-sizing:border-box}
a{color:inherit}footer{margin-top:36px;border-top:2px solid #14796e;padding-top:16px}
.risks{background:#fff1ee;border-left:5px solid #c43c32;padding:4px 24px 16px}
.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:15px}
th,td{padding:14px;text-align:left;border:1px solid #dce6e9;vertical-align:top}
th{background:#163f48;color:white}td:last-child{min-width:100px}
.amount{font-size:46px;font-weight:800;margin:12px 0;color:#126558}
.badge{font-weight:700;background:#ffe5a1;padding:12px;border-radius:8px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}
body{margin:0;background:#edf2f4;color:#1c303d;font:17px/1.65 system-ui,Arial}
main{max-width:940px;margin:40px auto;padding:52px;background:white;border-top:7px solid #14796e}
h1{font-size:34px;line-height:1.2;letter-spacing:-.6px}h2{font-size:23px;line-height:1.3;margin-top:36px}
h3{font-size:20px;margin:0}.eyebrow{color:#14796e;font-weight:700}.lead{font-size:21px}
article{border:1px solid #dce6e9;border-left:4px solid #14796e;padding:22px;margin:20px 0}
blockquote{margin:16px 0;padding:12px 16px;background:#f2f6f7;font-size:16px}
.price{background:#eff7f5;padding:4px 24px 16px}
.next{background:#163f48;color:white;padding:4px 24px 16px}
p,li,blockquote{overflow-wrap:anywhere}li{margin:12px 0}
@media(max-width:640px){main{margin:0;padding:24px}h1{font-size:27px}h2{font-size:21px}}
@media print{@page{margin:16mm}
body{background:white;font-size:11pt}
main{margin:0;padding:0;max-width:none}
h2,h3{break-after:avoid}article,.price,.next,tr,footer{break-inside:avoid}
thead{display:table-header-group}*{print-color-adjust:exact}
.next{color:#163f48;border:2px solid #163f48}}
</style></head><body><main>"""
        + body
        + "</main></body></html>"
    )
