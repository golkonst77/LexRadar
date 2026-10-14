"""Offline compact primary snapshot with lossless evidence and local metadata."""

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..auditors.evidence import load_packet
from ..auditors.preflight import authorize, write_preflight
from .service import audit_saved_dossier

MAX_BYTES = 40_000


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class PacketOverflow(ValueError):
    """Required evidence cannot fit; nothing may be sent."""


@dataclass(frozen=True)
class CompactPacket:
    payload: str
    sha256: str
    primary_json: str
    trace_json: str
    as_of: datetime


def prepare(root: Path, *, as_of: datetime, sector="unknown") -> CompactPacket:
    """Explicit assessment time makes replay deterministic; retain all primary facts."""
    report = audit_saved_dossier(root, as_of=as_of, sector=sector)
    original = report.model_dump(mode="json")
    traces = {}
    excerpts = {}
    citation_ids = {}

    def project(value):
        if isinstance(value, dict):
            if {"basis", "evidence_id", "artifact_sha256", "quote"} <= value.keys():
                identity = canonical(value)
                key = citation_ids.setdefault(identity, f"e{len(citation_ids)}")
                if key in traces and traces[key] != value:
                    raise ValueError("Excerpt ID collision")
                traces[key] = value
                excerpts[key] = {
                    k: value[k]
                    for k in ("evidence_id", "url", "basis", "quote", "page", "static_span")
                }
                return {"excerpt": key}
            return {k: project(v) for k, v in value.items()}
        if isinstance(value, list):
            return [project(v) for v in value]
        return value

    projected = project(original)
    # Commercial packaging is local; retain all facts, hypotheses and further checks.
    projected.pop("commercial_opportunity")
    for material in projected["materials"]:
        material.pop("artifacts")
    for form in projected["forms"]:
        form.pop("screenshot")
        form.pop("screenshot_scope")
    for stage in projected["stages"]:
        stage.pop("possible_paid_legal_work")
        stage.pop("paid_work_rationale")
    # Content-addressed object pooling removes repeated metadata without dropping facts.
    counts = Counter()

    def count(value):
        encoded = canonical(value)
        if isinstance(value, (str, dict, list)) and len(encoded.encode()) >= 60:
            counts[encoded] += 1
        if isinstance(value, dict):
            for child in value.values():
                count(child)
        elif isinstance(value, list):
            for child in value:
                count(child)

    body = {"primary": projected, "excerpts": excerpts}
    count(body)
    field_names = set()

    def fields(value):
        if isinstance(value, dict):
            field_names.update(value)
            for child in value.values():
                fields(child)
        elif isinstance(value, list):
            for child in value:
                fields(child)

    fields(body)
    keys = {name: str(i) for i, name in enumerate(sorted(field_names))}
    shared = sorted(k for k, n in counts.items() if n > 1)
    ids = {key: str(i) for i, key in enumerate(shared)}

    def pack(value, *, top=False):
        encoded = canonical(value)
        if not top and encoded in ids:
            return {"$": ids[encoded]}
        if isinstance(value, dict):
            return {keys[k]: pack(v) for k, v in value.items()}
        if isinstance(value, list):
            return [pack(v) for v in value]
        return value

    payload = canonical(
        {
            "schema": "compact-primary-v1",
            "encoding": "recursive_dictionary_refs_v1",
            "dictionary": {ids[k]: pack(json.loads(k), top=True) for k in shared},
            "keys": {v: k for k, v in keys.items()},
            "body": pack(body),
            "primary_sha256": digest(canonical(original)),
            "omissions": [
                "commercial_opportunity",
                "materials.*.artifacts",
                "forms.*.screenshot",
                "forms.*.screenshot_scope",
                "stages.*.possible_paid_legal_work",
                "stages.*.paid_work_rationale",
            ],
            "production_outcome": "HOLD",
            "legal_status": "not_confirmed",
            "client_release_allowed": False,
        }
    )
    if len(payload.encode()) > MAX_BYTES:
        raise PacketOverflow(
            f"packet_overflow/review_required: {len(payload.encode())} > 40000 bytes"
        )
    packet = CompactPacket(payload, digest(payload), canonical(original), canonical(traces), as_of)
    verify_quotes(root, packet)
    return packet


