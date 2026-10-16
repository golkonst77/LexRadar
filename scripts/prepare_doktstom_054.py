"""Offline derivative import; no collector changes, network, redaction or approval."""

import argparse
import hashlib
import json
import re
import shutil
import socket
from datetime import UTC, datetime
from pathlib import Path

from lexradar.auditors.evidence import load_packet
from lexradar.auditors.preflight import authorize, screen
from lexradar.collector.artifacts import ArtifactStore, verify_integrity
from lexradar.collector.models import CollectionResult
from lexradar.collector.provenance import MaterialOrigin
from lexradar.collector.reading import extract_html
from lexradar.primary.markup import SavedMarkup, compact
from lexradar.single_auditor import MAX_INPUT_TOKENS_BOUND, messages_for, profile


def digest(body):
    return hashlib.sha256(body).hexdigest()


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize(body, url):
    """Apply only the existing recovery parser's unambiguous token dispositions."""
    source = body.decode("utf-8", errors="strict")
    parser = SavedMarkup(url, recover=True)
    parser.feed(source)
    parser.close()
    if parser.quarantined:
        raise ValueError("Quarantined suffix cannot be imported")
    lines = source.splitlines(keepends=True)
    edits = []
    for diagnostic in parser.diagnostics:
        pos = diagnostic.position
        offset = sum(len(line) for line in lines[: pos.line - 1]) + pos.column
        end = source.find(">", offset) + 1
        token = source[offset:end]
        if diagnostic.action == "ignored_stray_close" and re.fullmatch(r"</[a-z]+>", token):
            replacement = ""
        elif diagnostic.action == "ignored_ambiguous_attributes":
            match = re.match(r"<([a-z]+)\b", token)
            if not match:
                raise ValueError("Unsupported attribute token")
            replacement = f"<{match[1]}>"
        else:
            raise ValueError("Unsupported recovery action")
        edits.append(
            dict(
                offset=offset,
                end_exclusive=end,
                original=token,
                replacement=replacement,
                diagnostic=diagnostic.model_dump(mode="json"),
            )
        )
    derived = source
    for edit in reversed(edits):
        derived = derived[: edit["offset"]] + edit["replacement"] + derived[edit["end_exclusive"] :]
    text, reading = extract_html(derived.encode(), 1_000_000)
    if reading.extraction_state != "complete" or text != compact(parser.parts):
        raise ValueError("Normalized extraction must exactly reproduce recovered text")
    return derived.encode(), text, reading, edits, parser.inspection().model_dump(mode="json")


