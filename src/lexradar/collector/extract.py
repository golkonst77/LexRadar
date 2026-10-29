"""Conservative discovery heuristics; results are not identity verification."""

import re

from .models import EntityObservation

CATEGORIES = {
    "privacy_policy": ("политик", "персональн", "privacy"),
    "consent": ("соглас", "consent"),
    "documents": ("документ", "реквизит", "document"),
    "contacts": ("контакт", "contact"),
    "appointment": ("запис", "приём", "appointment"),
    "feedback": ("обратн", "feedback"),
}
SCREENING_LINK_PATTERNS = {
    "appointment": re.compile(
        r"запис|при[её]м|appointment|booking|форма заявки|форма записи", re.I
    ),
    "contacts": re.compile(r"контакт|contact|адрес|телефон|как добраться", re.I),
    "privacy_policy": re.compile(r"политик|персональн|privacy|personal.?data|пдн", re.I),
    "consent": re.compile(r"соглас|consent|соглашени", re.I),
    "cookie": re.compile(r"cookies?|куки", re.I),
}
ANALYTICS_HINTS = {
    "yandex_metrika": re.compile(r"mc\.yandex|yandex[-_.]?metri[kc]|ym\s*\(", re.I),
    "google_analytics": re.compile(r"google-analytics|googletagmanager\.com/gtag|gtag\s*\(", re.I),
    "google_tag_manager": re.compile(r"googletagmanager\.com/gtm|GTM-[A-Z0-9]+", re.I),
    "other_analytics": re.compile(r"matomo|clarity\.ms|hotjar|top\.mail\.ru", re.I),
}
COOKIE_TEXT = re.compile(r"cookie|cookies|куки", re.I)
DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".rtf", ".odt", ".xls", ".xlsx")


def categories(text: str) -> list[str]:
    text = text.lower()
    return [name for name, words in CATEGORIES.items() if any(w in text for w in words)]


def legal_link_kind(url: str, label: str = "") -> str | None:
    """Classify the document itself, without treating every PD mention as a policy."""
    from urllib.parse import unquote, urlsplit

    path = unquote(urlsplit(url).path).casefold()
    text = label.casefold()
    if re.search(r"\bполитик[а-яё]*\b|privacy\s+policy", text) or re.search(
        r"(?:^|/)(?:privacy|politi[ck]|politika)[^/]*", path
    ):
        return "privacy_policy"
    if re.search(r"соглас|consent", text) or re.search(r"soglas|consent|agreement", path):
        return "consent"
    if re.search(
        r"правов[а-яё]*\s+информац|юридическ[а-яё]*\s+(?:информац|документ)|реквизит", text
    ) or re.search(r"legal[-_/]?information|requisites|rekvizit", path):
        return "legal_information"
    if path.endswith(".pdf"):
        return "document"
    return None


def screening_link_kind(text: str) -> str | None:
    legal = legal_link_kind("", text)
    return legal or next(
        (kind for kind, pattern in SCREENING_LINK_PATTERNS.items() if pattern.search(text)), None
    )


def screening_link_priority(text: str) -> int:
    kind = screening_link_kind(text)
    if kind in {"privacy_policy", "consent", "legal_information", "document"}:
        return -1
    if kind in {"appointment", "contacts"}:
        return 0
    if kind == "cookie":
        return 2
    return 3 if categories(text) else 4


def static_analytics_hints(html: bytes) -> list[str]:
    text = html.decode("utf-8", errors="replace")
    return [name for name, pattern in ANALYTICS_HINTS.items() if pattern.search(text)]


def entities(text: str, source: str) -> list[EntityObservation]:
    results = []
    # Keep each named entity in its own block rather than assigning all IDs to the first name.
    starts = list(re.finditer(r'(?:ООО|АО|ПАО|ЗАО|ГБУЗ|ГАУЗ)\s+[«"][^»"\n]{1,150}[»"]', text))
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[match.start() : min(end, match.start() + 1000)]
        inn = re.search(r"ИНН\s*[:№]?\s*(\d{10}|\d{12})(?!\d)", block)
        ogrn = re.search(r"ОГРН(?:ИП)?\s*[:№]?\s*(\d{13}|\d{15})(?!\d)", block)
        address = re.search(r"(?:Адрес|адрес)\s*:\s*([^\n]{1,300})", block)
        results.append(
            EntityObservation(
                source=source,
                raw_text=block,
                name=match.group(),
                inn=inn[1] if inn else None,
                ogrn=ogrn[1] if ogrn else None,
                address=address[1] if address else None,
            )
        )
    if not starts:
        for line in text.splitlines():
            if re.search(r"ИНН|ОГРН|[Аа]дрес\s*:", line):
                results.append(EntityObservation(source=source, raw_text=line[:1000]))
    return results


DOM_SCRIPT = r"""() => {
  const text = document.body?.innerText || '';
  const isVisible = el => {
    const style = getComputedStyle(el);
    return el.getClientRects().length > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const controlText = el => [el.innerText, el.value, el.id, el.className,
    el.getAttribute('aria-label')].filter(Boolean).join(' ').slice(0, 300);
  const links = [...document.querySelectorAll('a[href]')].map(a => ({
    url: a.href, text: a.innerText.slice(0, 300), visible: isVisible(a),
    footer_policy: !!a.closest('footer,[role="contentinfo"]') && /политик/i.test(a.innerText)
  }));
  const consentControls = [...document.querySelectorAll(
    'button,[role="button"],a[href],input[type="button"],input[type="submit"],input[type="checkbox"]'
  )].map(controlText).filter(text => /cookie|куки|consent|соглас|настро|accept|reject|отказ/i.test(text)).slice(0, 50);
  const forms = [...document.querySelectorAll('form')].slice(0, 30).map(form => ({
    purpose: form.getAttribute('aria-label') ||
      form.querySelector('legend,h1,h2,h3')?.innerText || null,
    fields: [...form.querySelectorAll('input,select,textarea')].slice(0,100).map(el => ({
      tag: el.tagName.toLowerCase(), type: el.type || el.tagName.toLowerCase(),
      name: el.name || null,
      placeholder: el.getAttribute('placeholder'), aria_label: el.getAttribute('aria-label'),
      disabled: el.disabled, readonly: !!el.readOnly,
      associated_label: [...(el.labels || [])].map(label => label.innerText).join(' ').slice(0, 1000) || null,
      label: el.labels?.[0]?.innerText.slice(0,300) || el.getAttribute('aria-label') || null,
      required: el.required,
      checked: ['checkbox','radio'].includes(el.type) ? el.checked : null
    })),
    submission_context: form.innerText.slice(0,3000),
    links: [...form.querySelectorAll('a[href]')].map(a => ({url:a.href,text:a.innerText}))
  }));
  return {text, title: document.title, links, forms, consentControls};
}"""
