"""Bounded, isolated PDF text inventory; never OCR or visual examination."""

import json
import sys


def worker():
    import resource

    from pypdf import PdfReader

    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    reader = PdfReader(sys.argv[1])
    if reader.is_encrypted or len(reader.pages) > int(sys.argv[2]):
        raise ValueError("Encrypted or oversized PDF")
    texts = {}
    remaining = 1_000_000
    truncated = False
    for index, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        limit = min(100_000, remaining)
        truncated |= len(text) > limit
        text = text[:limit]
        remaining -= len(text)
        if text.strip():
            texts[index] = text
    print(
        json.dumps({"page_count": len(reader.pages), "page_texts": texts, "truncated": truncated})
    )


if __name__ == "__main__":
    worker()
