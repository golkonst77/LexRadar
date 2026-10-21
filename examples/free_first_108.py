"""Offline SYNTHETIC fixtures, never real clients or live network requests.

PYTHONPATH=src:. python examples/free_first_108.py /tmp/lexradar-demo
"""

import json
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

from lexradar.collector.artifacts import ArtifactStore
from lexradar.collector.models import CollectedEvidence, CollectionResult, Limits, PageObservation
from lexradar.collector.reading import extract_html
from lexradar.free_first import run

POLICY = (
    "<p>Срок хранения заявок — 30 дней. Срок хранения заявок — 90 дней.</p>"
    "<p>После завершения обработки удаление выполняет администратор.</p>"
    "<p>Передача третьим лицам осуществляется по договору для обработки заявки.</p>"
)
SEPARATE = (
    "<p>Согласие на обработку персональных данных включено в текст договора "
    "и отдельно не оформляется.</p>"
)
FORM = (
    '<form><input type="text" name="name"><input type="tel" name="phone">'
    '<label><input type="checkbox" name="consent" checked>Согласие на обработку '
    'персональных данных</label>{extra}<button type="submit">Записаться</button></form>'
)


def make_dossier(root, *, policy=POLICY, extra="", form=True, analytics=False):
    store = ArtifactStore(root, Limits())
    pages, evidence = [], []
    bodies = [
        (
            "/",
            "<h1>СИНТЕТИЧЕСКИЙ ПРИМЕР. Организация N</h1>"
            + (FORM.format(extra=extra) if form else "")
            + ('<script src="https://mc.yandex.ru/metrika/tag.js"></script>' if analytics else ""),
        ),
        ("/privacy-policy", policy),
    ]
    for index, (path, body) in enumerate(bodies, 1):
        eid = f"page-{index:04d}"
        raw = (
            "<!doctype html><html><head><title>Синтетический пример</title></head><body>"
            + body
            + "</body></html>"
        ).encode()
        text, reading = extract_html(raw, 100_000)
        artifacts = [
            store.save(eid + ".html", raw, "html"),
            store.save(eid + ".txt", text.encode(), "text"),
            store.save(eid + ".png", b"synthetic-placeholder-not-a-browser-capture", "screenshot"),
        ]
        url = "https://business.example.invalid" + path
        evidence.append(
            CollectedEvidence(
                id=eid,
                source=url,
                captured_at=datetime.now(UTC),
                observed_fact="Synthetic offline fixture",
                available=True,
                status="complete",
                artifacts=artifacts,
                artifact_path=artifacts[0].path,
                sha256=artifacts[0].sha256,
            )
        )
        pages.append(
            PageObservation(
                requested_url=url,
                final_url=url,
                http_status=200,
                title="Политика персональных данных" if index == 2 else "Пример",
                captured_at=datetime.now(UTC),
                text=text,
                reading=reading,
                evidence_id=eid,
            )
        )
    result = CollectionResult(
        target_url="https://business.example.invalid/",
        started_at=datetime.now(UTC),
        limits=Limits(),
        pages=pages,
        evidence=evidence,
    )
    (root / "collection.json").write_text(result.model_dump_json(indent=2))
    return root


def main():
    def blocked(*args, **kwargs):
        raise AssertionError("Network prohibited in offline demonstration")

    socket.socket.connect = blocked
    socket.getaddrinfo = blocked
    root = Path(sys.argv[1])
    root.mkdir(parents=True, exist_ok=False)
    go = run(make_dossier(root / "go-dossier"), root / "go")
    pending = run(
        make_dossier(root / "pending-dossier", policy="<p>Документ доступен.</p>"), root / "pending"
    )
    assert go["outcome"] == "GO" and go["new_ai_calls"] == 0
    assert (
        pending["outcome"] == "AI_PENDING_APPROVAL"
        and not (root / "pending/proposal.html").exists()
    )
    print(json.dumps({"go": go["outcome"], "pending": pending["outcome"], "ai_calls": 0}))


if __name__ == "__main__":
    main()
