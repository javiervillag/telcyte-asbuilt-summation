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
    totals = derive_code_totals(blocks, code_catalog={("UG", "56"): "UG-56", ("UG", "7"): "UG-07"})
    assert totals == ["UG-56 - 170'", "UG-07 - 1"]


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
        "UG-07 - 10'",
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


def test_derive_code_totals_reads_code_first_multiplier_notes() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Planner approved PC-01 x 2 for the splice work.")
    content = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(content)

    assert derive_code_totals(blocks) == ["PC-01 - 2"]
    diagnostics = diagnose_extraction(blocks, code_totals=["PC-01 - 2"])
    assert diagnostics.ambiguous_code_line_count == 0


def test_derive_code_totals_reads_quantity_first_code_when_line_has_direct_total() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "UG-06 - 13 plus 2 x PC-01 for splice work.")
    content = doc.tobytes()
    doc.close()

    totals = derive_code_totals(extract_text_blocks(content))

    assert totals == ["UG-06 - 13", "PC-01 - 2"]


def test_unresolved_callout_is_kept_when_shared_with_supported_code_line() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "UG-06 - 13 EOL - 48Ct - 66'")
    page.insert_text((72, 96), "Readable note with enough quantity context for parser review.")
    page.insert_text((72, 120), "Additional readable note for text-layer confidence.")
    content = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(content)
    totals = derive_code_totals(blocks)
    diagnostics = diagnose_extraction(blocks, code_totals=totals)

    assert totals == ["UG-06 - 13"]
    assert diagnostics.review_required is True
    assert diagnostics.unresolved_callouts == ["EOL - 48Ct - 66'"]


def test_unresolved_callout_segment_preserves_numbered_marker() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "UG-06 - 13 #3 EOL - 48Ct - 66'")
    content = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(content)
    diagnostics = diagnose_extraction(blocks, code_totals=derive_code_totals(blocks))

    assert diagnostics.unresolved_callouts == ["#3 EOL - 48Ct - 66'"]


def test_unresolved_callouts_split_multiple_segments_on_one_line() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(
        (72, 72),
        "UG-06 - 13 EOL - 48Ct - 66' #2 Tie Point - 48Ct - 52' Storage - 48Ct - 108'",
    )
    content = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(content)
    diagnostics = diagnose_extraction(blocks, code_totals=derive_code_totals(blocks))

    assert diagnostics.unresolved_callouts == [
        "EOL - 48Ct - 66'",
        "#2 Tie Point - 48Ct - 52'",
        "Storage - 48Ct - 108'",
    ]


def test_unresolved_callout_details_extract_generic_fields() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text(
        (72, 72),
        "Pull through - 48Ct #3 EOL - 48Ct - 40' Tie Point - .625",
    )
    content = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(content)
    diagnostics = diagnose_extraction(blocks, code_totals=derive_code_totals(blocks))

    assert diagnostics.unresolved_callout_details == [
        {
            "raw_text": "Pull through - 48Ct",
            "marker": "",
            "callout_type": "Pull Through",
            "descriptor": "48Ct",
            "cable_count": "48Ct",
            "footage": "",
        },
        {
            "raw_text": "#3 EOL - 48Ct - 40'",
            "marker": "#3",
            "callout_type": "EOL",
            "descriptor": "48Ct - 40'",
            "cable_count": "48Ct",
            "footage": "40'",
        },
        {
            "raw_text": "Tie Point - .625",
            "marker": "",
            "callout_type": "Tie Point",
            "descriptor": ".625",
            "cable_count": "",
            "footage": "",
        },
    ]


def test_quantity_first_code_notes_do_not_duplicate_direct_totals() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Planner approved 13 x UG-6 for the PH work.")
    page.insert_text((72, 96), "UG-06 - 13")
    content = doc.tobytes()
    doc.close()

    assert derive_code_totals(extract_text_blocks(content)) == ["UG-06 - 13"]


def test_quantity_first_code_notes_do_not_duplicate_direct_totals_with_units() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "UG-56 - 170' and field note 170 x UG-56.")
    content = doc.tobytes()
    doc.close()

    assert derive_code_totals(extract_text_blocks(content)) == ["UG-56 - 170'"]


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
