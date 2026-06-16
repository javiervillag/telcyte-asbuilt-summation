from app.cable_footage import derive_cable_footage, normalize_cable_type
from app.pdf_parser import TextBlock, _unresolved_callout_lines, derive_code_totals


def _block(text: str, *, page: int = 1, source: str = "annotation") -> TextBlock:
    return TextBlock(page=page, bbox=(0.0, 0.0, 240.0, 80.0), text=text, source=source)


def test_normalize_cable_type_variants() -> None:
    assert normalize_cable_type("48Ct") == "48ct"
    assert normalize_cable_type("048 count") == "48ct"
    assert normalize_cable_type(".625") == ".625"
    assert normalize_cable_type(".875") == ".875"
    assert normalize_cable_type("PWR-625") is None


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
    assert any("Materials" in note for note in result.informational_notes)


def test_resolved_cable_callouts_do_not_stay_unresolved() -> None:
    blocks = [_block("Storage - 48Ct - 100'\nTie Point - 48Ct - 100'\nEOL - 48Ct - 30'\nComp-15 - 290'")]
    result = derive_cable_footage(blocks)

    unresolved = _unresolved_callout_lines(blocks, resolved_callout_lines=result.handled_callout_lines)

    assert unresolved == []
