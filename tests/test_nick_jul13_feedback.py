"""Pins Nick's 2026-07-13 email feedback (Re: Fw: summation asbuilt issues).

Email 1 (6:00 p.m.): add Comp-10 as another code to look for on the fiber
material count, and recognize Top/Pole/P jacket-footage markers (canonical form
has no dash, e.g. Top23712, but the snip itself shows Top-23712, so the dash is
tolerated on already-drawn sheets).

Email 2 (6:15 p.m.): the 48ct is a sequence - the Tie Point callout starts the
cable and the EOL ends it, so the difference of their T (tail) numbers is the
raw footage (24394 - 23560 = 834), then 834 * 1.1 = 917.4 rounds up to 1000'.
Storage/risers/slack are already inside the span. With multiple cables pulled
through the same route(s), the callout/code/hexagon footage is used instead.
"""

import fitz
import pytest

from app.cable_footage import (
    MARKER_PAIR_PATTERN,
    _station_marker_parts,
    derive_cable_footage,
)
from app.models import SummaryResult
from app.pdf_annotator import annotate_pdf
from app.pdf_parser import TextBlock, _unresolved_callout_lines


def _block(text: str, *, page: int = 1, source: str = "annotation") -> TextBlock:
    return TextBlock(page=page, bbox=(0.0, 0.0, 240.0, 80.0), text=text, source=source)


def _email_sequence_blocks() -> list[TextBlock]:
    """The snip's callouts plus the off-crop Tie Point Nick's math implies."""
    return [
        _block("EOL - 48Ct - 50'\nT23560 - D23610"),
        _block("Storage - 48Ct - 26'\nD23656 - D23668"),
        _block("Riser - 48Ct\nD23692 - Top-23712"),
        _block("Tie Point - 48Ct - 50'\nT24394 - D24344"),
    ]


def _short_sequence_blocks() -> list[TextBlock]:
    """800' tail span -> 880' buffered -> 900' rounded."""
    return [
        _block("EOL - 48Ct - 50'\nT1000 - D1050"),
        _block("Tie Point - 48Ct - 50'\nT1800 - D1750"),
    ]


def _materials_box_content(pdf_bytes: bytes) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for annotation in doc[0].annots() or []:
            content = str((annotation.info or {}).get("content", ""))
            if content.lower().startswith("materials"):
                return content
    finally:
        doc.close()
    return ""


def test_marker_parts_accept_top_pole_p_with_and_without_dash() -> None:
    assert _station_marker_parts("Top23712") == ("P", 23712)
    assert _station_marker_parts("Pole23712") == ("P", 23712)
    assert _station_marker_parts("P23712") == ("P", 23712)
    assert _station_marker_parts("Top-23712") == ("P", 23712)
    assert _station_marker_parts("T23560") == ("T", 23560)
    assert _station_marker_parts("D23610") == ("D", 23610)


def test_marker_pair_matches_pole_top_and_rejects_pole_ids_and_codes() -> None:
    match = MARKER_PAIR_PATTERN.search("D23692 - Top-23712")
    assert match is not None
    assert match.group("a") == "D23692"
    assert match.group("b") == "Top-23712"
    # Pole IDs (PH11104E) and billing codes (Comp-15, max 3 digits) never match.
    assert MARKER_PAIR_PATTERN.search("LE PH11104E - PH11105E") is None
    assert MARKER_PAIR_PATTERN.search("Comp-15 - 46") is None


def test_fiber_tail_sequence_email_example_rounds_to_1000() -> None:
    """834 * 1.1 = 917.4 -> 1000' (Nick's worked example, T24394 - T23560)."""
    result = derive_cable_footage(_email_sequence_blocks(), auto_stamp=True)

    assert result.warnings == []
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.path_source == "tail_sequence"
    assert line.path_subtotal == 834
    assert line.included_storage_ft == 0
    assert line.subtotal_used == 834
    assert line.buffered_ft_before_rounding == pytest.approx(917.4)
    assert line.total_ft == 1000
    assert line.material_line == "605-3277 (48Ct) - 1000'"
    assert line.eligible_for_stamp is True
    assert "Tie Point T24394 - EOL T23560 = 834'" in line.path_segments[0].source


