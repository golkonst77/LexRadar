"""Reproduce derivatives from integrity-checked originals; never rewrite the dossier."""

from hashlib import sha256
from pathlib import Path

from .models import CollectionResult
from .pdf import extract_inventory
from .reading import ReadingState, extract_html


def recheck_text(data: CollectionResult, root: Path) -> list[str]:
    notes = []
    evidence = {e.id: e for e in data.evidence}
    for page in data.pages:
        e = evidence[page.evidence_id]
        original = next((a for a in e.artifacts if a.kind == "html"), None)
        derivative = next((a for a in e.artifacts if a.kind == "text"), None)
        legacy = page.reading.extraction_state == "unknown"
        if not original or not derivative:
            page.reading = ReadingState(
                original_saved=original is not None,
                provenance="missing_original",
                limitations=["Original HTML or saved derivative missing"],
            )
            page.text = None
            continue
        with (root / original.path).open("rb") as file:
            body = file.read(20_000_001)
        if len(body) > 20_000_000 or sha256(body).hexdigest() != original.sha256:
            raise ValueError("Original HTML changed during reproduction")
        reproduced, reading = extract_html(body, page.reading.max_total_chars)
        reading.original_saved = True
        reading.source_sha256 = original.sha256
        if page.text == reproduced:
            reading.provenance = "reproduced"
            reading.text_coverage_complete = (
                reading.extraction_state == "complete"
                and not reading.text_truncated
                and not legacy
                and bool(reproduced.strip())
            )
        elif legacy:
            # v0.2 DOM innerText differs from static extraction: retain only the saved excerpt.
            reading = ReadingState(
                original_saved=True,
                source_sha256=original.sha256,
                provenance="unknown",
                supplied_chars=len(page.text or ""),
                limitations=["Legacy DOM text not reproducible by static HTML extraction"],
            )
        else:
            reading.provenance = "mismatch"
            reading.text_coverage_complete = False
            reading.limitations.append("HTML derivative does not reproduce from original")
            page.text = None
        if legacy:
            reading.extraction_state = "unknown"
            reading.text_truncated = None
            reading.text_coverage_complete = False
            reading.limitations.append("Legacy metadata absent; full extraction not established")
            for item in reading.pages:
                item.extraction_state = "unknown"
        page.reading = reading
        notes.extend(f"{e.id}: {reason}" for reason in reading.limitations)
    for document in data.documents:
        e = evidence[document.evidence_id]
        original = next((a for a in e.artifacts if a.kind == "pdf"), None)
        legacy = document.reading.extraction_state == "unknown"
        if not original:
            document.reading = ReadingState(
                original_saved=False,
                provenance="missing_original",
                limitations=["No original PDF available for reproduction"],
            )
            document.text = None
            document.page_texts = {}
            continue
        extracted = extract_inventory(
            root / original.path,
            min(data.limits.max_pdf_pages, document.reading.page_limit),
            data.limits.timeout_seconds,
            document.reading.max_page_chars,
            document.reading.max_total_chars,
        )
        reading = extracted.reading
        source_matches = reading.source_sha256 == original.sha256
        reading.original_saved = True
        reading.source_sha256 = original.sha256
        matches = source_matches and (
            document.text == extracted.text
            or (not (document.text or "").strip() and not (extracted.text or "").strip())
        )
        if document.page_texts and document.page_texts != extracted.page_texts:
            matches = False
        if reading.provenance == "failed" or document.extraction_status == "failed":
            reading.provenance = "failed"
            matches = False
        elif not matches:
            reading.provenance = "mismatch"
        if not matches:
            reading.text_coverage_complete = False
            reading.limitations.append(
                "PDF derivative unavailable or does not reproduce from original"
            )
            document.text = None
            document.page_texts = {}
        else:
            document.text = extracted.text
            document.page_texts = extracted.page_texts
        if legacy:
            reading.extraction_state = "unknown"
            reading.text_coverage_complete = False
            reading.limitations.append("Legacy metadata absent; full extraction not established")
            for item in reading.pages:
                if item.extraction_state == "complete":
                    item.extraction_state = "unknown"
        document.reading = reading
        notes.extend(f"{e.id}: {reason}" for reason in reading.limitations)
    return notes
