"""Create explicitly synthetic DEMO inputs and run the batch without network or API."""

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path

from .auditors.models import REQUIRED_TOPICS
from .collector.artifacts import ArtifactStore
from .collector.models import CollectedEvidence, CollectionResult, Limits, PageObservation
from .collector.reading import extract_html
from .mvp_batch import run


def create_inputs(root):
    root.mkdir(parents=True, exist_ok=False)
    rows = []
    for index, company in enumerate(["DEMO Альфа", "DEMO Бета", "DEMO Гамма", "DEMO Ошибка"]):
        url = f"https://demo-{index}.example/"
        dossier = root / f"dossier-{index}"
        store = ArtifactStore(dossier, Limits())
        body = (
            "<html><body>DEMO: согласие на обработку данных включено "
            "в общий текст условий заявки.</body></html>"
        ).encode()
        text, reading = extract_html(body, 100000)
        artifacts = [
            store.save("page-0001.html", body, "html"),
            store.save("page-0001.txt", text.encode(), "text"),
            store.save("page-0001.png", b"DEMO synthetic screenshot", "screenshot"),
        ]
        now = datetime(2026, 10, 16, tzinfo=UTC)
        evidence = CollectedEvidence(
            id="page-0001",
            source=url,
            captured_at=now,
            observed_fact="DEMO saved text",
            available=True,
            status="complete",
            artifacts=artifacts,
            artifact_path=artifacts[0].path,
            sha256=artifacts[0].sha256,
        )
        collection = CollectionResult(
            target_url=url,
            started_at=now,
            limits=Limits(),
            evidence=[evidence],
            pages=[
                PageObservation(
                    requested_url=url,
                    final_url=url,
                    captured_at=now,
                    http_status=200,
                    text=text,
                    reading=reading,
                    evidence_id=evidence.id,
                )
            ],
        )
        (dossier / "collection.json").write_text(collection.model_dump_json(indent=2))
        finding = dict(
            id="demo-f1",
            topic="consent",
            claim_code="separate_consent_present",
            subject="appointment_form",
            source=url,
            evidence_ids=["page-0001"],
            fact=text,
            fact_assertion="present",
            examination_scope="text_excerpt",
            text_grounding=[dict(evidence_id="page-0001", exact_quote=text)],
            evidence_quality="complete",
            legal_position="possible_issue",
            legal_interpretation="DEMO: проверить порядок оформления согласия",
            normative_basis=[
                dict(
                    act_name="DEMO: условная норма",
                    act_id="demo",
                    provision="DEMO",
                    requirement="DEMO не правовое основание",
                )
            ],
            confidence=0.8,
            material=True,
            limitations=[],
            alternative_explanations=["Требуется проверка полного контекста"],
            additional_checks=["Проверить применимые требования"],
            status="potential",
            proposed_work=["legal_documents", "webmaster_instructions"],
        )
        response = {
            "auditor": "A",
            "findings": [] if index == 2 else [finding],
            "cookie_analytics_review": {
                "status": "NOT_VERIFIED",
                "rationale": (
                    "DEMO: синтетический фрагмент не содержит сведений о cookie и аналитике"
                ),
                "evidence": [],
                "limitations": ["Не проверены баннер, политика, JavaScript и сетевые запросы"],
            },
            "coverage": [
                {
                    "topic": topic.value,
                    "status": (
                        "insufficient_evidence" if topic.value == "cookies_analytics"
                        else "reviewed"
                    ),
                    "limitations": (
                        ["DEMO: нет сведений о cookie и аналитике"]
                        if topic.value == "cookies_analytics" else []
                    ),
                }
                for topic in sorted(REQUIRED_TOPICS)
            ],
            "limitations": ["DEMO"],
        }
        response_path = root / f"response-{index}.json"
        response_path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
        rows.append(
            dict(
                url=url,
                company_name=company,
                email="",
                dossier=str(dossier.absolute()),
                response=str(response_path.absolute()),
            )
        )
    rows[-1]["dossier"] = str(root.absolute() / "missing-dossier")
    rows.append(dict(rows[0]))
    path = root / "input.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    path = create_inputs(args.output / "DEMO-inputs")
    state = run(path, args.output / "DEMO-results", max_sites=5)
    print(f"DEMO; USD 0; {state['summary']}; {args.output}/DEMO-results/results.csv")


if __name__ == "__main__":
    main()
