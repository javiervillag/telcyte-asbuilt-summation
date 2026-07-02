from app.cable_footage import (
    buffered_cable_footage,
    cable_material_key,
    canonicalize_cable_material_row,
    derive_cable_footage,
    extract_material_rows,
    merge_material_rows,
    normalize_cable_type,
)
from app.pdf_parser import TextBlock, _unresolved_callout_lines, derive_code_totals


def _block(text: str, *, page: int = 1, source: str = "annotation") -> TextBlock:
    return TextBlock(page=page, bbox=(0.0, 0.0, 240.0, 80.0), text=text, source=source)


def test_normalize_cable_type_variants() -> None:
    assert normalize_cable_type("48Ct") == "48ct"
    assert normalize_cable_type("048 count") == "48ct"
    assert normalize_cable_type(".625") == ".625"
    assert normalize_cable_type(".875") == ".875"
    assert normalize_cable_type("Drop F") == "drop_f"
    assert normalize_cable_type("PWR-625") is None


def test_material_cable_keys_include_legacy_bare_rows_without_hitting_manual_rows() -> None:
    assert cable_material_key("605-3277 (48Ct) - 1000'") == "48ct"
    assert cable_material_key("605-3277 - 1200'") == "48ct"
    assert cable_material_key("48Ct - 1200'") == "48ct"
    assert cable_material_key(".625 - 140'") == ".625"
    assert cable_material_key("220-9236 (.625) - 140'") == ".625"
    assert cable_material_key("Spare 605-3277 cable - 20'") is None
    assert cable_material_key("605-3277 spare cable - 20'") is None
    assert cable_material_key("Spare coil (48Ct) - 20'") is None
    assert cable_material_key("Mule - 900'") is None
    assert cable_material_key("EMT - 20'") is None
    assert cable_material_key('2" PVC - 40\'') is None
    assert cable_material_key("Tape - 1") is None


def test_canonicalize_cable_material_rows_preserves_footage_and_avoids_notes() -> None:
    assert canonicalize_cable_material_row("144ct - 1192'") == "605-1502 (144Ct) - 1192'"
    assert canonicalize_cable_material_row("605-3277 - 603'") == "605-3277 (48Ct) - 603'"
    assert canonicalize_cable_material_row("605-3277 (48Ct) - 603'") == "605-3277 (48Ct) - 603'"
    assert canonicalize_cable_material_row(".625 - 140'") == "220-9236 (.625) - 140'"
    assert canonicalize_cable_material_row("Spare 605-3277 cable - 20'") == "Spare 605-3277 cable - 20'"
    assert canonicalize_cable_material_row("605-3277 spare cable - 20'") == "605-3277 spare cable - 20'"
    assert canonicalize_cable_material_row("Spare coil (48Ct) - 20'") == "Spare coil (48Ct) - 20'"
    assert canonicalize_cable_material_row("EMT - 10'") == "EMT - 10'"
    assert canonicalize_cable_material_row("470-9997 - 500'") == "470-9997 - 500'"


def test_merge_material_rows_replaces_cable_rows_and_preserves_manual_rows() -> None:
    existing = extract_material_rows(
        "Material\n\n48Ct - 1200'\nLg Ped - 2\nEMT - 20'\nMule - 900'\nTape - 1"
    )
    computed = ["605-3277 (48Ct) - 1200'"]

    merged = merge_material_rows(existing, computed)

    assert merged == [
        "605-3277 (48Ct) - 1200'",
        "Lg Ped - 2",
        "EMT - 20'",
        "Mule - 900'",
        "Tape - 1",
    ]


def test_merge_material_rows_replaces_each_cable_type_independently() -> None:
    existing = [
        "48Ct - 1000'",
        ".625 - 140'",
        "EMT - 10'",
    ]
    computed = [
        "605-3277 (48Ct) - 1200'",
    ]

    merged = merge_material_rows(existing, computed)

    assert merged == [
        "605-3277 (48Ct) - 1200'",
        "220-9236 (.625) - 140'",
        "EMT - 10'",
    ]


