"""Inventory distinguishes supplied text, acknowledged text examination and unread images."""

import json
from pathlib import Path

from ..auditors.evidence import EvidencePacket
from .models import Completeness, IndependentResult, Material


def inventory(packet: EvidencePacket, root: Path) -> list[Material]:
    result = []
    data = json.loads(packet.payload)["untrusted_collector_data"]
    evidence = {e.id: e for e in packet.data.evidence}
    for page in data["pages"]:
        e = evidence[page["evidence_id"]]
        evidence_page = next(p for p in packet.data.pages if p.evidence_id == e.id)
        text = page["text"] or ""
        result.append(
            Material(
                evidence_id=e.id,
                reading=evidence_page.reading,
                url=e.source,
                type="html_page",
                page_count=1,
                text_page_count=int(bool(text.strip())),
                text_available=bool(text.strip()),
                supplied_text=text,
                page_texts={1: text} if text.strip() else {},
                collection_status=e.status,
                reasons=[
                    *e.collection_errors,
                    *([e.unavailable_reason] if e.unavailable_reason else []),
                ],
            )
        )
    for document in packet.data.documents:
        e = evidence[document.evidence_id]
        artifact = next((a for a in e.artifacts if a.kind == "pdf"), None)
        material = Material(
            evidence_id=e.id,
            url=e.source,
            type="pdf" if artifact else "other_document",
            collection_status=e.status,
            visual_review_required=True,
            reasons=["PDF images and original layout not visually examined", *e.collection_errors],
        )
        material.reading = document.reading.model_copy(deep=True)
        material.page_count = document.reading.page_count
        material.page_texts = document.page_texts.copy()
        material.text_page_count = len(material.page_texts)
        material.supplied_text = document.text or ""
        material.text_available = bool(material.supplied_text.strip())
        material.reasons.extend(document.reading.limitations)
        # Text extraction never examines images or layout, including on text-bearing pages.
        material.visual_review_required = True
        material.reasons.append("Original PDF layout has not been visually examined")
        if not artifact:
            material.reasons.append(e.unavailable_reason or "No PDF artifact available")
        result.append(material)
    return result


def examined(materials: list[Material], result: IndependentResult) -> list[Material]:
    copies = [m.model_copy(deep=True) for m in materials]
    by_id = {m.evidence_id: m for m in copies}
    ids = [r.evidence_id for r in result.materials]
    if len(ids) != len(set(ids)) or set(ids) != set(by_id):
        raise ValueError("Exactly one examination record per material required")
    for review in result.materials:
        material = by_id[review.evidence_id]
        if len(review.pages_examined) != len(set(review.pages_examined)):
            raise ValueError("Duplicate examined page")
        if not review.text_examined and review.pages_examined:
            raise ValueError("Cannot examine pages without text review")
        if review.text_examined and not review.pages_examined:
            raise ValueError("Acknowledged text examination requires actual page numbers")
        if review.text_examined and not material.text_available:
            raise ValueError("Cannot examine unavailable text")
        if not set(review.pages_examined) <= material.page_texts.keys():
            raise ValueError("Claimed examination of an unread page")
        complete_numbers = {
            p.page
            for p in material.reading.pages
            if p.extraction_state == "complete" and p.text_available and p.truncated is False
        }
        material.fully_examined_text_pages = len(set(review.pages_examined) & complete_numbers)
        material.examined_pages = len(review.pages_examined)
        material.examined_page_numbers = review.pages_examined
        for page in material.reading.pages:
            if page.page in review.pages_examined:
                page.text_examination = (
                    "acknowledged_text" if page.page in complete_numbers else "acknowledged_excerpt"
                )
        material.reasons.append(review.limitation)
        if review.text_examined:
            material.examination = (
                "text_only"
                if material.reading.text_coverage_complete
                and material.page_count == material.fully_examined_text_pages
                else "partial_text"
            )
        if material.examined_pages != material.page_count:
            material.reasons.append("Whole material not examined")
    return copies


def completeness(materials: list[Material]) -> Completeness:
    reviewed = sum(m.examination != "not_examined" for m in materials)
    pdfs = [m for m in materials if m.type == "pdf"]
    known_pages = sum(m.page_count or 0 for m in pdfs)
    fully_reviewed = sum(m.fully_examined_text_pages for m in pdfs)
    return Completeness(
        pdf_full_text_review_fraction=fully_reviewed / known_pages if known_pages else None,
        pdf_page_denominator_complete=bool(pdfs)
        and all(m.page_count is not None for m in materials if m.type in {"pdf", "other_document"}),
        examined_text_materials=reviewed,
        total_materials=len(materials),
        text_review_fraction=reviewed / len(materials) if materials else None,
        fully_examined_pdf_text_pages=sum(
            m.fully_examined_text_pages for m in materials if m.type == "pdf"
        ),
        extracted_pdf_text_pages=sum(m.text_page_count for m in materials if m.type == "pdf"),
        extraction_complete_documents=sum(
            m.reading.extraction_state == "complete" for m in materials
        ),
        known_pdf_pages=sum(m.page_count or 0 for m in materials if m.type == "pdf"),
        examined_pdf_text_pages=sum(m.examined_pages for m in materials if m.type == "pdf"),
        unknown_page_count_documents=sum(m.page_count is None for m in materials),
    )