def test_tail_sequence_marker_lines_do_not_stay_unresolved() -> None:
    blocks = _email_sequence_blocks()
    result = derive_cable_footage(blocks)

    unresolved = _unresolved_callout_lines(blocks, resolved_callout_lines=result.handled_callout_lines)

    assert unresolved == []


def test_tail_sequence_agrees_with_path_codes_and_notes_it() -> None:
    blocks = _email_sequence_blocks() + [
        _block("Comp-15 - 46'\nComp-15 - 44'"),
        _block("Comp-10 - 300'\nComp-10 - 310'"),
    ]
    # Codes method: path 700 + storage 126 = 826 * 1.1 = 908.6 -> 1000 (agrees).
    result = derive_cable_footage(blocks, auto_stamp=True)

    assert result.warnings == []
    line = result.lines[0]
    assert line.path_source == "tail_sequence"
    assert line.total_ft == 1000
    assert line.eligible_for_stamp is True
    assert any("agree at 1000'" in note for note in result.informational_notes)
    assert any("combined Comp-10, Comp-15 callouts" in note for note in result.informational_notes)
    assert any("corroborates combined Comp-10, Comp-15" in note for note in result.informational_notes)


def test_tail_sequence_disagreement_with_path_codes_flags_review() -> None:
    blocks = _email_sequence_blocks() + [_block("Comp-15 - 46'\nComp-15 - 44'")]
    # Codes method: 90 + 126 storage = 216 * 1.1 = 237.6 -> 300 vs sequence 1000.
    result = derive_cable_footage(blocks, auto_stamp=True)

    line = result.lines[0]
    assert line.path_source == "tail_sequence"
    assert line.total_ft == 1000
    assert line.eligible_for_stamp is False
    assert line.material_line == ""
    assert line.review_material_line == "605-3277 (48Ct) - VERIFY"
    assert any("disagree" in flag for flag in line.review_flags)
    assert any("verify cable length" in warning for warning in result.warnings)
    assert any(issue.severity == "action" for issue in result.issues)


def test_tail_sequence_lower_than_path_codes_also_flags_review() -> None:
    blocks = _short_sequence_blocks() + [_block("Comp-15 - 800'")]
    # Codes method: 800 + 100 terminal slack = 900 * 1.1 = 990 -> 1000,
    # while the tail sequence rounds to 900. Never silently under-order.
    result = derive_cable_footage(blocks, auto_stamp=True)

    line = result.lines[0]
    assert line.path_source == "tail_sequence"
    assert line.total_ft == 900
    assert line.eligible_for_stamp is False
    assert line.material_line == ""
    assert line.review_material_line == "605-3277 (48Ct) - VERIFY"
    assert any("900'" in flag and "1000'" in flag for flag in line.review_flags)


def test_dual_primary_codes_without_sequence_require_review() -> None:
    blocks = [
        _block("Tie Point - 48Ct"),
        _block("Comp-15 - 500'\nComp-10 - 500'"),
    ]

    result = derive_cable_footage(blocks, auto_stamp=True)

    line = result.lines[0]
    assert line.path_source == "path_codes"
    assert line.path_subtotal == 1000
    assert line.total_ft == 1100
    assert line.eligible_for_stamp is False
    assert line.material_line == ""
    assert line.review_material_line == "605-3277 (48Ct) - VERIFY"
    assert any("Comp-10, Comp-15 both contribute" in flag for flag in line.review_flags)