def test_merge_material_rows_normalizes_preliminary_rows_without_computed_rows() -> None:
    # Building the stamped Materials box, fiber cable rows are also given the order
    # quantity: +10% buffer rounded up to the next 100' (Nick, BI-872022).
    # 1192 -> 1400, 603 -> 700. Non-cable rows keep their exact value.
    existing = [
        "144ct - 1192'",
        "LockBox - 1",
        "EMT - 10'",
        'EMT 2" Fitting - 2',
        "470-9997 - 500'",
        "605-3277 - 603'",
        "460-0008 - 1315'",
    ]

    merged = merge_material_rows(existing, [])

    assert merged == [
        "605-1502 (144Ct) - 1400'",
        "LockBox - 1",
        "EMT - 10'",
        'EMT 2" Fitting - 2',
        "470-9997 - 500'",
        "605-3277 (48Ct) - 700'",
        "460-0008 - 1315'",
    ]


def test_fiber_material_uses_comp15_only_and_rounds_up() -> None:
    blocks = [
        _block("Storage - 48Ct - 100'\nStorage - 48Ct - 100'\nStorage - 48Ct - 100'"),
        _block("Storage - 48Ct - 100'\nStorage - 48Ct - 100'\nStorage - 48Ct - 100'"),
        _block("Tie Point - 48Ct - 100'\nEOL - 48Ct - 30'"),
        _block("Comp-15 - 290'\nComp-15 - 270'\nComp-15 - 336'"),
        _block("Comp-15 - 124'\nComp-15 - 552'\nComp-15 - 200'"),
        _block("UG-56 - 358'"),  # pull-through cue, not a cable-footage addend
    ]

    result = derive_cable_footage(blocks, auto_stamp=False)

    assert result.warnings == []
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.path_subtotal == 1772
    assert line.storage_subtotal == 730
    assert line.total_ft == 2800
    assert line.material_line == "605-3277 (48Ct) - 2800'"
    assert line.eligible_for_stamp is False
    assert any("not stamped" in note for note in result.informational_notes)

    stamped = derive_cable_footage(blocks, auto_stamp=True).lines[0]
    assert stamped.eligible_for_stamp is True


def test_bi_942102_fiber_material_rounds_to_1700() -> None:
    blocks = [
        _block("Comp-15 - 1200'\nComp-15 - 28'"),
        _block("EOL - 48Ct - 122'\nStorage - 48Ct - 100'\nTie Point - 48Ct - 68'"),
        _block("UG-28 - 1\nUG-16 - 1\nUG-17 - 1"),
    ]

    result = derive_cable_footage(blocks, auto_stamp=True)

    assert result.warnings == []
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.path_subtotal == 1228
    assert line.storage_subtotal == 290
    assert line.total_ft == 1700
    assert line.material_line == "605-3277 (48Ct) - 1700'"
    assert line.eligible_for_stamp is True


def test_cable_path_is_independent_of_rate_card_filtering() -> None:
    blocks = [_block("Storage - 48Ct - 100'\nComp-15 - 290'\nComp-15 - 270'")]

    billing_totals = derive_code_totals(blocks, code_catalog={("UG", "56"): "UG-56"})
    cable = derive_cable_footage(blocks)

    assert billing_totals == []
    assert cable.lines[0].path_subtotal == 560


def test_coax_rollup_box_is_not_double_counted_and_stays_review_gated() -> None:
    blocks = [
        _block("Tie Point - .625 T998\nSplice - .625 T1032\nEOL - .625 T990"),
        _block("COMP-15 - 34'\nCOMP-1 - 34'"),
        _block("COMP-15 - 84'\nCOMP-1 - 84'"),
        _block("MKR Job Totals\nCOMP-15 - 118'\nCOMP-1 - 118'", source="annotation"),
        _block("Materials\n220-9236 (.625) - 140'", source="annotation"),
    ]

    result = derive_cable_footage(blocks, auto_stamp=True)

    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.family == "coax"
    assert line.path_subtotal == 118
    assert line.storage_subtotal == 0
    assert line.material_line == "220-9236 (.625) - 130'"
    assert line.eligible_for_stamp is False
    assert any("Coax source path" in flag for flag in line.review_flags)
    assert not any("Materials" in note for note in result.informational_notes)


