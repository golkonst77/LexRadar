"""Distinct statutory grounds, without turning observations into offences."""

from test_approved_proposal_084 import render


def test_fines_are_separate_and_entity_scoped():
    page = render()
    block = page.split('<div class="alert">')[1].split("<p>Жалоба пациента")[0]
    assert "для юридических лиц" in block
    assert "Ч. 2 — 300–700 тыс. ₽" in block and "письменного согласия" in block
    assert "ст. 17.13" in block and "содержанию такого согласия" in block
    assert "Ч. 2.1 — 1–1,5 млн ₽" in block and "Повторное" in block
    special = block.split("Ч. 16 — 10–15 млн ₽")[1].split("</p>")[0]
    assert "повлекшие неправомерную передачу" in special
    assert "специальных категорий" in special and "НКО" in special
    assert "уголовно" not in special and "биометр" not in special
    assert "не подтверждают указанные составы" in block
