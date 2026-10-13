"""Conservative discovery heuristics; results are not identity verification."""

import re

from .models import EntityObservation

CATEGORIES = {
    "privacy_policy": ("политик", "персональн", "privacy"),
    "consent": ("соглас", "consent"),
    "documents": ("документ", "реквизит", "document", "dokum"),
    "contacts": ("контакт", "contact"),
    "appointment": ("запис", "приём", "appointment"),
    "feedback": ("обратн", "feedback"),
}
DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".rtf", ".odt", ".xls", ".xlsx")


def image_references(tag: str, values: dict) -> list[tuple[str, str]]:
    """Literal image declarations only; neither document content nor execution."""
    refs = []
    for name in ("src", "data-src", "data-original", "data-img-zoom-url"):
        if values.get(name) and (tag == "img" or name != "src"):
            refs.append((name, values[name]))
    style = values.get("style") or ""
    for match in re.finditer(
        r"background(?:-image)?\s*:[^;]*?url\(\s*(['\"]?)([^'\"()]+)\1\s*\)",
        style,
        re.I,
    ):
        refs.append(("style.background-image", match[2].strip()))
    return [(method, url) for method, url in refs if len(url) <= 4096]


def categories(text: str) -> list[str]:
    text = text.lower()
    return [name for name, words in CATEGORIES.items() if any(w in text for w in words)]


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
  const links = [...document.querySelectorAll('a[href]')].map(a => ({
    url: a.href, text: a.innerText.slice(0, 300)
  }));
  const forms = [...document.querySelectorAll('form')].slice(0, 30).map(form => ({
    element: form.id ? 'form#' + form.id : null,
    visibility: form.getClientRects().length && getComputedStyle(form).visibility !== 'hidden'
      ? 'visible_in_isolated_render' : 'hidden_in_isolated_render',
    purpose: form.getAttribute('aria-label') ||
      form.querySelector('legend,h1,h2,h3')?.innerText || null,
    fields: [...form.querySelectorAll('input,select,textarea')].slice(0,100).map(el => ({
      tag: el.tagName.toLowerCase(), type: el.type || el.tagName.toLowerCase(),
      name: el.name || null,
      label: el.labels?.[0]?.innerText.slice(0,300) || el.getAttribute('aria-label') || null,
      required: el.required,
      checked: ['checkbox','radio'].includes(el.type) ? el.checked : null
    })),
    submission_context: form.innerText.slice(0,3000),
    links: [...form.querySelectorAll('a[href]')].map(a => ({url:a.href,text:a.innerText}))
  }));
  const images = [...document.querySelectorAll('img,[data-original],[data-img-zoom-url],[style]')]
    .slice(0,200).map(el => ({tag: el.tagName.toLowerCase(),
      values: Object.fromEntries([...el.attributes].map(a => [a.name,a.value])),
      element: el.id ? el.tagName.toLowerCase() + '#' + el.id : el.tagName.toLowerCase()
    }));
  return {text, title: document.title, links, forms, images};
}"""
