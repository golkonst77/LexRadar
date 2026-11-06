from types import SimpleNamespace

import pytest

from lexradar.collector.models import DocumentObservation, PageObservation
from lexradar.collector.reading import ReadingState
from lexradar.collector.static_renderer import StaticPage
from lexradar.policy5 import inspect_policy5, policy_materials
from lexradar.proposal_rules import evaluate_rules

PARTS = {
    "operator": "Оператор: ООО «Клиника», ИНН 1234567890, ОГРН 1234567890123.",
    "purposes": "Сведения используются, чтобы оформить запись на приём и ответить посетителю.",
    "data": "Обрабатываем контактные сведения: фамилия, адрес электронной почты, номер телефона.",
    "retention": "Сохраняем сведения до завершения ответа на обращение.",
    "deletion": "Когда ответ отправлен, записи стираются из системы.",
}
PADDING = (
    "Сотрудники обеспечивают защиту информации от неправомерного доступа. "
    "Посетитель вправе обратиться к ответственному лицу по вопросам конфиденциальности. "
) * 5


def packet(*, missing=None, complete=True, available=True, pdf=False, text=None, url=None):
    text = (
        text
        if text is not None
        else "\n".join(v for k, v in PARTS.items() if k != missing) + "\n" + PADDING
    )
    url = url or (
        "https://clinic.test/userfiles/doc77.pdf" if pdf else "https://clinic.test/privacy/"
    )
    reading = ReadingState(
        provenance="reproduced", text_coverage_complete=complete, text_truncated=not complete
    )
    args = dict(requested_url=url, evidence_id="policy", text=text, reading=reading)
    if pdf:
        material = DocumentObservation(**args, extraction_status="text", page_texts={1: text})
        home = PageObservation(
            requested_url="https://clinic.test/",
            captured_at="2026-10-27T10:00:00Z",
            evidence_id="home",
            text="Главная",
            relevant_links=[
                dict(
                    url=url,
                    text="Политика конфиденциальности",
                    kind="privacy_policy",
                    visible_in_static_html=True,
                )
            ],
        )
        pages, docs = [home], [material]
    else:
        material = PageObservation(**args, captured_at="2026-10-27T10:00:00Z", http_status=200)
        pages, docs = [material], []
    evidence = [
        SimpleNamespace(id=m.evidence_id, available=available, artifacts=[]) for m in pages + docs
    ]
    return SimpleNamespace(
        data=SimpleNamespace(
            pages=pages,
            documents=docs,
            entities=[],
            evidence=evidence,
            target_url="https://clinic.test/",
        )
    )


def test_equivalent_wording_without_five_literal_headings_passes():
    rows = inspect_policy5(packet())
    assert len(rows) == 5 and all(r["status"] == "PASS" for r in rows)
    assert evaluate_rules(packet()) == []


def test_unread_pdf_policy_records_source_without_content_accusation():
    from lexradar.policy5 import unread_pdf_policies

    p = packet(pdf=True, text="", complete=False)
    row = unread_pdf_policies(p)[0]
    assert row["source"] == str(p.data.documents[0].requested_url)
    assert row["message"] == "Политика в PDF — содержание не проверено"
    assert evaluate_rules(p) == []
    # An unfetched PDF link must also be retained.
    p.data.documents = []
    assert unread_pdf_policies(p) == [row]
    # Extensionless URLs are PDFs only after the response identified them as such.
    url = "https://clinic.test/download?id=77"
    p.data.pages[0].relevant_links[0].url = url
    assert unread_pdf_policies(p) == []
    p.data.pages.append(PageObservation(
        requested_url=url, captured_at="2026-10-27T10:00:00Z", evidence_id="download",
        categories=["application/pdf"],
    ))
    p.data.evidence.append(SimpleNamespace(id="download", available=False, artifacts=[]))
    assert unread_pdf_policies(p)[0]["source"] == url
    assert evaluate_rules(p) == []


def test_readable_pdf_keeps_full_checks_without_manual_pdf_marker():
    from lexradar.policy5 import unread_pdf_policies

    p = packet(pdf=True, missing="retention")
    assert unread_pdf_policies(p) == []
    findings = evaluate_rules(p)
    assert findings and findings[0]["rule_id"] == 1
    url = "https://clinic.test/download?id=77"
    p.data.documents[0].requested_url = url
    p.data.pages[0].relevant_links[0].url = url
    assert unread_pdf_policies(p) == []
    p.data.pages[0].relevant_links.append(p.data.pages[0].relevant_links[0].model_copy(
        update={"url": "https://clinic.test/another.pdf"}
    ))
    assert unread_pdf_policies(p)[0]["source"].endswith("/another.pdf")
    assert evaluate_rules(p)[0]["rule_id"] == 1


