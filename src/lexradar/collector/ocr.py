"""Offline bounded OCR assistance. Never modifies originals or certifies visual/legal review."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .models import CollectionResult


def resource_bounds():
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_FSIZE, (20_000_000, 20_000_000))


def checked_path(root, relative):
    path = root / relative
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.resolve().is_relative_to(
        root.resolve()
    ):
        raise ValueError("Unsafe OCR artifact path")
    return path


def run_ocr(
    dossier,
    evidence_id,
    output,
    *,
    language="rus+eng",
    max_pages=30,
    timeout=240,
    artifact_path=None,
):
    root, output = Path(dossier).absolute(), Path(output).absolute()
    if not 1 <= max_pages <= 100 or not 1 <= timeout <= 900:
        raise ValueError("OCR limits outside bounds")
    if not re.fullmatch(r"[a-z]{3}(?:\+[a-z]{3})*", language):
        raise ValueError("Invalid OCR language")
    if output.resolve().is_relative_to(root.resolve()) or any(
        p.is_symlink() for p in (output, *output.parents)
    ):
        raise ValueError("OCR output must be new and outside the dossier")
    manifest = checked_path(root, "collection.json").read_bytes()
    data = CollectionResult.model_validate_json(manifest)
    evidence = next((e for e in data.evidence if e.id == evidence_id), None)
    if evidence is None:
        raise ValueError("Unknown evidence id")
    candidates = [a for a in evidence.artifacts if a.kind in {"pdf", "image"}]
    if artifact_path is not None:
        candidates = [a for a in candidates if a.path == artifact_path]
    if len(candidates) > 1:
        raise ValueError("Multiple document artifacts; select --artifact explicitly")
    artifact = candidates[0] if candidates else None
    if artifact is None:
        raise ValueError("No saved original PDF")
    source = checked_path(root, artifact.path)
    if source.stat().st_size > 20_000_000:
        raise ValueError("Original PDF size limit")
    raw = source.read_bytes()
    if len(raw) > 20_000_000 or hashlib.sha256(raw).hexdigest() != artifact.sha256:
        raise ValueError("Original PDF size/hash mismatch")
    if sys.platform != "linux":
        raise ValueError("Offline OCR resource controls require Linux")
    if artifact.kind == "image":
        return run_image_ocr(root, evidence, artifact, raw, manifest, output, language, timeout)
    for program in ("pdfinfo", "pdftoppm", "tesseract"):
        if not shutil.which(program):
            raise ValueError(f"Missing {program}; install poppler-utils / tesseract-ocr")
    languages = subprocess.run(
        ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10, check=True
    )
    if not set(language.split("+")).issubset(set(languages.stdout.splitlines()[1:])):
        raise ValueError("Missing OCR language; install tesseract-ocr-rus and tesseract-ocr-eng")
    # Count in a bounded subprocess before rendering untrusted PDF bytes.
    counter = subprocess.run(
        ["pdfinfo", str(source)],
        capture_output=True,
        text=True,
        timeout=min(10, timeout),
        check=True,
        preexec_fn=resource_bounds,
    )
    if re.search(r"^Encrypted:\s*yes", counter.stdout, re.M):
        raise ValueError("Encrypted PDF")
    match = re.search(r"^Pages:\s*(\d+)", counter.stdout, re.M)
    if not match:
        raise ValueError("PDF page count unavailable")
    count = int(match[1])
    if not 1 <= count <= 500:
        raise ValueError("PDF page count outside bounds")
    output.mkdir(parents=True, exist_ok=False)
    # Render immutable bytes verified above, not a path that may change during OCR.
    source = output / "original.pdf"
    source.write_bytes(raw)
    started = time.monotonic()
    output_bytes = len(raw)
    report = {
        "audience": "internal_only",
        "evidence_id": evidence_id,
        "source_url": str(evidence.source),
        "source_artifact": artifact.path,
        "source_sha256": artifact.sha256,
        "dossier_sha256": hashlib.sha256(manifest).hexdigest(),
        "language": language,
        "page_count": count,
        "pages": [],
        "ocr_complete": False,
        "visual_examined": False,
        "legal_research_completed": False,
        "client_release_allowed": False,
        "automatic_send_allowed": False,
        "notice": (
            "Unverified OCR derivative; check quotes against original page images. "
            "Not imported into legal findings."
        ),
    }

    def persist():
        (output / "ocr.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def remaining():
        value = timeout - (time.monotonic() - started)
        if value <= 0:
            raise TimeoutError("OCR total time limit")
        return min(30, value)

    for number in range(1, min(count, max_pages) + 1):
        page = {"page": number, "status": "failed", "error": None}
        report["pages"].append(page)
        prefix = output / f"page-{number:04d}"
        try:
            subprocess.run(
                [
                    "pdftoppm",
                    "-f",
                    str(number),
                    "-l",
                    str(number),
                    "-singlefile",
                    "-scale-to",
                    "2200",
                    "-png",
                    str(source),
                    str(prefix),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=remaining(),
                check=True,
                preexec_fn=resource_bounds,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
            picture = prefix.with_suffix(".png")
            if picture.stat().st_size > 20_000_000:
                raise ValueError("Rendered image size limit")
            output_bytes += picture.stat().st_size
            if output_bytes > 100_000_000:
                raise ValueError("OCR output byte limit")
            subprocess.run(
                ["tesseract", str(picture), str(prefix), "-l", language, "--psm", "3"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=remaining(),
                check=True,
                preexec_fn=resource_bounds,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
            textfile = prefix.with_suffix(".txt")
            if textfile.stat().st_size > 400_000:
                raise ValueError("OCR text size limit")
            output_bytes += textfile.stat().st_size
            if output_bytes > 100_000_000:
                raise ValueError("OCR output byte limit")
            text = textfile.read_text()
            page.update(
                status="ocr_text" if text.strip() else "no_text",
                image=picture.name,
                image_sha256=hashlib.sha256(picture.read_bytes()).hexdigest(),
                text=textfile.name,
                text_sha256=hashlib.sha256(textfile.read_bytes()).hexdigest(),
                chars=len(text),
            )
        except (OSError, ValueError, subprocess.SubprocessError, TimeoutError) as exc:
            page["error"] = str(exc)[:300]
        persist()
        if output_bytes > 100_000_000 or timeout - (time.monotonic() - started) <= 0:
            break
    report["unprocessed_pages"] = count - len(report["pages"])
    report["ocr_complete"] = len(report["pages"]) == count and all(
        p["status"] == "ocr_text" for p in report["pages"]
    )
    persist()
    return report


def run_image_ocr(root, evidence, artifact, raw, manifest, output, language, timeout):
    """Same bounded local reader for saved images; derivative never grants legal trust."""
    if not shutil.which("tesseract"):
        raise ValueError("Missing tesseract; image remains not_read")
    output.mkdir(parents=True, exist_ok=False)
    source = output / "original.image"
    source.write_bytes(raw)
    report = {
        "audience": "internal_only",
        "evidence_id": evidence.id,
        "source_url": str(evidence.source),
        "source_artifact": artifact.path,
        "source_sha256": artifact.sha256,
        "dossier_sha256": hashlib.sha256(manifest).hexdigest(),
        "language": language,
        "page_count": 1,
        "pages": [],
        "ocr_complete": False,
        "visual_examined": False,
        "legal_research_completed": False,
        "client_release_allowed": False,
        "automatic_send_allowed": False,
        "notice": "Unverified image OCR; inspect original. Not imported into legal findings.",
    }
    page = {"page": 1, "status": "failed", "error": None, "confidence": None}
    report["pages"].append(page)
    prefix = output / "page-0001"
    try:
        subprocess.run(
            ["tesseract", str(source), str(prefix), "-l", language, "--psm", "3", "txt", "tsv"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=min(timeout, 30),
            check=True,
            preexec_fn=resource_bounds,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
        textfile, tablefile = prefix.with_suffix(".txt"), prefix.with_suffix(".tsv")
        if textfile.stat().st_size > 400_000 or tablefile.stat().st_size > 4_000_000:
            raise ValueError("Image OCR derivative size limit")
        text = textfile.read_text(encoding="utf-8")
        confidences = []
        for row in tablefile.read_text(encoding="utf-8").splitlines()[1:]:
            columns = row.split("\t")
            if len(columns) == 12 and columns[-1].strip():
                value = float(columns[-2])
                if 0 <= value <= 100:
                    confidences.append(value)
        confidence = sum(confidences) / len(confidences) if confidences else None
        status = (
            "no_text"
            if not text.strip()
            else "low_confidence"
            if confidence is None or confidence < 70
            else "ocr_text"
        )
        page.update(
            status=status,
            confidence=confidence,
            text=textfile.name,
            text_sha256=hashlib.sha256(textfile.read_bytes()).hexdigest(),
            chars=len(text),
        )
        report["ocr_complete"] = status == "ocr_text"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        page["error"] = str(exc)[:300]
        page["status"] = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "failed"
    (output / "ocr.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def ocr_main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dossier", type=Path)
    parser.add_argument("--evidence", required=True)
    parser.add_argument(
        "--artifact", help="Select a saved image/PDF when evidence has several documents"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--language", default="rus+eng")
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args(argv)
    try:
        report = run_ocr(
            args.dossier,
            args.evidence,
            args.output,
            language=args.language,
            max_pages=args.max_pages,
            timeout=args.timeout,
            artifact_path=args.artifact,
        )
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"OCR unavailable: {exc}\n")
    print(
        f"OCR pages: {len(report['pages'])}/{report['page_count']}; "
        f"complete: {report['ocr_complete']}; internal only"
    )