def test_resolved_cable_callouts_do_not_stay_unresolved() -> None:
    blocks = [_block("Storage - 48Ct - 100'\nTie Point - 48Ct - 100'\nEOL - 48Ct - 30'\nComp-15 - 290'")]
    result = derive_cable_footage(blocks)

    unresolved = _unresolved_callout_lines(blocks, resolved_callout_lines=result.handled_callout_lines)

    assert unresolved == []


def test_drop_f_station_markers_derive_base_without_double_counting_storage() -> None:
    blocks = [
        _block(
            "EOL - Drop F - 40'\n"
            "D11444 - T11404\n"
            "Storage - Drop F - 4'\n"
            "D11558 - D11554\n"
            "Tie Point - Drop F - 40'\n"
            "D11688 - T11728\n"
            "EOL - 48Ct - 100'\n"
            "D34116 - T34166"
        )
    ]

    result = derive_cable_footage(blocks, auto_stamp=True)
    drop = next(line for line in result.lines if line.callout == "drop_f")

    assert drop.path_subtotal == 324
    assert drop.storage_subtotal == 0
    assert drop.total_ft == 356
    assert drop.material_line == "240-0318 (Drop F) - 356'"
    assert drop.eligible_for_stamp is True


def test_drop_f_station_marker_mismatch_warns_without_stamping_bad_quantity() -> None:
    result = derive_cable_footage(
        [_block("EOL - Drop F - 100'\nD11444 - T11404\nTie Point - Drop F - 40'\nD11688 - T11728")],
        auto_stamp=True,
    )
    drop = next(line for line in result.lines if line.callout == "drop_f")

    assert drop.material_line == ""
    assert drop.review_material_line == "240-0318 (Drop F) - VERIFY"
    assert any("does not match" in warning for warning in result.warnings)


def test_fiber_storage_without_supported_path_gets_visible_verify_material() -> None:
    result = derive_cable_footage(
        [_block("EOL - 48Ct - 30'\nTie Point - 48Ct - 100'\nStorage - 48Ct - 106'")],
        auto_stamp=True,
    )
    line = result.lines[0]

    assert line.material_line == ""
    assert line.review_material_line == "605-3277 (48Ct) - VERIFY"
    assert line.eligible_for_stamp is False


def test_verify_material_row_is_keyed_and_replaced_by_numeric_row() -> None:
    assert merge_material_rows(["605-3277 (48Ct) - VERIFY"], ["605-3277 (48Ct) - 600'"]) == [
        "605-3277 (48Ct) - 600'",
    ]
    assert merge_material_rows(["605-3277 (48Ct) - 1000'"], ["605-3277 (48Ct) - VERIFY"]) == [
        "605-3277 (48Ct) - 1000'",
    ]


# ---------------------------------------------------------------------------
# BI-872022: stamped Materials box must (1) remap the legacy 144ct part number to
# the current one, (2) label the cable type, (3) add the 10% buffer + round up to
# the next 100'. Single rounding rule lives in buffered_cable_footage().
# ---------------------------------------------------------------------------


def test_buffered_cable_footage_rule() -> None:
    # fiber: +10% then ceil to next 100'
    assert buffered_cable_footage(1810, "fiber") == 2000
    assert buffered_cable_footage(4270, "fiber") == 4700
    assert buffered_cable_footage(100, "fiber") == 200
    assert buffered_cable_footage(2000, "fiber") == 2200  # exact multiples still buffer
    # coax: +10% then ceil to the configured increment (default 10)
    assert buffered_cable_footage(500, "coax", 10) == 550
    assert buffered_cable_footage(118, "coax", 10) == 130


def test_cable_material_key_recognizes_legacy_part_number() -> None:
    # Cox's old printed 144ct part number must be recognized as a 144ct cable row.
    assert cable_material_key("605-3324 - 1810'") == "144ct"
    assert cable_material_key("605-3324 (144Ct) - 1810'") == "144ct"


def test_canonicalize_remaps_legacy_part_default_preserves_footage() -> None:
    # Default (no buffer) keeps the legacy-row footage but emits the CURRENT part #.
    assert canonicalize_cable_material_row("605-3324 - 1810'") == "605-1502 (144Ct) - 1810'"