def prepare(source, output, imported_at=None):
    source = source.resolve()
    output = output.resolve()
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Output must be separate from source")
    original = load_packet(source, profile().max_input_bytes)
    if len(original.data.pages) != 1 or original.data.documents:
        raise ValueError("This small import script expects one HTML page and no PDFs")
    data = original.data.model_copy(deep=True)
    page = data.pages[0]
    evidence = next(e for e in data.evidence if e.id == page.evidence_id)
    artifact = next(a for a in evidence.artifacts if a.kind == "html")
    raw = (source / artifact.path).read_bytes()
    derived, text, reading, edits, inspection = normalize(raw, str(page.requested_url))
    output.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source, output / "original-dossier")
    root = output / "dossier"
    store = ArtifactStore(root, data.limits)
    html = store.save("page-0001.html", derived, "html")
    txt = store.save("page-0001.txt", text.encode(), "text")
    stamp = imported_at or datetime.now(UTC)
    limitations = [
        "Local derivative: token repairs only; no HTTP request or publisher authentication",
        f"Original HTML SHA256 {digest(raw)}; original dossier manifest {original.manifest_sha256}",
        (
            "Recovered ORIGINAL HTML coverage is partial; "
            "complete derivative extraction is not complete source review"
        ),
        (
            "CSS visibility, JavaScript, external resources, "
            "legal completeness and current site not examined"
        ),
        "No redaction; original form observations retained, not re-observed on a live website",
    ]
    origin = MaterialOrigin(
        kind="manual_html_import",
        acquisition_method="local_saved_html_token_repair",
        declared_source=str(page.requested_url),
        source_sha256=html.sha256,
        original_capture_at=page.captured_at,
        imported_at=stamp,
        captured_at_role="local_import",
        started_at_role="local_import",
        limitations=limitations,
    )
    forms = [
        form.model_copy(
            update={
                "screenshot": None,
                "unavailable_reason": (
                    "Original screenshot archived separately; no derivative screenshot generated"
                ),
            }
        )
        for form in page.forms
    ]
    page = page.model_copy(
        update=dict(
            origin=origin,
            captured_at=stamp,
            http_status=None,
            final_url=None,
            text=text,
            reading=reading,
            forms=forms,
        )
    )
    evidence = evidence.model_copy(
        update=dict(
            captured_at=stamp,
            artifacts=[html, txt],
            artifact_path=html.path,
            sha256=html.sha256,
            status="partial",
            collection_errors=[
                "Derivative screenshot unavailable; original source recovery partial"
            ],
            observed_fact=(
                "Locally repaired saved HTML; embedded policy retained; original recovery partial"
            ),
        )
    )
    data = data.model_copy(
        update=dict(
            origin=origin,
            started_at=stamp,
            pages=[page],
            evidence=[evidence],
            warnings=[*data.warnings, *limitations],
        )
    )
    data = CollectionResult.model_validate(data.model_dump(mode="json"))
    save_json(root / "collection.json", data.model_dump(mode="json"))
    packet = load_packet(root, profile().max_input_bytes)
    assert not verify_integrity(packet.data, root)
    assert packet.data.pages[0].text == text
    assert packet.data.pages[0].reading.provenance == "reproduced"
    assert load_packet(root, profile().max_input_bytes).payload == packet.payload
    messages, _, prompt_sha = messages_for("A", packet)
    bound = sum(len(m["content"].encode()) for m in messages) + 1024
    assert bound <= MAX_INPUT_TOKENS_BOUND
    try:
        authorize(packet, None)
    except PermissionError as exc:
        authorization = str(exc)
    else:
        raise AssertionError("Import must not authorize transfer")
    (output / "packet.json").write_text(packet.payload, encoding="utf-8")
    provenance = dict(
        source_dossier=str(source),
        source_manifest_sha256=original.manifest_sha256,
        source_packet_sha256=original.sha256,
        source_url=str(page.requested_url),
        original_html_sha256=digest(raw),
        derivative_html_sha256=digest(derived),
        transformation=(
            "Only listed parser-directed token edits; all recovered text unchanged; no redaction"
        ),
        edits=edits,
        original_static_inspection=inspection,
        original_files={
            p.relative_to(source).as_posix(): digest(p.read_bytes())
            for p in sorted(source.rglob("*"))
            if p.is_file()
        },
    )
    save_json(output / "provenance.json", provenance)
    metrics = dict(
        pages=len(data.pages),
        pdf_documents=len(data.documents),
        extracted_chars=len(text),
        original_extracted_chars=len(original.data.pages[0].text or ""),
        payload_bytes=len(packet.payload.encode()),
        original_payload_bytes=len(original.payload.encode()),
        manifest_sha256=packet.manifest_sha256,
        packet_sha256=packet.sha256,
        messages_content_bytes=bound - 1024,
        input_bound=bound,
        prompt_sha256=prompt_sha,
        integrity_issues=verify_integrity(packet.data, root),
        screening=screen(packet),
        authorization=authorization,
        readiness="NOT READY",
        transfer_allowed=False,
    )
    save_json(output / "validation.json", metrics)
    assert load_packet(source, profile().max_input_bytes).sha256 == original.sha256
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    def blocked(*args, **kwargs):
        raise RuntimeError("Network disabled for offline import")

    socket.getaddrinfo = socket.socket.connect = socket.socket.connect_ex = blocked
    print(json.dumps(prepare(args.source, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