def unpack(packet):
    wire = json.loads(packet.payload)

    def expand(value):
        if isinstance(value, dict):
            if set(value) == {"$"}:
                return expand(wire["dictionary"][value["$"]])
            return {wire["keys"][k]: expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v) for v in value]
        return value

    return expand(wire["body"])


def verify_quotes(root, packet):
    """Reproduce originals first; verify quotes against sent excerpts and source text."""
    if digest(packet.payload) != packet.sha256:
        raise ValueError("Compact snapshot hash mismatch")
    source = load_packet(root, 2_000_000)
    primary = json.loads(packet.primary_json)
    if source.manifest_sha256 != primary["dossier_sha256"] or json.loads(packet.payload)[
        "primary_sha256"
    ] != digest(packet.primary_json):
        raise ValueError("Original manifest or primary snapshot changed")
    body = unpack(packet)
    traces = json.loads(packet.trace_json)
    materials = {v.evidence_id: v for v in [*source.data.pages, *source.data.documents]}
    for key, excerpt in body["excerpts"].items():
        trace = traces[key]
        if any(excerpt[k] != trace[k] for k in excerpt):
            raise ValueError("Sent excerpt differs from local trace")
        evidence = next(e for e in source.data.evidence if e.id == trace["evidence_id"])
        if str(evidence.source) != trace["url"]:
            raise ValueError("Citation source mismatch")
        if trace["artifact_path"] is not None and not any(
            a.path == trace["artifact_path"] and a.sha256 == trace["artifact_sha256"]
            for a in evidence.artifacts
        ):
            raise ValueError("Citation artifact mismatch")
        quote = excerpt["quote"]
        if not quote:
            continue
        material = materials[trace["evidence_id"]]
        if trace["basis"] == "saved_html":
            path = root / trace["artifact_path"]
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != trace["artifact_sha256"]:
                raise ValueError("Original changed")
            text = raw.decode("utf-8")
            if span := trace["static_span"]:
                lines = text.splitlines(keepends=True)
                start, end = span["start"], span["end_exclusive"]
                a = sum(map(len, lines[: start["line"] - 1])) + start["column"]
                b = sum(map(len, lines[: end["line"] - 1])) + end["column"]
                text = text[a:b]
        elif trace["basis"] == "reproduced_text":
            text = (
                material.page_texts.get(trace["page"], "")
                if trace["page"] is not None and hasattr(material, "page_texts")
                else material.text or ""
            )
        else:
            raise ValueError("Manifest metadata cannot ground a quote")
        if quote not in text:
            raise ValueError("Quote not reproduced from original scope")


def verify_quote(packet, excerpt_id, quote):
    if digest(packet.payload) != packet.sha256:
        raise ValueError("Compact snapshot hash mismatch")
    sent = unpack(packet)["excerpts"].get(excerpt_id, {}).get("quote")
    if not quote or not sent or quote not in sent:
        raise ValueError("Quote absent from sent excerpt")


def preflight(root, packet, output, *, sector="unknown", approval_template=True):
    fresh = prepare(root, as_of=packet.as_of, sector=sector)
    if fresh != packet:
        raise ValueError("Source or compact snapshot changed")
    output = output.absolute()
    if output.resolve().is_relative_to(root.resolve()) or any(
        p.is_symlink() for p in (output, *output.parents)
    ):
        raise ValueError("Output must be outside source and not use symlinks")
    write_preflight(packet, output, approval_template=approval_template)
    (output / "primary.local.json").write_text(packet.primary_json, encoding="utf-8")
    (output / "trace-map.local.json").write_text(packet.trace_json, encoding="utf-8")
    # All strings remain literal in the final wire JSON and are screened there.
    return packet.sha256


def approve(root, packet, approval_path, *, sector="unknown"):
    if prepare(root, as_of=packet.as_of, sector=sector) != packet:
        raise ValueError("Source or snapshot changed")
    return authorize(packet, approval_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dossier", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    parser.add_argument("--sector", choices=("medical", "unknown"), default="unknown")
    args = parser.parse_args(argv)
    try:
        packet = prepare(args.dossier, as_of=args.as_of, sector=args.sector)
        preflight(args.dossier, packet, args.output, sector=args.sector)
    except (ValueError, OSError, RecursionError, KeyError):
        parser.error("Compact prepare failed: review_required; no transfer performed")
    print(f"{len(packet.payload.encode())} bytes; SHA-256 {packet.sha256}; HOLD; offline only")


if __name__ == "__main__":
    main()