def test_canonicalize_buffer_remaps_labels_and_rounds_bi872022() -> None:
    # All three of Nick's concerns at once for the 144ct row, plus the 48ct buffer.
    assert (
        canonicalize_cable_material_row("605-3324 - 1810'", apply_buffer=True)
        == "605-1502 (144Ct) - 2000'"
    )
    assert (
        canonicalize_cable_material_row("605-3277 - 4270'", apply_buffer=True)
        == "605-3277 (48Ct) - 4700'"
    )
    assert (
        canonicalize_cable_material_row("144Ct - 1810'", apply_buffer=True)
        == "605-1502 (144Ct) - 2000'"
    )


def test_canonicalize_buffer_is_idempotent_and_heals_old_outputs() -> None:
    # A fully-finalized row (labeled + footage already a multiple of 100') is left
    # alone, so re-running an output never re-buffers.
    once = canonicalize_cable_material_row("605-3324 - 1810'", apply_buffer=True)
    assert once == "605-1502 (144Ct) - 2000'"
    assert canonicalize_cable_material_row(once, apply_buffer=True) == once
    # A pre-fix output (labeled but NOT yet rounded) self-heals on the next run.
    assert (
        canonicalize_cable_material_row("605-3277 (48Ct) - 4270'", apply_buffer=True)
        == "605-3277 (48Ct) - 4700'"
    )


def test_canonicalize_buffer_does_not_rebuffer_canonical_round_label() -> None:
    # DELIBERATE TRADEOFF (reviewer edge case, 2026-06-25): a row that already looks
    # exactly like THIS tool's canonical output - (NNCt) label AND a round multiple of
    # 100' - is treated as a prior output and left unbuffered, so re-runs stay
    # idempotent. The known cost is that a hypothetical RAW source callout printed in
    # that exact format would not get its +10% buffer. We accept that: idempotency is a
    # hard invariant, and field/Cox callouts use the legacy bare part or an unrounded
    # measurement, not our finished label.
    assert (
        canonicalize_cable_material_row("605-1502 (144Ct) - 2000'", apply_buffer=True)
        == "605-1502 (144Ct) - 2000'"
    )
    # The freeze is narrow: it requires BOTH the label AND the round-100 footage. A
    # labeled-but-unrounded row still buffers (self-heals)...
    assert (
        canonicalize_cable_material_row("605-1502 (144Ct) - 1810'", apply_buffer=True)
        == "605-1502 (144Ct) - 2000'"
    )
    # ...and a bare/legacy part with no canonical label always buffers, even at a round
    # multiple of 100, because it is unmistakably a source row, not our output.
    assert (
        canonicalize_cable_material_row("605-3324 - 2000'", apply_buffer=True)
        == "605-1502 (144Ct) - 2200'"
    )


def test_canonicalize_buffer_leaves_coax_and_non_cable_rows_untouched() -> None:
    # Coax is relabeled but never auto-buffered (source path still needs validation).
    assert (
        canonicalize_cable_material_row(".625 - 140'", apply_buffer=True)
        == "220-9236 (.625) - 140'"
    )
    # Non-cable hardware rows pass through verbatim even with the buffer flag on.
    assert canonicalize_cable_material_row("470-9997 - 460'", apply_buffer=True) == "470-9997 - 460'"
    assert canonicalize_cable_material_row("600-4013 - 54", apply_buffer=True) == "600-4013 - 54"


def test_merge_material_rows_bi872022_remaps_labels_buffers_and_is_idempotent() -> None:
    existing = [
        "470-9997 - 460'",
        "605-3277 - 4270'",
        "605-3324 - 1810'",
        "460-0008 - 5000'",
    ]
    merged = merge_material_rows(existing, [])
    assert merged == [
        "470-9997 - 460'",
        "605-3277 (48Ct) - 4700'",
        "605-1502 (144Ct) - 2000'",
        "460-0008 - 5000'",
    ]
    # Re-running the stamped output through the same merge is a no-op.
    assert merge_material_rows(merged, []) == merged
