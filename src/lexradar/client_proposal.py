"""Offline client copy from confirmed observations; never grants release permission.

Callers retain evidence validation and release gates. Commercial configuration is
explicit: tariffs and existing-client discounts are never selected implicitly.
"""

import hashlib
import re
from decimal import Decimal
from html import escape
from pathlib import Path


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
    Path(__file__).resolve().parents[2] / "templates" / "approved" / "КП LexRadar__v2_4.html"
)
APPROVED_SHA256 = "7e352c2597ff272bcf25095ac442411da2fc291bdbb0559cf0dd116970de7b5e"
APPROVED_GIT_BLOB = "028404f2de14ab9a77c442d081e5ba0cda86b334"
# Only the inner contents of these four approved fields may change.
PERSONALIZATION = re.compile(
    r'(<span class="editable" contenteditable="true">)(.*?)(</span>)'
    r'|(<ol class="findings editable" contenteditable="true">)(.*?)(</ol>)',
    re.DOTALL,
)


def render_client(client, findings, config):
    """Personalize the byte-pinned approved original, entirely offline."""
    if config.get("sender", "ИП Голубев") != "ИП Голубев":
        raise ValueError("Seller must be ИП Голубев")
    raw = APPROVED_TEMPLATE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != APPROVED_SHA256:
        raise ValueError("Approved proposal template integrity mismatch")
    selected = [f for f in findings if f.get("confirmed") is True and f.get("quote", "").strip()]
    selected = sorted(selected, key=lambda f: -f.get("priority", 0))[:4]
    if not selected:
        raise ValueError("Client proposal requires 2–4 confirmed findings")
    cards = "".join(
        f"<li><strong>{esc(f['title'])}</strong> «{esc(f['quote'])}» {esc(f['meaning'])}</li>"
        for f in selected
    )
    values = iter(
        (
            esc(config["date"]),
            esc(client.get("recipient") or "Руководителю " + (client.get("name") or client["url"])),
            esc(client["url"]),
            cards,
        )
    )
    template = raw.decode("utf-8")
    if len(PERSONALIZATION.findall(template)) != 4:
        raise ValueError("Unexpected approved personalization fields")

    def substitute(match):
        return (match[1] or match[4]) + next(values) + (match[3] or match[6])

    return PERSONALIZATION.sub(substitute, template)


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
