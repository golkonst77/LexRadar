from lexradar.collector.static_renderer import StaticPage


def forms(source):
    page = StaticPage()
    page.url = "https://clinic.test/"
    page.fulfill(body=source.encode())
    return page.evaluate(None)["forms"]


def test_recover_complete_form_after_bad_wrapper():
    source = """<html><body><div><span></div>
    <form><input type="tel"><label><input type="checkbox" checked>
    Я согласен на обработку персональных данных</label></form></body></html>"""
    recovered = forms(source)
    assert len(recovered) == 1
    assert recovered[0]["fields"][0]["type"] == "tel"
    assert recovered[0]["fields"][1]["checked"] is True
    assert "согласен на обработку" in recovered[0]["fields"][1]["label"]


def test_no_forms_from_scripts_comments_templates_or_partial_fragments():
    fake = '<form><input type="tel"><input type="checkbox" checked></form>'
    for source in (
        f"<script>{fake}</script>",
        f"<!--{fake}-->",
        f"<template>{fake}</template>",
        '<div><span></div><form><input type="tel">',
        '<div><span></div><form><form><input type="tel"></form></form>',
    ):
        assert forms(source) == []


def test_existing_valid_forms_not_duplicated():
    assert len(forms('<form><input type="tel"></form>')) == 1