def test_dual_primary_codes_disagreement_requires_review() -> None:
    blocks = _email_sequence_blocks() + [
        _block("Comp-15 - 500'\nComp-10 - 500'"),
    ]

    result = derive_cable_footage(blocks, auto_stamp=True)

    line = result.lines[0]
    assert line.path_source == "tail_sequence"
    assert line.total_ft == 1000
    assert line.eligible_for_stamp is False
    assert line.review_material_line == "605-3277 (48Ct) - VERIFY"
    assert any("1000'" in flag and "1300'" in flag for flag in line.review_flags)
    assert any("Comp-10, Comp-15 both contribute" in flag for flag in line.review_flags)


def test_comp10_only_sheet_counts_as_primary_path_footage() -> None:
    blocks = [
        _block("Comp-10 - 500'"),
        _block("Storage - 48Ct - 50'\nEOL - 48Ct - 30'\nTie Point - 48Ct - 20'"),
    ]
    # 500 + 100 storage = 600 * 1.1 = 660 -> 700.
    result = derive_cable_footage(blocks, auto_stamp=True)

    assert result.warnings == []
    line = result.lines[0]
    assert line.path_source == "path_codes"
    assert line.path_subtotal == 500
    assert line.storage_subtotal == 100
    assert line.total_ft == 700
    assert line.material_line == "605-3277 (48Ct) - 700'"
    assert line.eligible_for_stamp is True


def test_terminal_marker_mismatch_blocks_stamp_and_warns() -> None:
    blocks = [
        _block("EOL - 48Ct - 50'\nT23560 - D23640"),  # span 80 != labeled 50
        _block("Tie Point - 48Ct - 50'\nT24394 - D24344"),
        _block("Comp-15 - 760'"),
    ]
    result = derive_cable_footage(blocks, auto_stamp=True)

    line = result.lines[0]
    assert line.path_source == "path_codes"  # sequence rejected, codes still shown
    assert line.eligible_for_stamp is False
    assert any("does not match the labeled footage" in flag for flag in line.review_flags)


def test_multiple_tie_points_fall_back_to_path_codes() -> None:
    """Nick's messy case: multiple cables pulled through the same route."""
    blocks = [
        _block("Tie Point - 48Ct - 50'\nT24394 - D24344"),
        _block("Tie Point - 48Ct - 40'\nT51200 - D51160"),
        _block("EOL - 48Ct - 50'\nT23560 - D23610"),
        _block("Comp-15 - 500'"),
    ]
    result = derive_cable_footage(blocks, auto_stamp=True)

    line = result.lines[0]
    assert line.path_source == "path_codes"
    # 500 + 140 storage = 640 * 1.1 = 704 -> 800.
    assert line.total_ft == 800
    assert line.eligible_for_stamp is True
    assert any("tie point/EOL tail markers" in note for note in result.informational_notes)


def test_splice_presence_falls_back_to_path_codes() -> None:
    blocks = [
        _block("Tie Point - 48Ct - 50'\nT24394 - D24344"),
        _block("EOL - 48Ct - 50'\nT23560 - D23610"),
        _block("Splice - 48Ct"),
        _block("Comp-15 - 500'"),
    ]
    result = derive_cable_footage(blocks, auto_stamp=True)

    line = result.lines[0]
    assert line.path_source == "path_codes"
    assert any("splice callout" in note.lower() for note in result.informational_notes)


def test_riser_with_labeled_feet_counts_like_storage() -> None:
    blocks = [
        _block("Riser - 48Ct - 25'"),
        _block("EOL - 48Ct - 30'\nTie Point - 48Ct - 20'"),
        _block("Comp-15 - 500'"),
    ]
    # 500 + (25 + 30 + 20) storage = 575 * 1.1 = 632.5 -> 700.
    result = derive_cable_footage(blocks, auto_stamp=True)

    assert result.warnings == []
    line = result.lines[0]
    assert line.storage_subtotal == 75
    assert line.total_ft == 700
    assert line.eligible_for_stamp is True


