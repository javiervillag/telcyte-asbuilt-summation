from __future__ import annotations

from app.additional_materials import derive_additional_materials
from app.cable_footage import merge_material_rows
from app.pdf_parser import TextBlock, derive_code_total_map, derive_code_totals


def _block(text: str, *, page: int = 1, source: str = "page") -> TextBlock:
    return TextBlock(page=page, bbox=(0.0, 0.0, 260.0, 120.0), text=text, source=source)


def test_drop_cable_types_buffer_without_100_rounding() -> None:
    result = derive_additional_materials(
        [
            _block("Drop F - 105'\nRG11 - 200 ft\nRG6 - 50'"),
        ]
    )

    assert result.material_rows == [
        "240-0318 (Drop F) - 116'",
        "240-2083 (RG11) - 220'",
        "240-2079 (RG6) - 55'",
    ]


def test_drop_f_rounds_to_nearest_whole_foot_not_ceiling() -> None:
    result = derive_additional_materials([_block("Drop F - 324'")])

    assert result.material_rows == ["240-0318 (Drop F) - 356'"]


def test_drop_cable_detection_ignores_rg6_prose_without_footage() -> None:
    result = derive_additional_materials(
        [
            _block("DP-07 - 1\nInstall Post-Wire RG6 Siamese Fiber drop"),
            _block("General note: RG11 material may be needed by customer."),
        ]
    )

    assert result.material_rows == []


def test_fiber_style_drop_f_callouts_are_left_to_cable_marker_logic() -> None:
    result = derive_additional_materials(
        [
            _block("EOL - Drop F - 40'\nStorage - Drop F - 4'\nTie Point - Drop F - 40'"),
        ]
    )

    assert result.material_rows == []
    assert result.warnings == []


def test_direct_drop_cable_does_not_depend_on_comp15_path_subtotal() -> None:
    result = derive_additional_materials([_block("RG11 - 200'")])

    assert result.material_rows == ["240-2083 (RG11) - 220'"]


def test_misc_material_code_rules_from_de_duplicated_totals() -> None:
    result = derive_additional_materials(
        [
            _block("CD-02 - 40'\nMDU-11 - 60'\nUG-28 - 2\nSMC-07 - 3"),
        ]
    )

    assert result.material_rows == [
        "470-0349 (CD-02/MDU-11) - 110'",
        "450-0323 (UG-28) - 2",
        "470-0135 (SMC-07) - 3",
    ]
    smc = next(line for line in result.lines if line.rule_id == "smc07")
    assert smc.rule == "count each"
    assert smc.source_quantity == 3
    assert smc.source_lines == ["SMC-07 - 3"]


def test_material_code_rules_are_not_dropped_by_catalog_filtering() -> None:
    blocks = [_block("CD-02 - 40'\nMDU-11 - 60'\nUG-28 - 2\nSMC-07 - 3")]

    visible_totals = derive_code_totals(blocks, code_catalog={("UG", "6"): "UG-06"})
    unfiltered_totals = derive_code_total_map(blocks, code_catalog={("UG", "6"): "UG-06"}, apply_catalog=False)
    result = derive_additional_materials(blocks, code_totals_by_key=unfiltered_totals)

    assert visible_totals == []
    assert result.material_rows == [
        "470-0349 (CD-02/MDU-11) - 110'",
        "450-0323 (UG-28) - 2",
        "470-0135 (SMC-07) - 3",
    ]


def test_stamped_no_100_round_rows_are_replaced_not_rebuffered() -> None:
    existing = [
        "240-0318 (Drop F) - 116'",
        "Manual Material - 1",
    ]
    computed = ["240-0318 (Drop F) - 128'"]

    assert merge_material_rows(existing, []) == existing
    assert merge_material_rows(existing, computed) == [
        "240-0318 (Drop F) - 128'",
        "Manual Material - 1",
    ]


def test_existing_innerduct_alias_is_replaced_by_part_number_row() -> None:
    assert merge_material_rows(["Innerduct - 160'"], ["470-0349 (CD-02/MDU-11) - 165'"]) == [
        "470-0349 (CD-02/MDU-11) - 165'",
    ]


def test_cd02_and_mdu11_share_one_material_row() -> None:
    result = derive_additional_materials([_block("CD-02 - 10'\nMDU-11 - 10'")])

    assert result.material_rows == ["470-0349 (CD-02/MDU-11) - 22'"]
