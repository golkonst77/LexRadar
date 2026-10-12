"""Positive bounded screening questions. Never infer legal absence from a limited crawl."""

import re
from urllib.parse import urlsplit


def identity_statements(research):
    rows = []
    for eid, parser in research.markup.items():
        if parser.inspection().status != "complete" or eid not in research.originals:
            continue
        artifact, _ = research.originals[eid]
        from .primary.markup import compact

        rows.append(
            {
                "citations": [
                    {
                        "quote": compact(parser.parts),
                        "evidence_id": eid,
                        "url": str(research.evidence[eid].source),
                        "artifact_path": artifact.path,
                        "artifact_sha256": artifact.sha256,
                        "static_span": parser.span((1, 0), parser.end_position).model_dump(
                            mode="json"
                        ),
                    }
                ]
            }
        )
    return rows


def signals(primary, policies, collection=None, identities=()):
    result = []

    def add(kind, text, citations, strength=60):
        result.append(
            {
                "kind": kind,
                "reason": text,
                "citations": citations,
                "evidence_strength": strength,
                "normative_status": "research_question",
                "conditions_to_check": [
                    "Operator, purposes, exact norm/date and applicability",
                    "Related documents and contrary explanations",
                ],
                "violation_confirmed": False,
                "paid_work_needed": False,
            }
        )

    for policy in policies:
        if policy["reading_completeness"] != "complete":
            continue
        text = policy["text"]
        host = urlsplit(policy["source_url"]).hostname or ""
        citation = {
            k: policy[k]
            for k in ("evidence_id", "source_url", "artifact_path", "artifact_sha256", "block")
        }
        domain = re.search(
            r"политик[а-я\s]*конфиденциальности\s+интернет-сайта\s+([a-z0-9.-]+\.[a-z]{2,})",
            text.lower(),
        )
        explicit_scope = re.search(
            r"распространяется на сайты?[^;]{0,200}" + re.escape(host), text.lower()
        )
        if domain and domain[1].rstrip(".").removeprefix("www.") != host.removeprefix("www."):
            if not explicit_scope:
                add(
                    "document_domain_mismatch",
                    "Policy title names another domain; scope unresolved",
                    [{**citation, "quote": domain[0]}],
                )
        operator = re.search(
            r"Оператор\s*[:—–-]\s*[^.]{0,120}?ИНН\s*[: ]\s*(\d{10,12})\b", text, re.I
        )
        if operator and "по поручению" not in text.lower():
            for observation in identities:
                # Role is explicit in the text, not assigned from the number of mentions.
                description = " ".join(
                    c.get("quote") or "" for c in observation.get("citations", [])
                )
                other = re.search(
                    r"Реквизиты исполнителя\s*[:—–-][^.]{0,120}?ИНН\s*[: ]\s*"
                    r"(\d{10,12})\b",
                    description,
                    re.I,
                )
                if other and operator[1] != other[1] and observation.get("citations"):
                    add(
                        "declared_role_identifiers_differ",
                        "Explicit operator/executor identifiers differ; roles may be legitimate",
                        [{**citation, "quote": operator[0]}, *observation["citations"]],
                    )
        denies_phone = re.search(
            r"оператор не собирает (?:номера телефонов|телефонные номера)\s*[.]", text, re.I
        )
        conditional = re.search(r"кроме|за исключением|если", text, re.I)
        if denies_phone and not conditional:
            for form in primary.get("forms", []):
                if (
                    form.get("static_coverage", {}).get("status") == "complete"
                    and str(form["url"]) == policy["source_url"]
                    and any(
                        f["type"] == "tel" and f.get("name") != "catcher" for f in form["fields"]
                    )
                ):
                    add(
                        "policy_form_phone_contradiction",
                        "Policy denies phone collection; complete saved form declares phone",
                        [{**citation, "quote": denies_phone[0]}, form["citation"]],
                        70,
                    )
    for form in primary.get("forms", []):
        if form.get("static_coverage", {}).get("status") != "complete":
            continue
        if collection and collection.get("manifest_sha256"):
            for link in form.get("policy_links", []):
                attempts = [p for p in collection.get("pages", []) if p["requested_url"] == link]
                if not attempts or any(p.get("http_status") not in {404, 410} for p in attempts):
                    continue
                for page in attempts:
                    if page["requested_url"] == link and page.get("http_status") in {404, 410}:
                        add(
                            "linked_policy_http_error",
                            f"Linked policy returned HTTP {page['http_status']} in recorded GET; "
                            "not proof of absence or live interface failure",
                            [
                                form["citation"],
                                {
                                    "evidence_id": page["evidence_id"],
                                    "source_url": page["requested_url"],
                                    "http_status": page["http_status"],
                                    "captured_at": page["captured_at"],
                                    "artifact_path": collection.get(
                                        "manifest_path", "collection.json"
                                    ),
                                    "artifact_sha256": collection["manifest_sha256"],
                                    "basis": "recorded_collection_metadata",
                                },
                            ],
                        )
    return result
