"""Exact, unique excerpt locations in the supplied packet; no semantic inference."""


def exact_locations(packet, evidence_ids, source, quote):
    if not quote or not quote.strip():
        return []
    evidence = {e.id: e for e in packet.data.evidence}
    regions = []
    for p in packet.data.pages:
        regions.append((p.evidence_id, None, p.text or "", p.reading))
    for d in packet.data.documents:
        regions.extend((d.evidence_id, n, t, d.reading) for n, t in d.page_texts.items())
    matches = []
    for eid, page, text, reading in regions:
        item = evidence.get(eid)
        if (
            eid not in evidence_ids
            or not item
            or not item.available
            or item.source != source
            or reading.provenance != "reproduced"
        ):
            continue
        start = text.find(quote)
        while start >= 0:
            matches.append(
                dict(
                    evidence_id=eid,
                    page=page,
                    exact_quote=quote,
                    start=start,
                    end=start + len(quote),
                )
            )
            start = text.find(quote, start + 1)
    return matches if len(matches) == 1 else []
