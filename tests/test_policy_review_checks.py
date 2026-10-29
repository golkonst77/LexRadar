"""Contextual review signals, with exact quotes and no conclusions on unread text."""

import pytest

from lexradar.policy5 import inspect_policy_review
from lexradar.proposal_rules import evaluate_rules
from lexradar.client_proposal import render_client
from test_policy5 import packet, PARTS, PADDING

CASES = {
    "vague_purposes": "Допускаются иные цели обработки ПДн, не противоречащие деятельности Общества.",
    "indefinite_retention": "Обработка персональных данных осуществляется без ограничения срока.",
    "advertising_consent": (
        "Цели обработки: предоставление Пользователю с его согласия рекламных материалов "
        "и новостной рассылки."
    ),
    "third_party_conflict": (
        "Персональные данные не передаются третьим лицам. "
        "Оператор передает персональные данные сервису рассылок для отправки сообщений."
    ),
    "health_processing": "Мы обрабатываем сведения о состоянии здоровья пациентов.",
}


def review_packet(extra, *, omit_retention=False, **kwargs):
    text = "\n".join(v for k, v in PARTS.items() if not (omit_retention and k == "retention"))
    return packet(text=text + "\n" + extra + "\n" + PADDING, **kwargs)


def signals(p, medical=True):
    return {r["check"]: r for r in inspect_policy_review(p, medical=medical)}


@pytest.mark.parametrize("key", CASES)
def test_each_contextual_review_signal_has_exact_citation(key):
    p = review_packet(CASES[key], omit_retention=key == "indefinite_retention")
    result = signals(p)[key]
    assert result["status"] == "REVIEW" and result["confirmed"]
    assert result["complete_readable_text"] and result["full_text_sha256"]
    assert result["manual_review"] and not result["legal_violation_established"]
    for q in result["quotes"]:
        assert p.data.pages[0].text[q["start"] : q["end"]] == q["quote"]
        assert len(q["quote"]) <= 450


@pytest.mark.parametrize(
    "extra",
    [
        "Цели обработки: ответить на обращение посетителя. Категории данных: имя и телефон.",
        "Хранение данных осуществляется до достижения целей обработки.",
        "Сведения о рекламе не используются для обработки данных.",
        "На сайте используются сторонние сервисы. Персональные данные не передаются третьим лицам.",
        "Специальные категории данных — сведения о состоянии здоровья и интимной жизни.",
        "Субъект вправе отказаться от получения рекламных материалов.",
    ],
)
def test_isolated_word_definition_service_or_end_of_purpose_is_not_signal(extra):
    assert not any(r["confirmed"] for r in signals(review_packet(extra)).values())


@pytest.mark.parametrize(
    "condition",
    [
        "Для рекламных рассылок запрашивается отдельное предварительное согласие.",
        "Отдельное предварительное согласие на получение рекламы оформляется самостоятельно.",
        "Согласие на рекламную рассылку предоставляется отдельно.",
    ],
)
def test_separate_advertising_consent_blocks_ad_signal(condition):
    assert not signals(review_packet(CASES["advertising_consent"] + " " + condition))[
        "advertising_consent"
    ]["confirmed"]


def test_advertising_consent_document_link_prevents_claim_of_absence():
    p = review_packet(CASES["advertising_consent"])
    from lexradar.collector.models import RelevantLinkObservation

    p.data.pages[0].relevant_links = [
        RelevantLinkObservation(
            url="https://clinic.test/ads.pdf",
            text="Согласие на получение рекламы",
            kind="consent",
            visible_in_static_html=True,
        )
    ]
    assert not signals(p)["advertising_consent"]["confirmed"]


@pytest.mark.parametrize(
    "denial",
    [
        "Персональные данные не передаются третьим лицам без согласия пользователя.",
        "Персональные данные не передаются третьим лицам, кроме подрядчиков рассылки.",
        "Персональные данные не передаются третьим лицам, за исключением законных случаев.",
    ],
)
def test_conditional_nontransfer_is_not_contradiction(denial):
    p = review_packet(denial + " Оператор передает персональные данные сервису рассылок.")
    assert not signals(p)["third_party_conflict"]["confirmed"]


def test_only_anonymous_data_to_service_is_not_personal_data_transfer():
    p = review_packet(
        "Персональные данные не передаются третьим лицам. "
        "Оператор передает обезличенные персональные данные сервису статистики."
    )
    assert not signals(p)["third_party_conflict"]["confirmed"]


