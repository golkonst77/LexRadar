"""Focused client copy, source-bound recipient and preserved approved template."""

import copy

import pytest
from test_approved_proposal_084 import mask_fields
from test_operator_audit_102 import case, dossier, no_network, ready  # noqa: F401

from lexradar import client_proposal as cp
from lexradar.auditors.models import TextGrounding
from lexradar.operator_audit import clinic_name, finalize


@pytest.fixture
def proposal_case(case):  # noqa: F811
    _, packet, result, report = case
    packet = copy.deepcopy(packet)
    cookie = (
        "Мы используем сервис Яндекс.Метрика. Если Вы продолжите использовать сайт, "
        "мы будем считать что Вас это устраивает."
    )
    medical = "Политика упоминает данные о здоровье и биометрические персональные данные."
    packet.data.pages[0].text = (
        "Полное наименование фирмы ООО «Клиника Тест»\n" + cookie + "\n" + medical
    )
    rows = []
    for fid, topic, quote in [
        ("F005", "data_collection_forms", medical),
        ("F007", "mandatory_documents", medical),
        ("F004", "special_categories", medical),
        ("F003", "cookies_analytics", cookie),
    ]:
        f = result.findings[0].model_copy(deep=True)
        f.id, f.topic = fid, topic
        f.status = "potential"
        f.fact = quote if fid == "F003" else "Составной вывод модели, не являющийся цитатой"
        f.text_grounding = [TextGrounding(evidence_id="page-0001", exact_quote=quote)]
        f.proposed_work = ["legal_documents", "webmaster_instructions"]
        rows.append(f)
    result.findings = rows
    return packet, result, report


def test_cookie_card_medical_question_and_rest_of_template_unchanged(proposal_case, tmp_path):
    packet, result, report = proposal_case
    before = cp.APPROVED_TEMPLATE.read_bytes()
    decision = finalize(report, result, packet, tmp_path, commercial_mvp=True, replayed=True)
    assert decision["proposal_findings"] == ["F003", "F004"]
    page = (tmp_path / "proposal.html").read_text()
    section = page.split('<ol class="findings editable" contenteditable="true">')[1].split("</ol>")[
        0
    ]
    assert section.count("<li>") == 2
    assert "Cookie и Яндекс.Метрика" in section
    assert "до выбора и после отказа" in section
    assert "ТЗ вебмастеру" in section and "уточнения политики" in section
    assert "Соответствуют ли" in section and "биометрия" in section
    assert "политика упоминает" not in section.casefold()  # No long quote in the brief question.
    for marker in [
        "F003",
        "F004",
        "F005",
        "F007",
        "CONFIRMED",
        "PARTIAL",
        "Нарушение не установлено",
        "Возможное расхождение",
        "текстовое наблюдение",
    ]:
        assert marker not in section
    assert "Руководителю ООО «Клиника Тест»" in page
    assert "LexRadar · КП v2.4" in page and "LexRadar · КП v2.2" not in page
    expected = before.decode().replace("LexRadar · КП v2.2", "LexRadar · КП v2.4", 1)
    assert mask_fields(page) == mask_fields(expected)
    assert cp.APPROVED_TEMPLATE.read_bytes() == before
    assert not decision["client_release_allowed"] and not decision["automatic_send_allowed"]


@pytest.mark.parametrize("change", ["not_found", "rejected"])
def test_no_medical_question_without_valid_observation(proposal_case, tmp_path, change):
    packet, result, report = proposal_case
    medical = next(f for f in result.findings if f.id == "F004")
    if change == "not_found":
        medical.text_grounding[0].exact_quote = "Invented quotation"
    else:
        medical.status = "rejected"
    decision = finalize(report, result, packet, tmp_path, commercial_mvp=True)
    assert decision["proposal_findings"] == ["F003"]
    assert "Дополнительный вопрос" not in (tmp_path / "proposal.html").read_text()


@pytest.mark.parametrize("change", ["no_label", "unreproduced", "ambiguous", "wrong_requested"])
def test_recipient_never_uses_unconfirmed_company(proposal_case, change):
    packet, _, _ = proposal_case
    requested = ""
    if change == "no_label":
        packet.data.pages[0].text = "Сервис третьей стороны ООО «Клиника Тест»"
    elif change == "unreproduced":
        packet.data.pages[0].reading.provenance = "unknown"
    elif change == "ambiguous":
        packet.data.pages[0].text += "\nПолное наименование фирмы ООО «Другая клиника»"
    else:
        requested = "Придуманное название"
    assert clinic_name(packet, requested) == ""


def test_confirmed_full_name_and_neutral_recipient(proposal_case):
    packet, _, _ = proposal_case
    packet.data.pages[
        0
    ].text = "Полное наименование фирмы Общество с ограниченной ответственностью «Клиника Тест»"
    assert clinic_name(packet) == "Общество с ограниченной ответственностью «Клиника Тест»"
    page = cp.render_client(
        {"name": "", "url": "https://clinic.example/"},
        [dict(confirmed=True, title="Проверка", quote="Точный текст", meaning="Работа")],
        {"date": "21.10.2026"},
    )
    recipient = cp.PERSONALIZATION.findall(page)[1][1]
    assert recipient == "Руководителю клиники"


@pytest.mark.parametrize("conflict", ["", "name", "ogrn", "unreproduced"])
def test_official_medical_identity_with_ogrn(proposal_case, conflict):
    packet, _, _ = proposal_case
    identity = (
        'Общая информация о медицинской организации '
        'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ " ОН КЛИНИК Бейби " '
        'ОГРН 1107746169981'
    )
    packet.data.pages[0].text = identity + ' ООО «Он Клиник на Зубовском» ОГРН 1157746251904'
    if conflict == "name":
        packet.data.pages[0].text += '\n' + identity.replace("ОН КЛИНИК Бейби", "Другая клиника")
    elif conflict == "ogrn":
        packet.data.pages[0].text += '\n' + identity.replace("1107746169981", "1157746251904")
    elif conflict == "unreproduced":
        packet.data.pages[0].reading.provenance = "unknown"
    assert clinic_name(packet) == ("" if conflict else "ООО «ОН КЛИНИК Бейби»")


@pytest.mark.parametrize("separator", ["\nРеквизиты: ", " : ", "\n\n"])
def test_flexible_medical_identity(proposal_case, separator):
    packet, _, _ = proposal_case
    packet.data.pages[0].text = (
        'Общая информация о медицинской организации' + separator
        + 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ\nОТВЕТСТВЕННОСТЬЮ: “ ОН КЛИНИК\nБейби ”'
        + '\nРегистрационные сведения: ОГРН: 1107746169981'
        + '\nООО «Другая клиника» ОГРН 1157746251904'
    )
    assert clinic_name(packet) == 'ООО «ОН КЛИНИК Бейби»'
