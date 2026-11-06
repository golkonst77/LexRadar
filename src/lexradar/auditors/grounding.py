"""Exact excerpt locations in reproduced sources; never semantic/legal inference."""


def quote_locations(packet, quote):
    """Return every literal occurrence, including repeated notices on different pages."""
    if not quote.strip():
        return []
    evidence = {e.id: e for e in packet.data.evidence}
    regions = [(p.evidence_id, None, p.text or "", p.reading) for p in packet.data.pages]
    for document in packet.data.documents:
        regions.extend(
            (document.evidence_id, n, text, document.reading)
            for n, text in document.page_texts.items()
        )
    matches = []
    for eid, page, text, reading in regions:
        item = evidence[eid]
        if not item.available or reading.provenance != "reproduced":
            continue
        start = text.find(quote)
        while start >= 0:
            matches.append(
                dict(
                    evidence_id=eid,
                    source=str(item.source),
                    page=page,
                    start=start,
                    end=start + len(quote),
                    exact_quote=quote,
                )
            )
            start = text.find(quote, start + 1)
    return matches


def check_quote(packet, quote):
    """An evidence ID owns its URL; a finding may legitimately cite several URLs."""
    matches = quote_locations(packet, quote.exact_quote)
    own = [m for m in matches if m["evidence_id"] == quote.evidence_id and m["page"] == quote.page]
    if not own:
        status = "WRONG_SOURCE" if matches else "NOT_FOUND"
    else:
        precise = [
            m
            for m in own
            if (quote.start is None or quote.start == m["start"])
            and (quote.end is None or quote.end == m["end"])
        ]
        status = "MATCH" if precise else "BAD_OFFSETS"
    return dict(status=status, declared=quote.model_dump(mode="json"), matches=matches)


def exact_locations(packet, evidence_ids, source, quote):
    # Keep the legacy unique-span contract for callers that bind one exact location.
    matches = [
        {k: value for k, value in match.items() if k != "source"}
        for match in quote_locations(packet, quote)
        if match["evidence_id"] in evidence_ids and match["source"] == str(source)
    ]
    return matches if len(matches) == 1 else []
