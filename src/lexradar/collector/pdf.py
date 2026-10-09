"""Extract PDFs in a resource-limited child process, without OCR."""

import json
import subprocess
import sys
from pathlib import Path


def extract_pdf(path: Path, max_pages: int, timeout: float) -> tuple[str | None, str, str | None]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "lexradar.collector.pdf", str(path), str(max_pages)],
            capture_output=True,
            timeout=timeout,
            check=True,
        )
        payload = json.loads(result.stdout)
        return payload["text"], payload["status"], payload["reason"]
    except (subprocess.SubprocessError, ValueError):
        return None, "failed", "PDF extraction failed or exceeded resource/time limit"


def worker() -> None:
    import resource

    from pypdf import PdfReader

    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    reader = PdfReader(sys.argv[1])
    limit = int(sys.argv[2])
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF")
    if len(reader.pages) > limit:
        raise ValueError("PDF page limit")
    chunks = []
    visual = False
    for page in reader.pages:
        text = (page.extract_text() or "")[:100_000]
        chunks.append(text)
        visual |= not bool(text.strip())
    text = "\n".join(chunks)[:1_000_000]
    status = "visual_review_required" if visual or not text.strip() else "text"
    print(
        json.dumps(
            {
                "text": text,
                "status": status,
                "reason": "Some pages lack extracted text; visual review required"
                if status != "text"
                else None,
            }
        )
    )


if __name__ == "__main__":
    worker()
