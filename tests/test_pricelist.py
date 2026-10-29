"""Only confidently counted service-price pairs and complete scope can yield REVIEW."""

import pytest

from lexradar.client_proposal import render_client
from lexradar.pricelist import (
    assess_pricelist,
    html_positions,
    price_hint,
    review_supported,
    text_positions,
)
from lexradar.proposal_rules import NoProposalTriggers


def positions(count):
    return [
        dict(
            name=f"Консультация специалиста {i}",
            price="1000 руб.",
            quote=f"Консультация специалиста {i} 1000 руб.",
        )
        for i in range(count)
    ]


@pytest.mark.parametrize(
    "count,complete,status",
    [
        (0, True, "REVIEW"),
        (1, True, "REVIEW"),
        (29, True, "REVIEW"),
        (30, True, "PASS"),
        (30, False, "PASS"),
        (29, False, "UNKNOWN"),
        (0, False, "UNKNOWN"),
    ],
)
def test_threshold_and_complete_scope(count, complete, status):
    assert assess_pricelist(positions(count), coverage_complete=complete)["status"] == status


def test_missing_file_ambiguity_and_nonmedical_organization_never_make_review():
    p = positions(3)
    assert (
        assess_pricelist(p, coverage_complete=True, blockers=["missing xlsx"])["status"]
        == "UNKNOWN"
    )
    assert assess_pricelist(p, coverage_complete=True, count_confident=False)["status"] == "UNKNOWN"
    assert (
        assess_pricelist(positions(35), coverage_complete=True, medical=False)["status"]
        == "UNKNOWN"
    )


@pytest.mark.parametrize(
    "amount", ["от 1 000 до 2 000 руб.", "1 000–2 000 ₽", "от 1000 руб. до 2000 руб.", "1.500 руб."]
)
def test_range_is_one_service_and_thousands_are_not_split(amount):
    p = html_positions(f"<div>Приём кардиолога — {amount}</div>".encode())
    assert len(p.positions) == 1
    assert p.positions[0]["price"] == amount


def test_table_header_allows_bare_numeric_prices_but_not_other_numbers():
    p = html_positions(
        b"<table><tr><th>Service</th><th>Year</th></tr><tr><td>Cardiology</td><td>2026</td></tr></table>"
    )
    assert p.positions == []
    source = (
        "<table><tr><th>Наименование услуги</th><th>Цена, руб.</th></tr>"
        "<tr><td>Приём врача</td><td>1500</td></tr>"
        "<tr><td>Итого</td><td>1500</td></tr></table>"
    )
    p = html_positions(source.encode())
    assert [r["name"] for r in p.positions] == ["Приём врача"]
    assert p.positions[0]["quote"] == "Приём врача 1500"


def test_exact_service_duplicates_across_sources_and_price_variants_are_one():
    html = html_positions("<div>Приём врача — от 1000 ₽</div>".encode()).positions
    pdf, amb = text_positions("A01.001 Прием врача 1500 руб.")
    assert not amb
    result = assess_pricelist(html + pdf, coverage_complete=True)
    assert result["count"] == 1


def test_script_template_totals_and_casino_deposits_are_not_services():
    source = (
        "<script><div>Консультация 1000 ₽</div></script>"
        "<template><div>Массаж 2000 ₽</div></template><div>Итого 3000 ₽</div>"
        "<table><tr><td>Минимальный депозит</td><td>500 ₽</td></tr></table>"
    )
    assert html_positions(source.encode()).positions == []


def test_multiple_unpaired_prices_do_not_prove_multiple_services():
    source = "<div>Приём терапевта 1000 ₽ Массаж 2000 ₽</div>"
    result = html_positions(source.encode())
    assert not result.positions and result.ambiguous
    assert (
        assess_pricelist(result.positions, coverage_complete=True, count_confident=False)["status"]
        == "UNKNOWN"
    )


def test_pdf_line_needs_explicit_price_and_service_association():
    result, amb = text_positions(
        "Консультация невролога 1800 руб.\nТелефон +7 999 123-45-67\n№ 2026\nМассаж\n500 руб."
    )
    assert len(result) == 1 and result[0]["name"] == "Консультация невролога"
    assert amb == ["500 руб."]


def test_price_link_detection_excludes_license_and_medical_center_words():
    assert price_hint("/prices/") and price_hint("Стоимость услуг") and price_hint("/tseny")
    assert not price_hint("Лицензии") and not price_hint("Медицинский центр")


def test_review_price_proposal_uses_approved_slots_without_unrelated_cookie_claim():
    result = assess_pricelist(positions(2), coverage_complete=True)
    result["sources"] = [dict(url="https://clinic.test/prices", complete=True)]
    assert review_supported(result)
    page = render_client(
        {"name": "Клиника", "url": "https://clinic.test/"},
        [],
        {"date": "28.10.2026"},
        pricelist_review=result,
    )
    assert 'data-rule="8"' in page and 'data-rule="4"' not in page
    assert "внутренний коммерческий критерий" in page and "а не законодательный норматив" in page


def test_unknown_inventory_cannot_open_proposal_gate():
    result = assess_pricelist(positions(1), coverage_complete=False, blockers=["not saved PDF"])
    assert not review_supported(result)
    with pytest.raises(NoProposalTriggers):
        render_client(
            {"name": "Клиника", "url": "https://clinic.test/"},
            [],
            {"date": "28.10.2026"},
            pricelist_review=result,
        )


def test_marketing_heading_or_pdf_advertising_rules_are_not_a_complete_catalogue():
    from lexradar.pricelist import catalog_heading

    assert catalog_heading("Цены на услуги стоматологии")
    assert catalog_heading("Прейскурант")
    assert not catalog_heading("Почему у нас низкая стоимость услуг?")
    assert not catalog_heading("Стоимость услуг может меняться по условиям акции")
