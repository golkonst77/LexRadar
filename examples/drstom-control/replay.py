"""Build a locally reconstructed fixture and run the existing offline primary CLI export."""

import argparse
from datetime import UTC, datetime
from pathlib import Path

from lexradar.collector.artifacts import ArtifactStore
from lexradar.collector.models import CollectedEvidence, CollectionResult, Limits, PageObservation
from lexradar.collector.provenance import MaterialOrigin
from lexradar.collector.reading import extract_html
from lexradar.primary.cli import write_report
from lexradar.primary.service import audit_saved_dossier

CONTROL_DATE = datetime(2026, 10, 13, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/drstom-control"


def build_replay(output, fixtures=FIXTURES, *, base_url="https://drstom.net"):
    """Explicitly local import; no HTTP status or live acquisition/authentication claims."""
    dossier = Path(output)
    store = ArtifactStore(dossier, Limits())
    origin = MaterialOrigin(
        kind="manual_html_import",
        acquisition_method="reconstructed_user_control_fixture",
        reported_capture_date="2026-10-13",
        imported_at=CONTROL_DATE,
        captured_at_role="local_import",
        started_at_role="local_import",
        limitations=["Reconstructed fixture from user observations, not original website HTML"],
    )
    data = CollectionResult(
        target_url=base_url, started_at=CONTROL_DATE, limits=store.limits, origin=origin
    )
    for number, file in enumerate(sorted(Path(fixtures).glob("*.html")), 1):
        eid = f"page-{number:04d}"
        url = base_url.rstrip("/") + ("/" if file.stem == "index" else "/" + file.stem)
        raw = file.read_bytes()
        html = store.save(eid + ".html", raw, "html")
        text, reading = extract_html(raw, 100_000)
        derivative = store.save(eid + ".txt", text.encode(), "text")
        page_origin = origin.model_copy(
            update={"declared_source": url, "source_sha256": html.sha256}
        )
        data.pages.append(
            PageObservation(
                requested_url=url,
                captured_at=CONTROL_DATE,
                origin=page_origin,
                evidence_id=eid,
                text=text,
                reading=reading,
            )
        )
        data.evidence.append(
            CollectedEvidence(
                id=eid,
                source=url,
                captured_at=CONTROL_DATE,
                observed_fact="Local reconstructed fixture; no browser capture",
                status="partial",
                available=True,
                collection_errors=["No live screenshot; reconstructed local fixture"],
                artifacts=[html, derivative],
                artifact_path=html.path,
                sha256=html.sha256,
            )
        )
    (dossier / "collection.json").write_text(data.model_dump_json(indent=2), encoding="utf-8")
    return dossier


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dossier = build_replay(args.output / "dossier")
    report = audit_saved_dossier(
        dossier,
        sector="medical",
        as_of=CONTROL_DATE,
        registry_path=Path("examples/legal-registry.json"),
    )
    write_report(report, args.output / "primary", dossier)
    print(
        f"Reconstructed control: {len(report.forms)} forms; "
        f"{len(report.observations)} observations; HOLD"
    )
