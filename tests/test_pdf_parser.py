from pathlib import Path

from app.pdf_parser import build_pdf_context, derive_code_totals, extract_likely_quantity_lines, extract_text_blocks


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
