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
DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".rtf", ".odt", ".xls", ".xlsx")


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
  return {text, title: document.title, links, forms};
}"""
