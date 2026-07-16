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

import pytest

from app.cable_footage import (
    MARKER_PAIR_PATTERN,
    _station_marker_parts,
    derive_cable_footage,
)
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


def test_tail_sequence_disagreement_with_path_codes_flags_review() -> None:
    blocks = _email_sequence_blocks() + [_block("Comp-15 - 46'\nComp-15 - 44'")]
    # Codes method: 90 + 126 storage = 216 * 1.1 = 237.6 -> 300 vs sequence 1000.
    result = derive_cable_footage(blocks, auto_stamp=True)

    line = result.lines[0]
    assert line.path_source == "tail_sequence"
    assert line.total_ft == 1000
    assert line.eligible_for_stamp is False
    assert any("disagree" in flag for flag in line.review_flags)
    assert any("verify cable length" in warning for warning in result.warnings)


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


def test_drop_f_station_markers_still_use_d_span_method() -> None:
    """The Drop F method (D-span + terminal slack) is untouched by the trunk
    tail-sequence; mirrors test_drop_f_station_markers_derive_base..."""
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

    assert drop.path_source == "station_markers"
    assert drop.path_subtotal == 324
    assert drop.total_ft == 356
