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
    assert "UG-56 - 170" in totals
    assert "COMP-15 - 348" in totals


def test_derive_code_totals_uses_rate_card_display_and_filter() -> None:
    blocks = extract_text_blocks(SAMPLE.read_bytes())
    totals = derive_code_totals(blocks, code_catalog={("UG", "56"): "UG-56", ("UG", "7"): "UG-07"})
    assert totals == ["UG-56 - 170", "UG-07 - 1"]


def test_derive_code_totals_reads_pdf_annotation_text_boxes() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.add_freetext_annot(
        fitz.Rect(72, 72, 220, 116),
        "UG-7 - 10'\nCOMP-9 - 2",
        fontsize=10,
    )
    content = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(content)
    assert any(block.source == "annotation" for block in blocks)
    assert derive_code_totals(blocks, code_catalog={("UG", "7"): "UG-07", ("COMP", "9"): "Comp-9"}) == [
        "UG-07 - 10",
        "Comp-9 - 2",
    ]


def test_derive_code_totals_keeps_composite_zero_padded_variants_separate() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Comp-9 - 2\nComp-09 - 3")
    content = doc.tobytes()
    doc.close()

    totals = derive_code_totals(extract_text_blocks(content))

    assert totals == ["Comp-9 - 2", "Comp-09 - 3"]


def test_derive_code_totals_ignores_eli_codes() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "ELI-7 - 99\nUG-7 - 4")
    content = doc.tobytes()
    doc.close()

    assert derive_code_totals(extract_text_blocks(content)) == ["UG-07 - 4"]


def test_derive_code_totals_reads_quantity_first_code_notes() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Planner approved 13 x UG-6 for the PH work.")
    content = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(content)

    assert derive_code_totals(blocks) == ["UG-06 - 13"]
    diagnostics = diagnose_extraction(blocks, code_totals=["UG-06 - 13"])
    assert diagnostics.ambiguous_code_line_count == 0


def test_quantity_first_code_notes_do_not_duplicate_direct_totals() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Planner approved 13 x UG-6 for the PH work.")
    page.insert_text((72, 96), "UG-06 - 13")
    content = doc.tobytes()
    doc.close()

    assert derive_code_totals(extract_text_blocks(content)) == ["UG-06 - 13"]


def test_derive_code_totals_counts_dirt_pothole_callouts_in_sample() -> None:
    # BI-912047 has "Dirt - UG-6 - 1" (pothole callout) alongside a direct
    # "UG-06 - 13" line. Per Nick's 2026-06-09 guidance (Segment 7 PRJ17),
    # surface-descriptor callouts are billable and must be totaled -> 14.
    # NOTE: pending Nick's confirmation whether a coexisting direct-total
    # line should subsume descriptor callouts (manual box on this sample
    # said 13). See tests/test_nick_jun9_feedback.py.
    sample = Path("/Users/javiervillaguardado/Downloads/Asbuilt Examples for AI Summation/FIBER-ASBUILT-(TelCyte)-BI-912047-Totals Removed.pdf")
    blocks = extract_text_blocks(sample.read_bytes())
    totals = derive_code_totals(blocks)
    assert "UG-06 - 14" in totals


def test_derive_code_totals_reads_future_code_prefixes_and_surface_labels() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "SME-1 - 1")
    page.insert_text((72, 96), "DP-11 - 156'")
    page.insert_text((72, 120), "Asphalt - UG-06 - 2")
    page.insert_text((72, 144), "Concrete - UG-06 - 2")
    page.insert_text((72, 168), "UG-85 - 1 - UG-06 - 1")
    content = doc.tobytes()
    doc.close()

    totals = derive_code_totals(extract_text_blocks(content))

    assert totals == ["SME-01 - 1", "DP-11 - 156", "UG-06 - 5", "UG-85 - 1"]


def test_derive_code_totals_counts_dirt_surface_descriptor_codes() -> None:
    # Segment 7 PRJ17 regression (Nick Evans, 2026-06-09): DIRT- is a surface
    # descriptor like CONCRETE-/ASPHALT-, not a non-billing marker.
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Dirt - UG-6 - 1")
    page.insert_text((72, 96), "UG-06 - 2")
    content = doc.tobytes()
    doc.close()

    assert derive_code_totals(extract_text_blocks(content)) == ["UG-06 - 3"]


def test_diagnose_extraction_requires_review_for_blank_pdf() -> None:
    doc = fitz.open()
    doc.new_page(width=612, height=792)
    blank = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(blank)
    diagnostics = diagnose_extraction(blocks, code_totals=[])

    assert diagnostics.review_required is True
    assert "Manual review is required; the app did not add unsupported totals." in diagnostics.warnings


def test_diagnose_extraction_requires_review_when_no_supported_totals() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "General construction note with readable text.")
    page.insert_text((72, 96), "Another readable note, but no supported billing codes are present.")
    page.insert_text((72, 120), "Material location callout and station notes only.")
    content = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(content)
    diagnostics = diagnose_extraction(blocks, code_totals=[])

    assert diagnostics.review_required is True
    assert "No supported billing-code totals were found in the parsed text." in diagnostics.warnings


def test_diagnose_extraction_requires_review_for_partial_code_text() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "UG-56")
    page.insert_text((72, 96), "Readable but incomplete billing-code text.")
    content = doc.tobytes()
    doc.close()

    parsed_blocks = extract_text_blocks(content)
    diagnostics = diagnose_extraction(
        parsed_blocks,
        code_totals=[],
        quantity_lines=extract_likely_quantity_lines(parsed_blocks),
    )

    assert diagnostics.review_required is True
    assert diagnostics.ambiguous_code_line_count == 1
    assert "Some billing-code text was readable but not complete enough to total automatically." in diagnostics.warnings
