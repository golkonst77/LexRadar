"""Synthetic regressions for Sprint 1; no scz.ru acceptance claims."""

import itertools
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_primary import make_dossier, run
from test_reading_integrity import pdf_dossier

from lexradar.auditors.evidence import DossierError, load_packet
from lexradar.auditors.models import AnalysisConfig
from lexradar.collector.document_priority import document_hints, document_sort_key
from lexradar.collector.pdf import extract_inventory
from lexradar.primary.text_budget import allocate_blocks
from lexradar.quality.fixtures import pdf_bytes


def test_disclosures_survive_order_and_large_navigation():
    blocks = [
        ("Навигация " * 2000, True, 0),
        ("Политика обработки персональных данных синтетического оператора", False, 1),
        ("Правила оказания платных медицинских услуг", False, 2),
        ("Лицензия: синтетические сведения для проверки", False, 3),
    ]
    for ordered in itertools.permutations(blocks):
        selected = allocate_blocks(list(ordered), 200)
        assert sum(len(b[0]) for b in selected) <= 200
        assert all(any(text == b[0] for b in selected) for text, _, _ in blocks[1:])
        assert selected == allocate_blocks(blocks, 200)


def test_fair_quotas_and_marker_window():
    blocks = [("Политика " + "a" * 1000, False, 0), ("b" * 1000 + "Лицензия", False, 1)]
    selected = allocate_blocks(blocks, 100)
    assert len(selected) == 2 and [len(b[0]) for b in selected] == [50, 50]
    assert any("Лицензия" in b[0] for b in selected)
    assert all(any(excerpt in original for original, _, _ in blocks) for excerpt, _, _ in selected)


def test_late_html_disclosure_and_scoped_citation(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        "<nav>" + "Меню " * 1000 + "</nav></span>"
        "<p>Политика обработки персональных данных: синтетический документ.</p>",
        text_limit=100,
    )
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    report = run(root)
    observations = [o for o in report.observations if o.stage_id == "04" and o.kind == "text_quote"]
    assert observations and "Политика" in observations[0].citations[0].quote
    assert observations[0].citations[0].static_span.status == "partial"
    assert any("Static text budget" in reason for reason in report.limitations)
    assert next(s for s in report.stages if s.stage_id == "04").status == "insufficient_evidence"
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_balanced_html_late_disclosure_not_lost(tmp_path):
    root, _ = make_dossier(
        tmp_path,
        "<nav>" + "Меню " * 1000 + "</nav>"
        "<p>Политика обработки персональных данных: синтетический документ.</p>",
        text_limit=100,
    )
    report = run(root)
    assert any(o.stage_id == "04" and o.kind == "text_quote" for o in report.observations)


def test_empty_strict_prefix_does_not_hide_recovered_disclosure(tmp_path):
    root, _ = make_dossier(
        tmp_path, "</span><p>Политика обработки персональных данных: синтетический документ.</p>"
    )
    report = run(root)
    assert any(o.stage_id == "04" and o.kind == "text_quote" for o in report.observations)
    assert not report.materials[0].reading.text_coverage_complete


def test_complete_text_keeps_adjacent_requisites_and_reproduced_citation(tmp_path):
    root, _ = make_dossier(tmp_path, "<p>ООО «Синтетический оператор»</p><p>ИНН 0000000000</p>")
    report = run(root)
    assert report.organizations[0].inn == "0000000000"
    assert report.organizations[0].identification_sources[0].basis == "reproduced_text"


@pytest.mark.parametrize(
    "label",
    [
        "Политика обработки персональных данных",
        "Согласие",
        "Лицензия",
        "Правила оказания платных услуг",
        "Реквизиты",
        "Прайс-лист",
    ],
)
def test_opaque_pdf_filename_uses_label(label):
    opaque = "https://clinic.example/z999.pdf"
    ordinary = "https://clinic.example/a000.pdf"
    hints = document_hints(opaque, label)
    assert hints
    assert document_sort_key(opaque, hints) < document_sort_key(ordinary, [])


def test_direct_hints_outrank_parent_context_and_decode_url():
    contextual = document_hints("https://clinic.example/one.pdf", "Скачать", "Лицензии")
    direct = document_hints(
        "https://clinic.example/%D0%A1%D0%BE%D0%B3%D0%BB%D0%B0%D1%81%D0%B8%D0%B5.pdf", ""
    )
    assert direct == ["consent"]
    assert contextual == ["context:license"]
    assert document_sort_key("z", direct) < document_sort_key("a", contextual)


def test_pdf_total_budget_marks_unread_tail(tmp_path):
    root, data = pdf_dossier(tmp_path, ["Readable synthetic page"] * 3, total=7)
    packet = load_packet(root, 2_000_000)
    reading = packet.data.documents[0].reading
    assert reading.pages[0].extraction_state == "partial"
    assert [p.extraction_state for p in reading.pages[1:]] == ["unread", "unread"]
    assert reading.unread_page_ranges == [(2, 2), (3, 3)]
    assert data.documents[0].page_texts.keys() == {1}
    assert not reading.text_coverage_complete


def test_pdf_over_inventory_limit_preserves_unread_range(tmp_path):
    path = tmp_path / "long.pdf"
    path.write_bytes(pdf_bytes(["Synthetic"] * 502))
    inventory = extract_inventory(path, 1, 10)
    assert inventory.reading.page_count == 502
    assert inventory.reading.unread_page_ranges[-1] == (501, 502)
    assert len(inventory.reading.pages) == 500
    assert not inventory.reading.text_coverage_complete


def test_packet_utf8_exact_limit_and_no_truncation(tmp_path):
    root, _ = make_dossier(tmp_path, "<p>Синтетические сведения</p>")
    packet = load_packet(root, 20_000_000)
    size = len(packet.payload.encode("utf-8"))
    assert load_packet(root, size).payload == packet.payload
    with pytest.raises(DossierError, match=f"{size} > {size - 1} bytes"):
        load_packet(root, size - 1)


@pytest.mark.parametrize("limit", [0, -1, 20_000_001, True, 1.5])
def test_unsafe_packet_configuration_rejected_before_read(limit):
    with pytest.raises(DossierError, match="Packet limit"):
        load_packet(Path("does-not-exist"), limit)


def test_analysis_configuration_allows_larger_bounded_packet():
    settings = {"model": "offline/a", "prompt_price_cap": 1, "completion_price_cap": 1}
    config = AnalysisConfig(
        auditor_a=settings, auditor_b=settings, max_budget_usd=1, max_input_bytes=10_000_000
    )
    assert config.max_input_bytes == 10_000_000
    with pytest.raises(ValidationError):
        AnalysisConfig(
            auditor_a=settings, auditor_b=settings, max_budget_usd=1, max_input_bytes=20_000_001
        )
