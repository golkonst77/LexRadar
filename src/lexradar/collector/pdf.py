"""One bounded PDF extraction protocol, shared by Collector and independent rechecks."""

import hashlib
import io
import subprocess
import sys
from pathlib import Path

from .reading import PageReading, PDFInventory, ReadingState


def extract_inventory(
    path: Path,
    max_pages: int,
    timeout: float,
    max_page_chars: int = 100_000,
    max_total_chars: int = 1_000_000,
) -> PDFInventory:
    if (
        not 1 <= max_pages <= 500
        or not 1 <= max_page_chars <= 100_000
        or not (1 <= max_total_chars <= 1_000_000)
    ):
        raise ValueError("Extraction limits outside safety bounds")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "lexradar.collector.pdf",
                str(path),
                str(max_pages),
                str(max_page_chars),
                str(max_total_chars),
            ],
            capture_output=True,
            timeout=timeout,
            check=True,
        )
        return PDFInventory.model_validate_json(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return PDFInventory(
            reading=ReadingState(
                extraction_state="failed",
                provenance="failed",
                limitations=["PDF extraction failed or exceeded resource/time limit"],
            )
        )


def legacy_result(inventory: PDFInventory) -> tuple[str | None, str, str | None]:
    reading = inventory.reading
    if reading.extraction_state == "failed":
        return None, "failed", "; ".join(reading.limitations)
    requires_review = not reading.text_coverage_complete
    return (
        inventory.text,
        "visual_review_required" if requires_review else "text",
        (
            "Some pages lack extracted text or text is incomplete; visual review required"
            if requires_review
            else None
        ),
    )


def extract_pdf(path: Path, max_pages: int, timeout: float) -> tuple[str | None, str, str | None]:
    """Backward-compatible v0.2 tuple; new consumers use the explicit inventory."""
    return legacy_result(extract_inventory(path, max_pages, timeout))


def worker() -> None:
    import resource

    from pypdf import PdfReader

    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    path = Path(sys.argv[1])
    limit, per_page, total = (int(v) for v in sys.argv[2:5])
    with path.open("rb") as original:
        raw = original.read(20_000_001)
    if len(raw) > 20_000_000:
        raise ValueError("PDF file safety limit")
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF")
    count = len(reader.pages)
    if not count:
        raise ValueError("Empty PDF")
    records, texts, chunks = [], {}, []
    remaining = total
    for index in range(1, min(count, 500) + 1):
        if index > limit:
            records.append(
                PageReading(
                    page=index, extraction_state="unread", limitations=["Page extraction limit"]
                )
            )
            continue
        try:
            source = reader.pages[index - 1].extract_text() or ""
        except Exception:
            records.append(
                PageReading(
                    page=index,
                    extraction_state="failed",
                    limitations=["Page text extraction failed"],
                )
            )
            continue
        if chunks and remaining:
            remaining -= 1
        text = source[: min(per_page, remaining)]
        remaining -= len(text)
        truncated = len(text) < len(source)
        chunks.append(text)
        if text.strip():
            texts[index] = text
        records.append(
            PageReading(
                page=index,
                original_chars=len(source),
                supplied_chars=len(text),
                text_available=bool(text.strip()),
                truncated=truncated,
                extraction_state="partial"
                if truncated
                else "complete"
                if source.strip()
                else "no_text",
                limitations=["Text clipped by per-page or total safety limit"]
                if truncated
                else ["No extracted text; visual inspection needed"]
                if not source.strip()
                else [],
            )
        )
    text = "\n".join(chunks)[:total]
    partial = len(records) != count or any(
        p.extraction_state in {"partial", "unread", "failed"} for p in records
    )
    complete_text = not partial and all(p.extraction_state == "complete" for p in records)
    original_chars = (
        sum(p.original_chars for p in records)
        if all(p.original_chars is not None for p in records) and len(records) == count
        else None
    )
    inventory = PDFInventory(
        text=text,
        page_texts=texts,
        reading=ReadingState(
            original_saved=True,
            source_sha256=hashlib.sha256(raw).hexdigest(),
            extraction_state="partial" if partial else "complete",
            text_layer_extracted=any(p.original_chars for p in records),
            text_truncated=any(p.truncated for p in records),
            original_chars=original_chars,
            supplied_chars=len(text),
            page_count=count,
            pages=records,
            max_page_chars=per_page,
            max_total_chars=total,
            page_limit=limit,
            provenance="reproduced",
            text_coverage_complete=complete_text,
            limitations=["Extraction is not visual inspection or legal research"],
        ),
    )
    if count > 500:
        inventory.reading.limitations.append(
            "Only first 500 pages inventoried; remaining pages unread"
        )
    print(inventory.model_dump_json())


if __name__ == "__main__":
    worker()
