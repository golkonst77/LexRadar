"""A medical free-text observation is a review trigger, never a legal verdict."""

import pytest

from lexradar.collector.models import FormObservation
from lexradar.collector.reading import ReadingState
from lexradar.proposal_rules import HEALTH_FIELD_BLOCK, evaluate_rules
from test_prepare_proposal_fix import page, packet, render
from test_saved_form_recovery import forms


@pytest.mark.parametrize("markup", [
    '<textarea placeholder="Причина обращения"></textarea>',
    '<input type="text" aria-label="Жалобы">',
    '<label for="symptoms">Симптомы</label><input id="symptoms">',
    '<label>Что вас беспокоит<textarea></textarea></label>',
    '<label for="problem">Диагноз</label><input id="problem" aria-label="Комментарий">',
    '<input placeholder="Опишите проблему">',
    '<textarea aria-label="Описание заболевания"></textarea>',
    '<input placeholder="Самочувствие">',
])
def test_medical_free_text_field_renders_review_wording(markup):
    p = health_packet(markup)
    rules = evaluate_rules(p)
    assert [r['rule_id'] for r in rules] == [6]
    assert rules[0]['manual_review'] and not rules[0]['legal_violation_established']
    assert HEALTH_FIELD_BLOCK['paragraphs'][0] in render(p)


def health_packet(markup, title="Медицинский центр — стоматология"):
    extracted = forms('<form>' + markup + '</form>')[0]
    extracted.pop('links')
    f = FormObservation(page_url="https://clinic.test/", policy_links=["https://clinic.test/privacy/"], consent_links=[], **extracted)
    return packet(page(
        title=title, forms=[f],
        reading=ReadingState(provenance="reproduced", text_coverage_complete=True, text_truncated=False),
    ))


@pytest.mark.parametrize("title", ["Ремонт бытовой техники", "Магазин медицинских товаров", "Ветеринарная клиника"])
def test_nonmedical_free_text_does_not_trigger(title):
    assert evaluate_rules(health_packet('<textarea placeholder="Опишите проблему"></textarea>',
                                       title=title)) == []


@pytest.mark.parametrize("markup", [
    '<label>Причина обращения<select><option>Диагностика</option></select></label>',
    '<label>Жалобы<input type="radio"></label>',
    '<label>Симптомы<input type="checkbox"></label>',
    '<input name="phone" placeholder="Причина обращения">',
    '<input type="email" placeholder="Жалобы">',
    '<input name="name" placeholder="Самочувствие">',
    '<input type="tel" placeholder="Жалобы">',
    '<textarea placeholder="Жалобы" readonly></textarea>',
    '<textarea placeholder="Жалобы" disabled></textarea>',
])
def test_non_free_text_and_contact_fields_do_not_trigger(markup):
    assert evaluate_rules(health_packet(markup)) == []
