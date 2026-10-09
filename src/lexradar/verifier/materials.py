"""Inventory distinguishes supplied text, acknowledged text examination and unread images."""

import json
import subprocess
import sys
from pathlib import Path

from ..auditors.evidence import EvidencePacket
from .models import Completeness, IndependentResult, Material


def inventory(packet: EvidencePacket, root: Path) -> list[Material]:
    result = []
    data = json.loads(packet.payload)["untrusted_collector_data"]
    evidence = {e.id: e for e in packet.data.evidence}
    for page in data["pages"]:
        e = evidence[page["evidence_id"]]
        text = page["text"] or ""
        result.append(
            Material(
                evidence_id=e.id,
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
        if artifact:
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "lexradar.verifier.pdf_inventory",
                        str(root.resolve() / artifact.path),
                        str(packet.data.limits.max_pdf_pages),
                    ],
                    capture_output=True,
                    timeout=packet.data.limits.timeout_seconds,
                    check=True,
                )
                extracted = json.loads(completed.stdout)
                material.page_count = extracted["page_count"]
                material.page_texts = {int(k): v for k, v in extracted["page_texts"].items()}
                material.text_page_count = len(material.page_texts)
                material.supplied_text = "\n".join(material.page_texts.values())
                material.text_available = bool(material.supplied_text.strip())
                if extracted["truncated"]:
                    material.reasons.append("Text truncated at extraction safety limit")
                if material.text_page_count < material.page_count:
                    material.reasons.append(
                        "Some PDF pages have no supplied text; visual review needed"
                    )
            except (subprocess.SubprocessError, ValueError, KeyError, TypeError):
                material.reasons.append(
                    "PDF inventory unavailable or extraction safety limit exceeded"
                )
        else:
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
        material.examined_pages = len(review.pages_examined)
        material.examined_page_numbers = review.pages_examined
        material.reasons.append(review.limitation)
        if review.text_examined:
            material.examination = (
                "partial_text" if material.page_count != material.examined_pages else "text_only"
            )
        if material.examined_pages != material.page_count:
            material.reasons.append("Whole material not examined")
    return copies


def completeness(materials: list[Material]) -> Completeness:
    reviewed = sum(m.examination != "not_examined" for m in materials)
    return Completeness(
        examined_text_materials=reviewed,
        total_materials=len(materials),
        text_review_fraction=reviewed / len(materials) if materials else None,
        known_pdf_pages=sum(m.page_count or 0 for m in materials if m.type == "pdf"),
        examined_pdf_text_pages=sum(m.examined_pages for m in materials if m.type == "pdf"),
        unknown_page_count_documents=sum(m.page_count is None for m in materials),
    )