def test_tie_point_without_space_still_anchors_the_sequence() -> None:
    blocks = [
        _block("EOL - 48Ct - 50'\nT23560 - D23610"),
        _block("TiePoint - 48Ct - 50'\nT24394 - D24344"),
    ]
    result = derive_cable_footage(blocks, auto_stamp=True)

    line = result.lines[0]
    assert line.path_source == "tail_sequence"
    assert line.total_ft == 1000


def test_near_agreement_within_one_increment_requires_review() -> None:
    blocks = _email_sequence_blocks() + [_block("Comp-15 - 650'")]
    # Codes method: 650 + 126 storage = 776 * 1.1 = 853.6 -> 900 vs sequence 1000.
    result = derive_cable_footage(blocks, auto_stamp=True)

    line = result.lines[0]
    assert line.path_source == "tail_sequence"
    assert line.total_ft == 1000
    assert line.eligible_for_stamp is False
    assert line.material_line == ""
    assert line.review_material_line == "605-3277 (48Ct) - VERIFY"
    assert any("1000'" in flag and "900'" in flag for flag in line.review_flags)


def test_same_line_terminal_markers_anchor_tail_sequence() -> None:
    blocks = [
        _block("EOL - 48Ct - 50' T23560 - D23610"),
        _block("Tie Point - 48Ct - 50' T24394 - D24344"),
    ]

    result = derive_cable_footage(blocks, auto_stamp=True)

    line = result.lines[0]
    assert line.path_source == "tail_sequence"
    assert line.path_subtotal == 834
    assert line.material_line == "605-3277 (48Ct) - 1000'"
    assert line.eligible_for_stamp is True


def test_144ct_tail_sequence_updates_only_its_existing_material_row() -> None:
    blocks = [
        _block("EOL - 144Ct - 50'\nT23560 - D23610"),
        _block("Tie Point - 144Ct - 50'\nT24394 - D24344"),
    ]
    cable = derive_cable_footage(blocks, auto_stamp=True)
    line = cable.lines[0]
    assert line.path_source == "tail_sequence"
    assert line.total_ft == 1000
    assert line.material_line == "605-1502 (144Ct) - 1000'"
    assert line.eligible_for_stamp is True

    existing_content = "Materials\n605-1502 (144Ct) - 1400'\nEMT - 10'"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.add_freetext_annot(fitz.Rect(20, 520, 300, 760), existing_content, fontsize=10)
    source = doc.tobytes()
    doc.close()

    summary = SummaryResult(model="parser-test", cable_footage=cable.lines).with_eligible_cable_materials()
    output = annotate_pdf(source, summary)

    assert _materials_box_content(output) == "Materials\n605-1502 (144Ct) - 1000'\nEMT - 10'"


def test_288ct_tail_sequence_uses_fiber_buffer_and_rounding() -> None:
    result = derive_cable_footage(
        [
            _block("EOL - 288Ct T1000"),
            _block("Tie Point - 288Ct T1200"),
        ],
        auto_stamp=True,
    )

    line = result.lines[0]
    assert line.path_source == "tail_sequence"
    assert line.path_subtotal == 200
    assert line.total_ft == 300
    assert line.material_line == "605-1503 (288Ct) - 300'"
    assert line.eligible_for_stamp is True


@pytest.mark.parametrize(
    ("cable_type", "part_number", "tie_marker", "expected"),
    [
        (".625", "220-9236", "T1120", 140),
        (".875", "220-6999", "T1200", 220),
    ],
)
def test_coax_single_terminal_markers_use_sequence_without_storage(
    cable_type: str,
    part_number: str,
    tie_marker: str,
    expected: int,
) -> None:
    result = derive_cable_footage(
        [
            _block(f"EOL - {cable_type} T1000"),
            _block(f"Storage - {cable_type} - 500' D1050"),
            _block(f"Tie Point - {cable_type} {tie_marker}"),
        ],
        auto_stamp=True,
    )

    line = result.lines[0]
    assert line.path_source == "tail_sequence"
    assert line.storage_subtotal == 0
    assert line.total_ft == expected
    assert line.material_line == f"{part_number} ({cable_type}) - {expected}'"
    assert line.eligible_for_stamp is True
    assert not any("Coax source path" in flag for flag in line.review_flags)


