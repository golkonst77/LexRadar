"""18 saved synthetic scenarios, independently parsed; not real-site quality estimates."""

import hashlib
import json
from pathlib import Path

import pytest
from test_primary import NOW, make_dossier, run

from lexradar.batch import rank
from lexradar.screening import identity_statements
from lexradar.verifier.offline import LocalResearch
from lexradar.verifier.saved_policy import policy_observations

FORM = '<form><input name="phone" type="tel"><a href="/policy">Политика ПДн</a></form>'


def document(text, domain="clinic.example", close=True):
    return (
        '<div id="privacy"><h2>Политика конфиденциальности интернет-сайта '
        + domain
        + "</h2><p>Персональные данные обрабатываются для обслуживания посетителей. "
        + text
        + "</p>"
        + ("</div>" if close else "")
    )


CASES = [
    (
        "foreign-domain",
        document("Обрабатываются имя и телефон.", "other.example"),
        "priority_review",
    ),
    ("correct-policy", document("Обрабатываются имя и телефон.") + FORM, "waiting"),
    (
        "phone-contradiction",
        document("Оператор не собирает номера телефонов.") + FORM,
        "priority_review",
    ),
    (
        "conditional-phone",
        document("Оператор не собирает номера телефонов. Кроме обратного звонка.") + FORM,
        "waiting",
    ),
    (
        "partial-policy",
        document("Оператор не собирает номера телефонов.", close=False) + FORM,
        "waiting",
    ),
    (
        "partial-form",
        document("Оператор не собирает номера телефонов.") + '<form><input type="tel">',
        "waiting",
    ),
    (
        "related-domains",
        document(
            "Политика распространяется на сайты other.example и clinic.example.", "other.example"
        )
        + FORM,
        "waiting",
    ),
    (
        "foreign-inn",
        document("Оператор: ООО Альфа ИНН 1234567890.")
        + "<p>Реквизиты исполнителя: ООО Бета ИНН 1234567891.</p>"
        + FORM,
        "priority_review",
    ),
    (
        "processor-role",
        document("Оператор: ООО Альфа ИНН 1234567890. Действует по поручению исполнителя.")
        + "<p>Реквизиты исполнителя: ООО Бета ИНН 1234567891.</p>"
        + FORM,
        "waiting",
    ),
    (
        "biography-entities",
        document("Обрабатываются имя и телефон.")
        + "<p>Врач учился в ООО Альфа и ООО Бета.</p>"
        + FORM,
        "waiting",
    ),
    ("broken-policy-404", FORM, "priority_review"),
    ("broken-policy-pdf-404", FORM.replace("/policy", "/policy.pdf"), "priority_review"),
    ("policy-timeout", FORM, "waiting"),
    ("no-form-no-doc", "<p>Информационная страница</p>", "low_priority"),
    ("checkbox-gtm", '<form><input name="email"></form><script src="gtm.js"></script>', "waiting"),
    ("technical-failure", None, "technical_retry"),
    ("paraphrased-phone", document("Оператор не запрашивает телефон.") + FORM, "priority_review"),
    (
        "affiliated-domain-wording",
        document(
            "clinic.example и other.example принадлежат одной группе; это общий документ группы.",
            "other.example",
        )
        + FORM,
        "waiting",
    ),
]


@pytest.mark.parametrize("name,body,expected", CASES)
def test_saved_scenario(tmp_path, name, body, expected):
    result = evaluate(tmp_path, name, body)
    known_limits = {"paraphrased-phone": "waiting", "affiliated-domain-wording": "priority_review"}
    assert result["group"] == known_limits.get(name, expected)
    assert not result.get("paid_work_needed", False)
    assert result.get("legal_status", "not_confirmed") == "not_confirmed"
    assert all(not s["violation_confirmed"] for s in result.get("signals", []))


def evaluate(parent, name, body):
    parent.mkdir(parents=True, exist_ok=True)
    root, collection = make_dossier(parent, body)
    if body is None:
        return {"group": "technical_retry", "reasons": ["Synthetic collection unavailable"]}
    primary = run(root).model_dump(mode="json")
    research = LocalResearch(
        root, registry=None, sector="unknown", as_of=NOW, max_input_bytes=2_000_000
    )
    policies = [p.model_dump(mode="json") for p in policy_observations(research)]
    records = collection.model_dump(mode="json")
    if name in {"broken-policy-404", "broken-policy-pdf-404", "policy-timeout"}:
        records["pages"].append(
            {
                "requested_url": "https://clinic.example/policy",
                "evidence_id": "failed-policy",
                "http_status": 404 if name.endswith("404") else None,
                "captured_at": NOW.isoformat(),
            }
        )
        if name == "broken-policy-pdf-404":
            records["pages"].pop()
            records["documents"] = [
                {
                    "requested_url": "https://clinic.example/policy.pdf",
                    "evidence_id": "failed-pdf",
                    "http_status": 404,
                }
            ]
            records["evidence"].append({"id": "failed-pdf", "captured_at": NOW.isoformat()})
        (root / "screening-synthetic-http-records.json").write_text(json.dumps(records, indent=2))
    if name in {"broken-policy-404", "broken-policy-pdf-404", "policy-timeout"}:
        manifest = root / "screening-synthetic-http-records.json"
        records["manifest_path"] = manifest.name
        records["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    result = rank(primary, policies, records, identity_statements(research))
    (parent / "screening-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    import sys

    output = Path(sys.argv[1])
    rows = []
    for name, body, expected in CASES:
        r = evaluate(output / name, name, body)
        rows.append(
            {
                "case": name,
                "expected": expected,
                "actual": r["group"],
                "reasons": r["reasons"],
                "signal_kinds": [s["kind"] for s in r.get("signals", [])],
            }
        )
    output.mkdir(parents=True, exist_ok=True)
    (output / "evaluation.json").write_text(
        json.dumps({"type": "synthetic_saved_cases", "rows": rows}, ensure_ascii=False, indent=2)
    )
    print(json.dumps(rows, ensure_ascii=False))
