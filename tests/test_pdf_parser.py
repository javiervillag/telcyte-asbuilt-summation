from pathlib import Path

import fitz

from app.pdf_parser import (
    build_pdf_context,
    diagnose_extraction,
    derive_code_totals,
    extract_likely_quantity_lines,
    extract_text_blocks,
)


SAMPLE = Path("/Users/javiervillaguardado/Downloads/Asbuilt Examples for AI Summation/FIBER-ASBUILT-(TelCyte)-BI-829050-Totals Removed.pdf")


def test_parser_extracts_quantity_context() -> None:
    blocks = extract_text_blocks(SAMPLE.read_bytes())
    lines = extract_likely_quantity_lines(blocks)
    joined = "\n".join(lines)
    assert "Tie Point - 48Ct - 100'" in joined
    assert "COMP-15 - 348'" in joined


def test_build_pdf_context_includes_positions() -> None:
    context = build_pdf_context(SAMPLE.read_bytes())
    assert "PDF page metadata" in context
    assert "Positioned text blocks" in context
    assert "Tie Point - 48Ct - 100'" in context
    assert "Deterministic code totals" in context


def test_derive_code_totals_sums_repeated_labels() -> None:
    blocks = extract_text_blocks(SAMPLE.read_bytes())
    totals = derive_code_totals(blocks)
    assert "UG-56 - 170'" in totals
    assert "COMP-15 - 348'" in totals


def test_derive_code_totals_uses_rate_card_display_and_filter() -> None:
    blocks = extract_text_blocks(SAMPLE.read_bytes())
    totals = derive_code_totals(blocks, code_catalog={("UG", 56): "UG-56", ("UG", 7): "UG-07"})
    assert totals == ["UG-56 - 170'", "UG-07 - 1"]


def test_derive_code_totals_ignores_bore_context_notes() -> None:
    sample = Path("/Users/javiervillaguardado/Downloads/Asbuilt Examples for AI Summation/FIBER-ASBUILT-(TelCyte)-BI-912047-Totals Removed.pdf")
    blocks = extract_text_blocks(sample.read_bytes())
    totals = derive_code_totals(blocks)
    assert "UG-6 - 14" not in totals
    assert "UG-06 - 13" in totals


def test_diagnose_extraction_requires_review_for_blank_pdf() -> None:
    doc = fitz.open()
    doc.new_page(width=612, height=792)
    blank = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(blank)
    diagnostics = diagnose_extraction(blocks, code_totals=[])

    assert diagnostics.review_required is True
    assert "Manual review is required; the app did not add unsupported totals." in diagnostics.warnings
