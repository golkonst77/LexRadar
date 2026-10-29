"""POLICY-3 finds evidence; local confirmed judgments establish PASS or FAIL."""

import copy
import json

import pytest
from test_operator_audit_102 import case, dossier, no_network, ready, review_for  # noqa: F401

from lexradar.auditors.models import TextGrounding
from lexradar.client_proposal import render_client
from lexradar.operator_audit import finalize
from lexradar.policy3 import Policy3Review, inspect_policy3, policy3_html

POLICY = (
    "Срок хранения заявок — 30 дней. Срок хранения заявок — 90 дней. "
    "После окончания срока удаление выполняет администратор, результат фиксируется актом. "
    "Передача третьим лицам выполняется с согласия субъекта для указанной в согласии цели."
)
DEFECT = "Для одних и тех же заявок указаны разные сроки 30 и 90 дней без разграничения."


@pytest.fixture
def policy_case(case):  # noqa: F811
    _, packet, result, report = case
    packet = copy.deepcopy(packet)
    page = packet.data.pages[0]
    page.final_url = "https://clinic.example/privacy-policy"
    packet.data.evidence[0].source = page.final_url
    page.text += "\n" + POLICY
    return packet, result, report


def local_review(packet, result, report, *, status="FAIL", confirmed=True, check="retention"):
    review = review_for(packet, result, report)
    review.policy3 = [
        Policy3Review(
            check=check,
            status=status,
            confirmed=confirmed,
            explanation=DEFECT,
            quote=TextGrounding(
                evidence_id="page-0001",
                exact_quote="Срок хранения заявок — 30 дней. Срок хранения заявок — 90 дней.",
            ),
        )
    ]
    return review


def test_automatic_results_are_review_with_exact_quotes_and_sources(policy_case):
    packet, _, _ = policy_case
    rows = inspect_policy3(packet)
    assert [r["check"] for r in rows] == ["retention", "deletion", "transfers"]
    assert all(r["status"] == "REVIEW" and r["quote"] for r in rows)
    for row in rows:
        assert row["quote"] in packet.data.pages[0].text
        assert row["source"] == "https://clinic.example/privacy-policy"
        assert row["citations"] and not row["independently_confirmed"]


def test_missing_keywords_never_become_failure(policy_case):
    packet, _, _ = policy_case
    packet.data.pages[0].text = "Доступен только небольшой фрагмент документа."
    rows = inspect_policy3(packet)
    assert all(r["status"] == "REVIEW" and r["quote"] is None for r in rows)
    assert all(r["source"] == "https://clinic.example/privacy-policy" for r in rows)
    assert all("недостаток не установлен" in r["explanation"] for r in rows)
    assert all(r["question"] in policy3_html(rows) for r in rows)


def test_unreproduced_or_unavailable_policy_is_not_evidence(policy_case):
    packet, _, _ = policy_case
    packet.data.pages[0].reading.provenance = "unknown"
    assert all(r["quote"] is None and r["status"] == "REVIEW" for r in inspect_policy3(packet))


@pytest.mark.parametrize("status", ["PASS", "REVIEW", "FAIL"])
def test_explicit_local_status_has_checked_evidence_and_correct_client_copy(policy_case, status):
    packet, result, report = policy_case
    review = local_review(packet, result, report, status=status)
    if status == "PASS":
        packet.data.pages[0].text = packet.data.pages[0].text.replace(
            " Срок хранения заявок — 90 дней.", ""
        )
        review.policy3[0].quote.exact_quote = "Срок хранения заявок — 30 дней."
        review.policy3[0].explanation = "Проверено описание конкретного срока хранения заявок."
    rows = inspect_policy3(packet, review)
    assert rows[0]["status"] == status and rows[0]["source"]
    page = policy3_html(rows)
    if status == "FAIL":
        assert DEFECT in page and rows[0]["quote"] in page
        assert "https://clinic.example/privacy-policy" in page
    else:
        assert DEFECT not in page and rows[0]["question"] in page
    assert rows[1]["question"] in page and rows[2]["question"] in page


@pytest.mark.parametrize(
    "change",
    ["unconfirmed", "invented_quote", "wrong_page", "wrong_packet", "duplicate", "bad_offsets"],
)
def test_unproven_fail_is_rejected(policy_case, change):
    packet, result, report = policy_case
    review = local_review(packet, result, report)
    item = review.policy3[0]
    if change == "unconfirmed":
        item.confirmed = False
    elif change == "invented_quote":
        item.quote.exact_quote = "Нет такого текста в политике"
    elif change == "wrong_page":
        item.quote.evidence_id = "page-other"
    elif change == "wrong_packet":
        review.packet_sha256 = "0" * 64
    elif change == "duplicate":
        review.policy3.append(item.model_copy(deep=True))
    else:
        item.quote.start = 999999
    with pytest.raises(ValueError, match="POLICY-3"):
        inspect_policy3(packet, review)


def test_every_shared_renderer_proposal_has_three_policy_questions_without_packet():
    page = render_client(
        {"url": "https://clinic.example/"},
        [dict(confirmed=True, title="Наблюдение", quote="Цитата", meaning="Работа")],
        {"date": "21.10.2026"},
    )
    assert '<li class="policy3">' in page
    for row in inspect_policy3():
        assert row["question"] in page
    assert "FAIL" not in page


def test_operator_persists_policy3_and_fail_does_not_grant_sending(policy_case, tmp_path):
    packet, result, report = policy_case
    review = local_review(packet, result, report)
    path = tmp_path / "review.json"
    path.write_text(review.model_dump_json())
    out = tmp_path / "result"
    out.mkdir()
    decision = finalize(report, result, packet, out, commercial_mvp=True, review_path=path)
    stored = json.loads((out / "policy3.json").read_text())
    assert stored == decision["policy3"] and stored[0]["status"] == "FAIL"
    assert DEFECT in (out / "proposal.html").read_text()
    assert not decision["automatic_send_allowed"] and not decision["client_release_allowed"]


def test_policy_defect_and_quote_are_html_escaped(policy_case):
    packet, result, report = policy_case
    review = local_review(packet, result, report)
    review.policy3[
        0
    ].explanation = "<script>alert('x')</script> — подтверждённое противоречие сроков."
    page = policy3_html(inspect_policy3(packet, review))
    assert "<script>" not in page and "&lt;script&gt;" in page
