from app.cable_footage import (
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
        "605-1502 (144Ct) - 1192'",
        "LockBox - 1",
        "EMT - 10'",
        'EMT 2" Fitting - 2',
        "470-9997 - 500'",
        "605-3277 (48Ct) - 603'",
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
