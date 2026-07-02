from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Literal

from app.pdf_parser import TextBlock, _clean_text, derive_code_total_map, field_evidence_blocks
from app.rate_cards import CodeKey


TriggerKind = Literal["material_label", "billing_code_total"]


@dataclass(frozen=True)
class MaterialRule:
    rule_id: str
    part_number: str
    display: str
    unit: Literal["ft", "ea"]
    trigger_kind: TriggerKind
    buffer: float = 1.0
    rounding_increment: int = 1
    trigger_patterns: tuple[re.Pattern[str], ...] = ()
    trigger_codes: tuple[CodeKey, ...] = ()


@dataclass
class DerivedMaterialLine:
    rule_id: str
    part_number: str
    display: str
    source_quantity: float
    total_quantity: int
    unit: str
    material_line: str
    source_lines: list[str] = field(default_factory=list)


@dataclass
class AdditionalMaterialResult:
    lines: list[DerivedMaterialLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    informational_notes: list[str] = field(default_factory=list)
    handled_callout_lines: set[str] = field(default_factory=set)

    @property
    def material_rows(self) -> list[str]:
        return [line.material_line for line in self.lines if line.material_line]


_FOOTAGE_QTY = r"(?P<qty>[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)"
_FOOTAGE_UNIT = r"(?:'|ft\.?\b|feet\b)"
_SEPARATOR = r"\s*[-:]\s*"
_MATERIAL_LIKE_CUE = re.compile(
    rf"\b(?:Drop\s+F|RG11|RG6)\b.*(?:{_FOOTAGE_UNIT}|{_FOOTAGE_QTY})|"
    rf"\b(?:EOL|Storage|Tie\s*Point|Splice)\s*-\s*(?:Drop\s+F|RG11|RG6)\b",
    re.I,
)

ADDITIONAL_MATERIAL_RULES: tuple[MaterialRule, ...] = (
    MaterialRule(
        rule_id="drop_f",
        part_number="240-0318",
        display="Drop F",
        unit="ft",
        trigger_kind="material_label",
        buffer=1.10,
        trigger_patterns=(
            re.compile(rf"^\s*Drop\s+F\b{_SEPARATOR}{_FOOTAGE_QTY}\s*{_FOOTAGE_UNIT}", re.I),
        ),
    ),
    MaterialRule(
        rule_id="rg11",
        part_number="240-2083",
        display="RG11",
        unit="ft",
        trigger_kind="material_label",
        buffer=1.10,
        trigger_patterns=(
            re.compile(rf"^\s*RG11\b{_SEPARATOR}{_FOOTAGE_QTY}\s*{_FOOTAGE_UNIT}", re.I),
        ),
    ),
    MaterialRule(
        rule_id="rg6",
        part_number="240-2079",
        display="RG6",
        unit="ft",
        trigger_kind="material_label",
        buffer=1.10,
        trigger_patterns=(
            re.compile(rf"^\s*RG6\b{_SEPARATOR}{_FOOTAGE_QTY}\s*{_FOOTAGE_UNIT}", re.I),
        ),
    ),
    MaterialRule(
        rule_id="innerduct_cd_mdu",
        part_number="470-0349",
        display="CD-02/MDU-11",
        unit="ft",
        trigger_kind="billing_code_total",
        buffer=1.10,
        trigger_codes=(("CD", "2"), ("MDU", "11")),
    ),
    MaterialRule(
        rule_id="ug28",
        part_number="450-0323",
        display="UG-28",
        unit="ea",
        trigger_kind="billing_code_total",
        trigger_codes=(("UG", "28"),),
    ),
    MaterialRule(
        rule_id="smc07",
        part_number="470-0135",
        display="SMC-07",
        unit="ea",
        trigger_kind="billing_code_total",
        trigger_codes=(("SMC", "7"),),
    ),
)


ADDITIONAL_MATERIAL_PARTS = frozenset(rule.part_number for rule in ADDITIONAL_MATERIAL_RULES)


def derive_additional_materials(
    blocks: list[TextBlock],
    *,
    code_totals_by_key: dict[CodeKey, float] | None = None,
) -> AdditionalMaterialResult:
    try:
        return _derive_additional_materials(
            blocks,
            code_totals_by_key=code_totals_by_key,
        )
    except Exception as exc:  # noqa: BLE001 - material derivation must never sink billing
        return AdditionalMaterialResult(
            informational_notes=[
                f"Additional material check was skipped because the material parser hit an unexpected error: {exc}."
            ]
        )


def _derive_additional_materials(
    blocks: list[TextBlock],
    *,
    code_totals_by_key: dict[CodeKey, float] | None,
) -> AdditionalMaterialResult:
    field_blocks, _skipped_total_boxes, _skipped_material_boxes = field_evidence_blocks(blocks)
    totals_by_key = (
        code_totals_by_key
        if code_totals_by_key is not None
        else derive_code_total_map(blocks, apply_catalog=False)
    )
    result = AdditionalMaterialResult()

    for rule in ADDITIONAL_MATERIAL_RULES:
        if rule.trigger_kind == "material_label":
            line = _derive_label_material(rule, field_blocks)
        else:
            line = _derive_code_material(rule, totals_by_key)
        if line:
            result.lines.append(line)
            result.handled_callout_lines.update(line.source_lines)
        elif rule.trigger_kind == "material_label":
            _record_unparsed_label_warning(result, rule, field_blocks)

    return result


def _derive_label_material(rule: MaterialRule, blocks: list[TextBlock]) -> DerivedMaterialLine | None:
    source_quantity = 0.0
    source_lines: list[str] = []
    seen_lines: set[str] = set()

    for block in blocks:
        for raw_line in block.text.splitlines():
            line = _clean_text(raw_line)
            if not line:
                continue
            for pattern in rule.trigger_patterns:
                for match in pattern.finditer(line):
                    source_quantity += _number(match.group("qty"))
                    if line not in seen_lines:
                        seen_lines.add(line)
                        source_lines.append(line)

    if source_quantity <= 0:
        return None
    return _material_line(rule, source_quantity, source_lines)


def _derive_code_material(
    rule: MaterialRule,
    totals_by_key: dict[CodeKey, float],
) -> DerivedMaterialLine | None:
    source_quantity = sum(totals_by_key.get(key, 0.0) for key in rule.trigger_codes)
    if source_quantity <= 0:
        return None
    source_lines = [f"{_display_code(key)} - {_format_source_quantity(totals_by_key[key])}" for key in rule.trigger_codes if key in totals_by_key]
    return _material_line(rule, source_quantity, source_lines)


def _record_unparsed_label_warning(
    result: AdditionalMaterialResult,
    rule: MaterialRule,
    blocks: list[TextBlock],
) -> None:
    possible_lines: list[str] = []
    for block in blocks:
        for raw_line in block.text.splitlines():
            line = _clean_text(raw_line)
            if not line or rule.display.lower() not in re.sub(r"\s+", " ", line).lower():
                continue
            if rule.rule_id == "drop_f" and _is_prefixed_drop_f_callout(line):
                continue
            if _MATERIAL_LIKE_CUE.search(line) and line not in possible_lines:
                possible_lines.append(line)
    if not possible_lines:
        return
    preview = "; ".join(possible_lines[:3])
    if len(possible_lines) > 3:
        preview += f"; plus {len(possible_lines) - 3} more"
    warning = (
        f"Possible {rule.display} material callout found, but it was not in a direct "
        f"'{rule.display} - footage' row: {preview}. Verify the Materials box manually."
    )
    if warning not in result.warnings:
        result.warnings.append(warning)


def _is_prefixed_drop_f_callout(line: str) -> bool:
    return bool(re.search(r"\b(?:EOL|Storage|Tie\s*Point|Splice)\s*-\s*Drop\s+F\b", line, re.I))


def _material_line(rule: MaterialRule, source_quantity: float, source_lines: list[str]) -> DerivedMaterialLine:
    total_quantity = _material_quantity(source_quantity, rule)
    line = f"{rule.part_number} ({rule.display}) - {total_quantity}"
    if rule.unit == "ft":
        line += "'"
    return DerivedMaterialLine(
        rule_id=rule.rule_id,
        part_number=rule.part_number,
        display=rule.display,
        source_quantity=source_quantity,
        total_quantity=total_quantity,
        unit=rule.unit,
        material_line=line,
        source_lines=source_lines,
    )


def _material_quantity(source_quantity: float, rule: MaterialRule) -> int:
    if rule.unit == "ea":
        return int(math.ceil(source_quantity))
    return round_half_up_to_increment(source_quantity * rule.buffer, rule.rounding_increment)


def round_half_up_to_increment(value: float, increment: int = 1) -> int:
    increment = max(1, int(increment))
    return int(math.floor((value / increment) + 0.5 + 1e-9) * increment)


def _number(value: str) -> float:
    return float(value.replace(",", ""))


def _display_code(key: CodeKey) -> str:
    prefix, number = key
    return f"{prefix}-{int(number):02d}"


def _format_source_quantity(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"