@pytest.mark.parametrize("missing", list(PARTS))
def test_each_complete_semantic_omission_triggers_rule_one(missing):
    p = packet(missing=missing)
    rows = inspect_policy5(p)
    failed = [r for r in rows if r["confirmed"]]
    assert [r["check"] for r in failed] == [missing]
    assert failed[0]["quote"] in p.data.pages[0].text
    assert failed[0]["complete_readable_text"] and failed[0]["full_text_sha256"]
    assert [r["rule_id"] for r in evaluate_rules(p)] == [1]


@pytest.mark.parametrize("change", ["partial", "unavailable", "unreadable", "foreign"])
def test_incomplete_or_unreadable_never_means_deficiency(change):
    opts = (
        {"complete": False}
        if change == "partial"
        else {"available": False}
        if change == "unavailable"
        else {
            "text": "\ufffd" * 1000
            if change == "unreadable"
            else "This policy describes personal information. " * 50
        }
    )
    p = packet(missing="retention", **opts)
    assert all(r["status"] == "UNDEFINED" and not r["confirmed"] for r in inspect_policy5(p))
    assert evaluate_rules(p) == []


def test_pdf_policy_found_by_link_despite_nonstandard_filename():
    p = packet(pdf=True, missing="retention")
    rows = inspect_policy5(p)
    assert len(policy_materials(p)) == 1
    assert next(r for r in rows if r["check"] == "retention")["status"] == "FAIL"
    assert all(r["source"].endswith("/doc77.pdf") for r in rows)


def test_third_party_policy_not_attributed_to_clinic():
    assert policy_materials(packet(pdf=True, url="https://service.test/privacy.pdf")) == []


def test_security_mention_of_destruction_is_not_a_deletion_procedure():
    text = "\n".join(v for k, v in PARTS.items() if k != "deletion") + "\n" + PADDING
    text += "Меры защиты направлены на предотвращение неправомерного уничтожения информации."
    assert (
        next(r for r in inspect_policy5(packet(text=text)) if r["check"] == "deletion")["status"]
        == "FAIL"
    )


def test_unavailable_policy_http_status_not_rule_one():
    p = packet(missing="operator")
    p.data.pages[0].http_status = 404
    assert all(r["status"] == "UNDEFINED" for r in inspect_policy5(p))
    assert evaluate_rules(p) == []


def test_links_after_malformed_wrapper_recovered_without_scripts_or_templates():
    page = StaticPage()
    page.url = "https://clinic.test/"
    page.fulfill(
        body=b'<div><span></div><a href="/legal77">'
        + "Политика конфиденциальности".encode()
        + b"</a>"
        + b'<script><a href="/bad">'
        + "Политика конфиденциальности".encode()
        + b"</a></script>"
    )
    links = page.evaluate(None)["links"]
    assert any(link["url"] == "https://clinic.test/legal77" for link in links)
    assert not any("/bad" in link["url"] for link in links)


def test_operator_conflicts_with_literal_same_ogrn_site_declaration():
    p = packet()
    text = "ООО «Клиника» ИНН 1234567891 ОГРН 1234567890123"
    home = PageObservation(
        requested_url="https://clinic.test/",
        captured_at="2026-10-27T10:00:00Z",
        evidence_id="home",
        text=text,
    )
    p.data.pages.append(home)
    p.data.evidence.append(SimpleNamespace(id="home", available=True, artifacts=[]))
    p.data.entities = [
        SimpleNamespace(
            source="https://clinic.test/", ogrn="1234567890123", inn="1234567891", raw_text=text
        )
    ]
    op = inspect_policy5(p)[0]
    assert op["status"] == "FAIL" and op["compared_with"]["quote"] == text


def test_explicit_body_recovers_links_when_head_end_tag_omitted():
    page = StaticPage()
    page.url = "https://clinic.test/"
    page.fulfill(
        body=(
            '<head><title>Клиника</title><body><a href="/legal">Политика конфиденциальности</a>'
        ).encode()
    )
    assert any(link["url"] == "https://clinic.test/legal" for link in page.evaluate(None)["links"])
