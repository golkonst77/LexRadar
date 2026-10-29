from test_prepare_proposal_fix import page, packet, form
from test_saved_form_recovery import forms
from lexradar.proposal_rules import evaluate_rules, inspect_form_signals


def test_missing_form_is_unknown_not_violation():
    p=packet(page())
    assert inspect_form_signals(p)[0]['extraction_status']=='НЕОПРЕДЕЛЕНО'
    assert evaluate_rules(p)==[]


def test_missing_checkbox_or_information_is_manual_only():
    f=form(False);f['fields']=f['fields'][:1];f['submission_context']='Оставить заявку'
    p=packet(page(forms=[f]))
    s=inspect_form_signals(p)[0]
    assert s['status']=='НА РУЧНУЮ ПРОВЕРКУ'
    assert not s['legal_violation_established']
    assert evaluate_rules(p)==[]


def test_adjacent_consent_is_information_not_arbitrary_checkbox_label():
    f=form(False);f['fields'][1]['label']=None
    p=packet(page(forms=[f]))
    s=inspect_form_signals(p)
    assert s[0]['status']=='НЕ ОБНАРУЖЕНО'
    assert s[1]['consent_kind']=='unresolved'
    assert s[1]['status']=='НА РУЧНУЮ ПРОВЕРКУ'
    assert evaluate_rules(p)==[]


def test_preset_is_fact_with_manual_legal_qualification():
    p=packet(page(forms=[form()]))
    s=next(s for s in inspect_form_signals(p) if s['signal']=='preset_consent')
    assert s['status']=='ПОДТВЕРЖДЕНО' and s['manual_review']
    assert s['consent_kind']=='personal_data' and not s['legal_violation_established']
    assert evaluate_rules(p)[0]['manual_review']
    assert not evaluate_rules(p)[0]['legal_violation_established']


def test_consent_types_are_separate_and_do_not_add_pd_trigger():
    for label,kind in [('Согласен на рекламную рассылку','advertising'),('Согласен на cookie','cookie'),('Согласен с политикой конфиденциальности','policy_acknowledgment')]:
        p=packet(page(forms=[form(True,label)]))
        s=next(s for s in inspect_form_signals(p) if s['signal']=='preset_consent')
        assert s['consent_kind']==kind
        assert evaluate_rules(p)==[]


def test_cookie_continued_use_and_refusal_are_separate():
    p=packet(page(text='Продолжая пользоваться сайтом, вы соглашаетесь с cookie.',cookie_text_observed=True))
    s=inspect_form_signals(p)
    assert any(x['signal']=='cookie_continued_use' and x['status']=='ПОДТВЕРЖДЕНО' for x in s)
    assert any(x['signal']=='cookie_refusal_not_observed' and x['status']=='НА РУЧНУЮ ПРОВЕРКУ' for x in s)
    assert evaluate_rules(p)==[]


def test_checked_variants_popup_and_script_are_static_only():
    for checked in ['checked','checked="checked"']:
        result=forms(f'<div class="popup"><form><input type="tel"><label><input type="checkbox" {checked}>Я согласен на обработку персональных данных</label><button>Записаться</button></form></div>')
        assert result[0]['fields'][1]['checked'] is True
    result=forms('<form><input type="tel"><input type="checkbox"><script>document.querySelector("input").checked=true</script></form>')
    assert result[0]['fields'][1]['checked'] is False
