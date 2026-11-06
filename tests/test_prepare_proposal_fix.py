"""Fixed-rule gating and HTML/PDF contracts, with no website collection or AI."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PDFError

from lexradar.client_proposal import APPROVED_TEMPLATE, proposal_text, render_client
from lexradar.collector.models import PageObservation
from lexradar.mvp_batch import run, write_prepared_proposal
from lexradar.proposal_rules import BLOCKS, NoProposalTriggers, evaluate_rules


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Network attempted")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)


def page(**values):
    return PageObservation(
        **{
            "requested_url": "https://clinic.test/",
            "captured_at": "2026-10-27T10:00:00Z",
            "evidence_id": "page-1",
            "http_status": 200,
            "text": "Главная Услуги Лечение зубов Телефон Метро",
            **values,
        }
    )


def packet(*pages):
    evidence = [
        SimpleNamespace(
            id=p.evidence_id,
            available=True,
            artifacts=[SimpleNamespace(kind="html", path=f"{p.evidence_id}.html", sha256="a" * 64)],
        )
        for p in pages
    ]
    return SimpleNamespace(
        data=SimpleNamespace(pages=list(pages), evidence=evidence),
        sha256="b" * 64,
        manifest_sha256="c" * 64,
        payload="{}",
        limitations=(),
    )


def form(checked=True, label="Я согласен на обработку персональных данных"):
    return dict(
        page_url="https://clinic.test/",
        fields=[
            dict(tag="input", type="tel", name="phone"),
            dict(tag="input", type="checkbox", name="agreement", label=label, checked=checked),
        ],
        policy_links=[],
        consent_links=[],
        submission_context=label,
    )


def render(p, findings=()):
    return render_client(
        {"name": "Клиника", "url": "https://clinic.test/"},
        findings,
        {"date": "27.10.2026"},
        policy_packet=p,
    )


def test_prepare_recipient_uses_confirmed_identity_not_csv(tmp_path, monkeypatch):
    from lexradar.collector.reading import ReadingState

    monkeypatch.setattr("lexradar.pdf_export.write_html_pdf", lambda *args: None)
    row = {"url": "https://clinic.test/", "input": {"company_name": "Бренд из CSV"}}
    for text, verified in [
        ('Полное наименование фирмы ООО «Клиника А»', True),
        ('Полное наименование фирмы ООО «Клиника А»\n'
         'Полное наименование фирмы ООО «Другая»', False),
        ('Бренд из CSV', False),
    ]:
        p = packet(page(text=text, forms=[form()], reading=ReadingState(provenance="reproduced")))
        p.data.documents = []
        p.data.evidence[0].source = row["url"]
        assert write_prepared_proposal(row, p, tmp_path)
        html = (tmp_path / "proposal.html").read_text()
        expected = 'Руководителю ООО «Клиника А»' if verified else 'Руководителю клиники'
        assert expected in html and 'Бренд из CSV' not in html
        review = json.loads((tmp_path / "review.json").read_text())
        assert review["identity_verification_required"] is not verified
        assert review["clinic_name"] == ('ООО «Клиника А»' if verified else '')


def test_positive_features_and_incomplete_coverage_do_not_trigger():
    p = packet(
        page(
            forms=[form(False)],
            cookie_text_observed=True,
            static_analytics_hints=["yandex_metrika"],
            relevant_links=[
                dict(
                    url="https://clinic.test/privacy",
                    text="Политика конфиденциальности",
                    kind="privacy_policy",
                    visible_in_static_html=True,
                )
            ],
        )
    )
    assert evaluate_rules(p) == []
    with pytest.raises(NoProposalTriggers):
        render(p)
    with pytest.raises(NoProposalTriggers):
        render(None, [dict(rule_id=4, confirmed=True, quote="invented")])


def test_preset_consent_has_specific_proof_and_deduplicates():
    p = packet(page(forms=[form(), form()]))
    rules = evaluate_rules(p)
    assert [r["rule_id"] for r in rules] == [3]
    proof = rules[0]["evidence"][0]
    assert proof["manifest_pointer"] == "forms/0/fields/1"
    assert proof["quote"] == "Я согласен на обработку персональных данных"
    assert proof["field"]["checked"] is True and proof["url"] == "https://clinic.test/"
    assert len(rules[0]["evidence"]) == 1


def test_unrelated_or_policy_acknowledgment_checkbox_is_not_consent():
    for label in ("Подписаться на новости", "Согласен с политикой конфиденциальности"):
        assert evaluate_rules(packet(page(forms=[form(True, label)]))) == []
    assert evaluate_rules(packet(page(forms=[form(False)]))) == []


def test_saved_failed_policy_and_form_link_are_concrete_triggers():
    f = form(False)
    f["policy_links"] = ["https://clinic.test/privacy"]
    p = packet(
        page(
            forms=[f],
            relevant_links=[
                dict(
                    url="https://clinic.test/privacy",
                    text="Политика конфиденциальности",
                    kind="privacy_policy",
                    visible_in_static_html=True,
                )
            ],
        ),
        page(requested_url="https://clinic.test/privacy", evidence_id="page-2", http_status=404),
    )
    assert [r["rule_id"] for r in evaluate_rules(p)] == [2]
    assert evaluate_rules(p)[0]["evidence"][0]["response"]["quote"] == "404"


def test_js_link_without_saved_failure_is_not_proven_broken():
    f = form(False)
    f["policy_links"] = ["javascript:;"]
    p = packet(
        page(
            forms=[f],
            relevant_links=[
                dict(
                    url="javascript:;",
                    text="Политика конфиденциальности",
                    kind="privacy_policy",
                    visible_in_static_html=True,
                )
            ],
        )
    )
    assert evaluate_rules(p) == []


def test_only_explicit_same_operator_contradiction_triggers():
    links = [
        dict(
            url=f"https://clinic.test/{path}",
            text=title,
            kind="privacy_policy",
            visible_in_static_html=True,
        )
        for path, title in (
            ("privacy", "Политика конфиденциальности"),
            ("consent", "Согласие на обработку персональных данных"),
        )
    ]

    def data(ogrn):
        return packet(
            page(relevant_links=links),
            page(
                requested_url=links[0]["url"],
                evidence_id="page-2",
                text="Оператор персональных данных: ООО «Клиника» "
                "ИНН: 1234567890 ОГРН: 1234567890123",
            ),
            page(
                requested_url=links[1]["url"],
                evidence_id="page-3",
                text=f"Оператор персональных данных: ООО «Клиника» ИНН: 1234567891 ОГРН: {ogrn}",
            ),
        )

    assert [r["rule_id"] for r in evaluate_rules(data("1234567890123"))] == [5]
    assert evaluate_rules(data("1234567890124")) == []


def test_fixed_blocks_two_paragraphs_and_commercial_design_preserved():
    import re

    p = packet(page(forms=[form()]))
    result = render(p)
    blocks = re.findall(r'<div class="motivation-block" data-rule="(\d)">(.*?)</div>', result, re.S)
    assert [int(n) for n, _ in blocks] == [3, 4]
    for number, block in blocks:
        assert block.count('<p class="MsoNormal"') == 2
        for paragraph in BLOCKS[int(number)]["paragraphs"]:
            assert paragraph in proposal_text(block)
        assert "Мы предлагаем" not in block and "Телефон Метро" not in block
    original = APPROVED_TEMPLATE.read_bytes().decode("utf-16")
    original = re.sub(
        r"<p><strong><span[^>]*>Ориентировочное время: 2–5 минут\.</span></strong></p>",
        "", original,
    )
    assert result.split("Замечания", 1)[1] == original.split("Замечания", 1)[1]
    assert (
        result[result.index("<style") : result.index("</style>")]
        == original[original.index("<style") : original.index("</style>")]
    )
    assert "Нажмите на текст" not in result and "Печать / сохранить" not in result


def test_no_trigger_writes_review_only(tmp_path):
    row = {"url": "https://clinic.test/", "input": {"company_name": "Клиника"}}
    assert write_prepared_proposal(row, packet(page()), tmp_path) == []
    assert not (tmp_path / "proposal.html").exists()
    assert not (tmp_path / "proposal.pdf").exists()
    review = json.loads((tmp_path / "review.json").read_text())
    assert review["reason"] == "no_confirmed_triggers_1_2_3_5"
    assert not review["block4_without_evidence"] and not review["automatic_send_allowed"]


def test_pdf_failure_keeps_html_without_text_fallback(tmp_path, monkeypatch):
    def failed(*args):
        raise PDFError("Browser unavailable")

    monkeypatch.setattr("lexradar.pdf_export.write_html_pdf", failed)
    row = {"url": "https://clinic.test/", "input": {"company_name": "Клиника"}}
    assert write_prepared_proposal(row, packet(page(forms=[form()])), tmp_path)
    assert (tmp_path / "proposal.html").exists() and not (tmp_path / "proposal.pdf").exists()
    review = json.loads((tmp_path / "review.json").read_text())
    assert not review["pdf_ready"] and review["block4_without_evidence"]
    assert review["manual_approval"] is None and not review["sent"]


def test_prepare_branch_skips_no_trigger_without_collecting_sites(tmp_path, monkeypatch):
    p = packet(page())

    def fixture_collect(url, output, *args, **kwargs):
        output.mkdir()
        return p.data

    monkeypatch.setattr("lexradar.collector.collect", fixture_collect)
    monkeypatch.setattr("lexradar.mvp_batch.load_packet", lambda *args: p)
    source = tmp_path / "input.csv"
    source.write_text("url,company_name,email\nhttps://clinic.test/,Clinic,\n")
    state = run(
        source,
        tmp_path / "batch",
        mode="prepare",
        allow_site_collection=True,
        max_sites=1,
        max_ai_calls=0,
    )
    row = state["rows"][0]
    assert row["status"] == "SKIPPED" and row["qualification"] == "HOLD"
    assert row["reason"] == "no_confirmed_triggers_1_2_3_5"
    assert "proposal" not in row


def test_approved_text_matrix_is_pinned():
    # Exact UTF-8 digest of the five titles and their ten approved paragraphs.
    text = "\n".join(
        line for block in BLOCKS.values() for line in [block["title"], *block["paragraphs"]]
    )
    assert (
        hashlib.sha256(text.encode()).hexdigest()
        == "c825ceaef7552486ec815bd8cb74f74bc916a4b2997f358c8503181a550cfc24"
    )


def test_saved_policy_acknowledgment_is_not_processing_consent():
    label = "Нажимая кнопку, вы соглашаетесь с Политикой обработки персональных данных"
    assert evaluate_rules(packet(page(forms=[form(True, label)]))) == []


def test_saved_checked_city_is_not_processing_consent():
    f = form(True, "Тула")
    f["fields"][1].update(type="radio", name="city")
    assert evaluate_rules(packet(page(forms=[f]))) == []


@pytest.mark.parametrize("obstacle", [None, "network", "partial", "document", "link", "no_form"])
def test_form_without_policy_requires_readable_checked_materials(obstacle):
    from lexradar.collector.models import DocumentObservation
    from lexradar.collector.reading import ReadingState

    complete = ReadingState(
        provenance="reproduced", text_coverage_complete=True, text_truncated=False
    )
    home = page(forms=[] if obstacle == "no_form" else [form(False)], reading=complete)
    p = packet(home)
    if obstacle == "network":
        home.http_status = None
        p.data.evidence[0].available = False
    elif obstacle == "partial":
        home.reading.text_coverage_complete = False
    elif obstacle == "link":
        home.forms[0].policy_links = ["https://clinic.test/legal.pdf"]
    elif obstacle == "document":
        p.data.documents = [DocumentObservation(
            requested_url="https://clinic.test/legal.pdf", evidence_id="doc", http_status=200
        )]
        p.data.evidence.append(SimpleNamespace(id="doc", available=True, artifacts=[]))
    rules = evaluate_rules(p)
    assert [r["rule_id"] for r in rules] == ([1] if obstacle is None else [])
    if obstacle is None:
        assert rules[0]["evidence"][0]["policy_check"] == "not_found_on_checked_pages"
        assert "НЕДОСТАТКИ ПОЛИТИКИ" in render(p)


@pytest.mark.parametrize("state", ["unlinked", "linked", "technical_link", "partial", "network", "stopped"])
def test_existing_policy_access_requires_complete_main_pages_and_visitor_links(state):
    from lexradar.collector.reading import ReadingState
    from lexradar.collector.models import RelevantLinkObservation

    home = page(forms=[form(False)], reading=ReadingState(
        provenance="reproduced", text_coverage_complete=True, text_truncated=False
    ))
    # The policy body need not be fully extracted to establish that a document exists.
    policy = page(
        requested_url="https://clinic.test/privacy/", evidence_id="policy",
        text="Политика обработки персональных данных", http_status=200,
    )
    p = packet(home, policy)
    if state in {"linked", "technical_link"}:
        home.relevant_links = [RelevantLinkObservation(
            url="https://clinic.test/privacy/", text="согласие", kind="consent",
            visible_in_static_html=state == "linked",
        )]
    elif state == "partial":
        home.reading.text_coverage_complete = False
    elif state == "network":
        home.http_status = None
        p.data.evidence[0].available = False
    elif state == "stopped":
        p.data.warnings = ["Express screening stopped early"]
    rules = evaluate_rules(p)
    assert [r["rule_id"] for r in rules] == (
        [1] if state in {"unlinked", "technical_link"} else []
    )
    if rules:
        proof = rules[0]["evidence"][0]
        assert proof["policy_check"] == "policy_access_link_not_found"
        assert proof["policy_document_exists"] and not proof["visitor_policy_link_found"]


@pytest.mark.parametrize("target", ["policy", "consent", "unavailable", "unknown", "acknowledgment"])
def test_consent_document_reference_must_not_target_explicit_policy(target):
    from lexradar.collector.reading import ReadingState
    from lexradar.collector.models import RelevantLinkObservation

    url = "https://clinic.test/privacy/"
    f = form(False)
    f["submission_context"] = (
        "Я даю свое согласие на условиях и для целей, определенных "
        "в Согласии на обработку персональных данных"
    )
    if target == "acknowledgment":
        f["submission_context"] = "Я согласен с политикой конфиденциальности"
    f["consent_links"] = [url]
    home = page(forms=[f], reading=ReadingState(provenance="reproduced"))
    home.relevant_links = [RelevantLinkObservation(
        url=url, text="согласие", kind="privacy_policy", visible_in_static_html=True,
    )]
    policy = page(
        requested_url=url, evidence_id="document",
        title="Согласие на обработку персональных данных" if target == "consent"
        else "Политика конфиденциальности",
        text="Согласие на обработку персональных данных" if target == "consent"
        else "Настоящая Политика конфиденциальности персональных данных действует на сайте.",
        # Identity is a positive fact; no claim is made about the unread tail.
        reading=ReadingState(provenance="unknown" if target == "unknown" else "reproduced"),
    )
    p = packet(home, policy)
    if target == "unavailable":
        policy.http_status = None
        p.data.evidence[1].available = False
    rules = evaluate_rules(p)
    assert [r["rule_id"] for r in rules] == ([2] if target == "policy" else [])
    if rules:
        assert rules[0]["evidence"][0]["form_check"] == "consent_link_targets_policy"
        assert not rules[0]["legal_violation_established"]
        assert "НЕДОСТАТКИ ФОРМ" in render(p)


@pytest.mark.parametrize("state", ["preset", "unchecked", "policy", "unavailable", "unconfirmed_text"])
def test_preset_policy_label_linking_actual_pd_consent_triggers(state):
    from lexradar.collector.reading import ReadingState

    url = "https://clinic.test/soglasie.html"
    f = form(state != "unchecked", "Нажимая кнопку, вы соглашаетесь с Политикой обработки персональных данных")
    f["policy_links"] = [url]
    home = page(forms=[f])
    consent = page(
        requested_url=url, evidence_id="consent",
        title="Политика обработки персональных данных" if state == "policy"
        else "Согласие на обработку персональных данных",
        text="Документ не прочитан" if state == "unconfirmed_text" else
        "Настоящим выражаю свое безусловное согласие на обработку моих персональных данных.",
        reading=ReadingState(provenance="reproduced"),
    )
    p = packet(home, consent)
    if state == "unavailable":
        p.data.evidence[1].available = False
    rules = evaluate_rules(p)
    assert [r["rule_id"] for r in rules] == ([3] if state == "preset" else [])
    if rules:
        proof = rules[0]["evidence"][0]
        assert proof["field"]["checked"] is True
        assert proof["document"]["url"] == url
        assert "выражаю свое безусловное согласие" in proof["document"]["quote"]
        assert not rules[0]["legal_violation_established"]
        assert "НАРУШЕНИЯ ПОРЯДКА ПОЛУЧЕНИЯ СОГЛАСИЯ" in render(p)


@pytest.mark.parametrize('text,complete,expected', [
    ('ООО «Клиника А»', True, 'Руководителю ООО «Клиника А»'),
    ('ООО «Клиника А» ООО «Другая»', True, 'Руководителю клиники'),
    ('ООО «Клиника А»', False, 'Руководителю клиники'),
    ('Без названия', True, 'Руководителю клиники'),
])
def test_placeholder_recipient_uses_only_unique_reproduced_name(text, complete, expected):
    from lexradar.client_proposal import proposal_recipient
    from lexradar.collector.reading import ReadingState

    p = packet(page(text=text, reading=ReadingState(
        provenance='reproduced', text_coverage_complete=complete,
    )))
    assert proposal_recipient({'name': 'Сайт clinic.test (связь не подтверждена)'}, p) == expected


def test_generated_proposal_drops_accidental_time_paragraph():
    html = render(packet(page(forms=[form()])))
    assert 'Ориентировочное время' not in html
    assert 'data-rule="4"' in html