def test_coax_sequence_cross_check_excludes_storage_from_code_method() -> None:
    result = derive_cable_footage(
        [
            _block("EOL - .625 T1000"),
            _block("Storage - .625 - 500' D1050"),
            _block("Tie Point - .625 T1120"),
            _block("Comp-15 - 120'"),
        ],
        auto_stamp=True,
    )

    line = result.lines[0]
    assert line.path_source == "tail_sequence"
    assert line.total_ft == 140
    assert line.eligible_for_stamp is True
    assert any("agree at 140'" in note for note in result.informational_notes)


@pytest.mark.parametrize(
    ("cable_type", "part_number", "eol", "tie", "expected"),
    [
        ("RG6", "240-2079", 1000, 1050, 55),
        ("RG11", "240-2083", 2000, 2200, 220),
    ],
)
def test_rg_drop_cable_tail_sequence_uses_whole_foot_rounding(
    cable_type: str,
    part_number: str,
    eol: int,
    tie: int,
    expected: int,
) -> None:
    result = derive_cable_footage(
        [
            _block(f"EOL - {cable_type} T{eol}"),
            _block(f"Tie Point - {cable_type} T{tie}"),
        ],
        auto_stamp=True,
    )

    line = result.lines[0]
    assert line.path_source == "tail_sequence"
    assert line.rounding == "nearest_1"
    assert line.total_ft == expected
    assert line.material_line == f"{part_number} ({cable_type}) - {expected}'"
    assert line.eligible_for_stamp is True


def test_mixed_coax_and_rg11_path_code_keeps_both_types_visible_for_review() -> None:
    result = derive_cable_footage(
        [
            _block("Splice - .625\nTie Point - .625\nEOL - .625"),
            _block("EOL - RG11 - 24'\nTie Point - RG11 - 20'"),
            _block("Comp-15 - 640'"),
        ],
        auto_stamp=True,
    )

    assert {line.callout for line in result.lines} == {".625", "rg11"}
    assert all(line.eligible_for_stamp is False for line in result.lines)
    assert {line.review_material_line for line in result.lines} == {
        "220-9236 (.625) - VERIFY",
        "240-2083 (RG11) - VERIFY",
    }
    assert all(
        any("could not be safely assigned" in flag for flag in line.review_flags)
        for line in result.lines
    )


def test_invalid_configured_path_code_warns_but_valid_codes_still_count() -> None:
    blocks = [
        _block("Comp-15 - 500'"),
        _block("Storage - 48Ct - 100'"),
    ]
    result = derive_cable_footage(blocks, auto_stamp=True, path_codes="Comp-15,BOGUS")

    line = result.lines[0]
    assert line.path_subtotal == 500
    assert line.total_ft == 700
    assert any("BOGUS" in warning for warning in result.warnings)
    assert any(issue.code == "invalid_cable_path_code" for issue in result.issues)


def test_drop_f_terminal_markers_use_confirmed_tail_sequence() -> None:
    blocks = [
        _block(
            "EOL - Drop F - 40'\n"
            "D11444 - T11404\n"
            "Storage - Drop F - 4'\n"
            "D11558 - D11554\n"
            "Tie Point - Drop F - 40'\n"
            "D11688 - T11728"
        )
    ]
    result = derive_cable_footage(blocks, auto_stamp=True)
    drop = next(line for line in result.lines if line.callout == "drop_f")

    assert drop.path_source == "tail_sequence"
    assert drop.path_subtotal == 324
    assert drop.total_ft == 356
    assert drop.rounding == "nearest_1"
