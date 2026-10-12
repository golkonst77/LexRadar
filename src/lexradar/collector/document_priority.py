"""Discovery hints for download order, never legal relevance determinations."""

from urllib.parse import unquote, urlsplit

DISCLOSURES = {
    "privacy_policy": ("политик", "персональн", "privacy", "policy", "politika"),
    "consent": ("соглас", "consent", "soglas"),
    "license": ("лиценз", "license", "licence", "licenz"),
    "service_rules": ("правила", "договор", "оферт", "rules", "contract", "oferta"),
    "organization_details": ("реквизит", "инн", "огрн", "requisites", "rekvizit"),
    "prices": ("прайс", "стоимост", "цены", "price", "tariff", "preiskurant"),
}


def document_hints(url: str, label: str, context: str = "") -> list[str]:
    # A document's own label/path outrank contextual hints from its parent page.
    try:
        path = unquote(urlsplit(url).path)
    except ValueError:
        path = ""
    direct = (path + " " + label).casefold()
    matches = [name for name, words in DISCLOSURES.items() if any(w in direct for w in words)]
    if matches:
        return matches
    return [
        "context:" + name
        for name, words in DISCLOSURES.items()
        if any(w in context.casefold() for w in words)
    ]


def document_sort_key(url: str, hints: list[str]):
    return (0 if any(not h.startswith("context:") for h in hints) else 1 if hints else 2, url)
