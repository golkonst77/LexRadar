"""Discovery hints for download order, never legal relevance determinations."""

from urllib.parse import unquote, urlsplit

DISCLOSURES = {
    "privacy_policy": (
        "политик",
        "персональ",
        "privacy",
        "policy",
        "politika",
        "personal-data",
        "pdn",
        "пдн",
        "personalnykh",
    ),
    "consent": (
        "соглас",
        "consent",
        "soglas",
        "сторонних оператор",
        "storonnikh",
        "reestr_pdn",
    ),
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
    tier = 0 if any(not h.startswith("context:") for h in hints) else 1 if hints else 2
    # Preserve PR #19's policy/consent preference within PR #20's broader tiers.
    direct = [h for h in hints if not h.startswith("context:")]
    priority = 0 if "privacy_policy" in direct else 1 if "consent" in direct else 2
    return (tier, priority, url)


def document_priority(description: str) -> int:
    """Compatibility helper from PR #19; use the same discovery vocabulary."""
    return document_sort_key("", document_hints("", unquote(description)))[1]
