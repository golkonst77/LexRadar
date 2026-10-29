"""Order-independent bounded excerpts of static semantic blocks."""

import re

IMPORTANT = re.compile(
    r"политик|персональн|соглас|лиценз|реквизит|ИНН|ОГРН|"
    r"платных медицинских|правила оказания|правила предоставления|"
    r"стоимост|прайс|\b(?:ООО|АО|ГБУЗ|ИП)\b|privacy|consent|license|\bцены\b",
    re.IGNORECASE,
)


def allocate_blocks(blocks: list[tuple[str, bool, int]], budget: int):
    """Max-min fair quotas within priority tiers; source order never breaks ties.

    Content with disclosure markers precedes ordinary content, then navigation.
    Truncated blocks retain a contiguous window around the first marker. Returned
    excerpts remain source text, never joined across omitted portions.
    """
    selected = []
    remaining = budget
    for tier in range(3):
        candidates = sorted(
            (b for b in blocks if (2 if b[1] else 0 if IMPORTANT.search(b[0]) else 1) == tier),
            key=lambda b: (b[0], b[1], b[2]),
        )
        quotas = [0] * len(candidates)
        active = list(range(len(candidates)))
        while remaining and active:
            share, extra = divmod(remaining, len(active))
            spent = 0
            for position, index in enumerate(active):
                amount = min(len(candidates[index][0]) - quotas[index], share + (position < extra))
                quotas[index] += amount
                spent += amount
            remaining -= spent
            active = [i for i in active if quotas[i] < len(candidates[i][0])]
        for (text, navigation, region), quota in zip(candidates, quotas, strict=True):
            if not quota:
                continue
            marker = IMPORTANT.search(text)
            start = 0
            if quota < len(text) and marker:
                start = min(max(0, marker.start() - quota // 4), len(text) - quota)
            selected.append((text[start : start + quota], navigation, region))
    return selected