def test_health_requires_medical_organization_and_concrete_collection():
    p = review_packet(CASES["health_processing"])
    assert not signals(p, medical=False)["health_processing"]["confirmed"]
    p.data.pages[
        0
    ].text += " Для оказания медицинской помощи используются данные пациента на основании 323-ФЗ."
    assert not signals(p)["health_processing"]["confirmed"]


@pytest.mark.parametrize("change", ["partial", "unavailable", "error", "unreadable", "unknown"])
def test_no_absence_inference_on_unread_document(change):
    p = review_packet(" ".join(CASES.values()), omit_retention=True)
    page = p.data.pages[0]
    if change == "partial":
        page.reading.text_coverage_complete = False
    elif change == "unavailable":
        p.data.evidence[0].available = False
    elif change == "error":
        page.http_status = 503
    elif change == "unreadable":
        page.text = "\ufffd" * 1000
    else:
        page.reading.provenance = "unknown"
    assert all(r["status"] == "UNDEFINED" and not r["confirmed"] for r in signals(p).values())


def test_purpose_based_retention_needs_open_ended_goal_context():
    p = review_packet(
        CASES["vague_purposes"] + " Хранение осуществляется до достижения целей обработки.",
        omit_retention=True,
    )
    assert signals(p)["indefinite_retention"]["confirmed"]
    p.data.pages[0].text += " Данные хранятся до отзыва согласия."
    assert not signals(p)["indefinite_retention"]["confirmed"]


def test_review_renders_neutral_proposal_and_preserves_all_signals():
    p = review_packet(CASES["vague_purposes"] + " " + CASES["advertising_consent"])
    p.data.pages[0].title = "Медицинский центр"
    result = evaluate_rules(p)
    review = next(r for r in result if r["rule_id"] == 7)
    assert len(review["evidence"]) == 2
    assert not review["legal_violation_established"]
    html = render_client(
        {"name": "Клиника", "url": "https://clinic.test/"},
        [],
        {"date": "28.10.2026"},
        policy_packet=p,
    )
    assert "ПРОВЕРКА ФОРМУЛИРОВОК ПОЛИТИКИ" in html
    assert "Рекомендуется проверить" in html
    assert "возможные нарушения установлены" not in html


def test_photo_publication_consent_and_spread_definition_do_not_cover_health_collection():
    p = review_packet(
        "Пациенты и контрагенты: имя; телефон; состояние здоровья; начало и окончание лечения. "
        "Данные, разрешенные для распространения, обрабатываются по ст. 10.1 закона. "
        "Фотографии пациентов размещены с их письменных согласий."
    )
    assert signals(p)["health_processing"]["confirmed"]


def test_explicit_written_health_consent_is_recognized():
    p = review_packet(
        CASES["health_processing"]
        + " Обработка сведений о состоянии здоровья производится на основании письменного согласия пациента."
    )
    assert not signals(p)["health_processing"]["confirmed"]


@pytest.mark.parametrize(
    "extra",
    [
        "Данные, предоставляемые Вами в онлайн-формах на нашем сайте или в рекламных материалах.",
        "Цель обработки: предоставление пользователям персонализированных сервисов для таргетирования рекламы.",
        "Мы не обрабатываем сведения о состоянии здоровья посетителей.",
    ],
)
def test_direction_and_processing_purpose_must_be_positive(extra):
    assert not any(r["confirmed"] for r in signals(review_packet(extra)).values())


def test_statement_that_separate_ad_consent_is_not_requested_is_not_an_opt_in_order():
    p = review_packet(CASES["advertising_consent"] + " Отдельное согласие на рекламу не требуется.")
    assert signals(p)["advertising_consent"]["confirmed"]


def test_complete_pdf_policy_has_the_same_review_guards_and_exact_quotes():
    p = review_packet(CASES["advertising_consent"], pdf=True)
    p.data.documents[0].http_status = 200
    result = signals(p)["advertising_consent"]
    assert result["confirmed"] and result["source"].endswith("/doc77.pdf")
    assert result["quote"] in p.data.documents[0].text
    p.data.documents[0].reading.text_truncated = True
    assert all(r["status"] == "UNDEFINED" for r in signals(p).values())
