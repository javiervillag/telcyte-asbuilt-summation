from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.models import CableFootageItem, CableFootageLine
from app.pdf_parser import TextBlock, _clean_text, _is_non_billing_context, field_evidence_blocks
from app.rate_cards import CODE_TEXT_PATTERN, QTY_TEXT_PATTERN, code_key


PART_MAP = {
    ".625": ("coax", ".625", "220-9236"),
    ".875": ("coax", ".875", "220-6999"),
    "48ct": ("fiber", "48Ct", "605-3277"),
    "144ct": ("fiber", "144Ct", "605-1502"),
    "288ct": ("fiber", "288Ct", "605-1503"),
}

TYPE_TEXT = r"(?:\.\s*(?:625|875)|0?(?:48|144|288)\s*(?:ct|count))"
TYPE_PATTERN = re.compile(TYPE_TEXT, re.I)
MATERIALS_TITLE_PATTERN = re.compile(r"^\s*materials?\s*:?\s*$", re.I)
STORAGE_PATTERN = re.compile(
    rf"\b(?P<label>Storage|Tie\s*Point|EOL)\s*-\s*(?P<type>{TYPE_TEXT})\s*-\s*"
    rf"(?P<feet>{QTY_TEXT_PATTERN})\s*(?:'|ft|feet)?\b",
    re.I,
)
DESIGNATION_PATTERN = re.compile(
    rf"\b(?P<label>Storage|Tie\s*Point|Splice|EOL)\s*-\s*(?P<type>{TYPE_TEXT})\b",
    re.I,
)
DIRECT_CODE_PATTERN = re.compile(
    rf"\b({CODE_TEXT_PATTERN})\s*-\s*({QTY_TEXT_PATTERN})(\s*(?:'|sqft))?",
    re.I,
)
MATERIAL_PART_TO_KEY = {part: key for key, (_family, _display, part) in PART_MAP.items() if part}
MATERIAL_PART_TEXT = "|".join(re.escape(part) for part in MATERIAL_PART_TO_KEY) or r"(?!x)x"
MATERIAL_PART_TYPE_ROW_PATTERN = re.compile(
    rf"^\s*(?P<part>{MATERIAL_PART_TEXT})\s*\(\s*(?P<type>{TYPE_TEXT})\s*\)\s*-\s*"
    rf"\d[\d,]*\s*(?:'|ft\b|feet\b)?\s*$",
    re.I,
)
MATERIAL_PART_ONLY_ROW_PATTERN = re.compile(
    rf"^\s*(?P<part>{MATERIAL_PART_TEXT})\s*-\s*\d[\d,]*\s*(?:'|ft\b|feet\b)?\s*$",
    re.I,
)
MATERIAL_BARE_TYPE_ROW_PATTERN = re.compile(
    rf"^\s*(?P<type>{TYPE_TEXT})\s*-\s*\d[\d,]*\s*(?:'|ft\b|feet\b)?\s*$",
    re.I,
)
CABLE_ROW_FOOTAGE_PATTERN = re.compile(
    r"(\d[\d,]*(?:\.\d+)?\s*(?:'|ft\b|feet\b)?)\s*$",
    re.I,
)


@dataclass
class CableFootageResult:
    lines: list[CableFootageLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    informational_notes: list[str] = field(default_factory=list)
    handled_callout_lines: set[str] = field(default_factory=set)


def normalize_cable_type(value: str) -> str | None:
    text = re.sub(r"\s+", "", value.strip().lower())
    coax = re.search(r"\.(625|875)", text)
    if coax:
        return f".{coax.group(1)}"
    fiber = re.search(r"^(0?48|144|288)(?:ct|count)$", text)
    if fiber:
        return f"{int(fiber.group(1))}ct"
    return None


def extract_material_rows(content: str) -> list[str]:
    rows: list[str] = []
    for raw in str(content or "").replace("\r", "\n").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line and not MATERIALS_TITLE_PATTERN.match(line):
            rows.append(line)
    return rows


def cable_material_key(line: str) -> str | None:
    text = re.sub(r"\s+", " ", line or "").strip()

    match = MATERIAL_PART_TYPE_ROW_PATTERN.match(text)
    if match:
        part_key = MATERIAL_PART_TO_KEY.get(match.group("part"))
        type_key = normalize_cable_type(match.group("type"))
        return part_key if part_key and part_key == type_key else None

    match = MATERIAL_PART_ONLY_ROW_PATTERN.match(text)
    if match:
        return MATERIAL_PART_TO_KEY.get(match.group("part"))

    match = MATERIAL_BARE_TYPE_ROW_PATTERN.match(text)
    if match:
        return normalize_cable_type(match.group("type"))
    return None


def canonicalize_cable_material_row(line: str) -> str:
    """Normalize a recognized cable material row while preserving its footage."""
    key = cable_material_key(line)
    if not key:
        return line
    entry = PART_MAP.get(key)
    if not entry:
        return line
    _family, display_type, part_number = entry
    if not part_number:
        return line
    text = re.sub(r"\s+", " ", line or "").strip()
    match = CABLE_ROW_FOOTAGE_PATTERN.search(text)
    if not match:
        return line
    return f"{part_number} ({display_type}) - {match.group(1).strip()}"


def merge_material_rows(existing_rows: list[str], computed_rows: list[str]) -> list[str]:
    computed_by_key: dict[str, str] = {}
    computed_other: list[str] = []

    for row in extract_material_rows("\n".join(computed_rows)):
        key = cable_material_key(row)
        if key:
            computed_by_key[key] = row
        else:
            computed_other.append(row)

    merged: list[str] = []
    seen: set[str] = set()
    used_keys: set[str] = set()

    def add(row: str) -> None:
        clean = re.sub(r"\s+", " ", row).strip()
        normalized = clean.lower()
        if clean and normalized not in seen:
            seen.add(normalized)
            merged.append(clean)

    for row in extract_material_rows("\n".join(existing_rows)):
        key = cable_material_key(row)
        if key and key in computed_by_key:
            add(computed_by_key[key])
            used_keys.add(key)
        elif key:
            add(canonicalize_cable_material_row(row))
        else:
            add(row)

    for key, row in computed_by_key.items():
        if key not in used_keys:
            add(row)

    for row in computed_other:
        add(row)

    return merged


def derive_cable_footage(
    blocks: list[TextBlock],
    *,
    auto_stamp: bool = False,
    path_code: str = "Comp-15",
    coax_rounding_increment: int = 10,
) -> CableFootageResult:
    try:
        return _derive_cable_footage(
            blocks,
            auto_stamp=auto_stamp,
            path_code=path_code,
            coax_rounding_increment=coax_rounding_increment,
        )
    except Exception as exc:  # noqa: BLE001 - cable must never sink billing
        return CableFootageResult(
            informational_notes=[
                f"Cable material check was skipped because the cable parser hit an unexpected error: {exc}."
            ]
        )


def _derive_cable_footage(
    blocks: list[TextBlock],
    *,
    auto_stamp: bool,
    path_code: str,
    coax_rounding_increment: int,
) -> CableFootageResult:
    field_blocks, _skipped_total_boxes, _skipped_material_boxes = field_evidence_blocks(blocks)
    target_code = code_key(path_code)
    if not target_code:
        return CableFootageResult(
            warnings=[f"Cable footage path code is not a supported code: {path_code}."]
        )

    handled_callout_lines: set[str] = set()
    type_evidence: dict[str, set[str]] = defaultdict(set)
    storage_by_type: dict[str, list[CableFootageItem]] = defaultdict(list)
    path_segments: list[CableFootageItem] = []

    for block in field_blocks:
        for raw_line in block.text.splitlines():
            line = _clean_text(raw_line)
            if not line:
                continue
            for match in STORAGE_PATTERN.finditer(line):
                key = normalize_cable_type(match.group("type"))
                if not key:
                    continue
                item = CableFootageItem(
                    label=_clean_label(match.group("label")),
                    page=block.page,
                    feet=_number(match.group("feet")),
                    source=line,
                )
                storage_by_type[key].append(item)
                type_evidence[key].add(line)
                handled_callout_lines.add(line)
            for match in DESIGNATION_PATTERN.finditer(line):
                key = normalize_cable_type(match.group("type"))
                if key:
                    type_evidence[key].add(line)
                    handled_callout_lines.add(line)
            for match in DIRECT_CODE_PATTERN.finditer(line):
                raw_code, raw_qty, _raw_unit = match.groups()
                if code_key(raw_code) != target_code:
                    continue
                if _is_non_billing_context(line, match.start()):
                    continue
                if _is_explicitly_not_pulled(line):
                    handled_callout_lines.add(line)
                    continue
                path_segments.append(
                    CableFootageItem(
                        label=_display_path_code(raw_code),
                        page=block.page,
                        feet=_number(raw_qty),
                        source=line,
                    )
                )

    if not path_segments and not storage_by_type and not type_evidence:
        return CableFootageResult()

    result = CableFootageResult(handled_callout_lines=handled_callout_lines)

    type_keys = sorted(type_evidence or storage_by_type)
    if not type_keys:
        result.warnings.append("Cable path footage was found, but no cable type was clear enough to assign it.")
        return result

    assigned_path_type = type_keys[0] if len(type_keys) == 1 else ""
    if path_segments and not assigned_path_type:
        result.warnings.append(
            "Cable path footage was found with multiple cable types nearby; path ownership needs review."
        )

    for key in type_keys:
        family, display_type, part_number = PART_MAP.get(key, ("", key, ""))
        storage_items = storage_by_type.get(key, [])
        assigned_segments = path_segments if key == assigned_path_type else []
        if not assigned_segments and not storage_items:
            continue
        review_flags: list[str] = []
        if not family:
            review_flags.append(f"No material part mapping found for cable type {display_type}.")
        if not assigned_segments:
            review_flags.append(f"No supported pulled-path footage was found for {display_type}.")
        if path_segments and not assigned_segments:
            review_flags.append("Pulled-path footage could not be safely assigned to this cable type.")
        line = _build_line(
            key,
            display_type,
            family,
            part_number,
            assigned_segments,
            storage_items,
            auto_stamp=auto_stamp,
            coax_rounding_increment=coax_rounding_increment,
            review_flags=review_flags,
        )
        result.lines.append(line)

    for line in result.lines:
        for flag in line.review_flags:
            message = f"Cable material needs review for {line.display_type}: {flag}"
            if message not in result.warnings:
                result.warnings.append(message)
        if line.material_line and not line.eligible_for_stamp and not line.review_flags and not auto_stamp:
            result.informational_notes.append(
                f"Derived cable material line for review only (not stamped): {line.material_line}."
            )
    return result


def _build_line(
    key: str,
    display_type: str,
    family: str,
    part_number: str,
    path_segments: list[CableFootageItem],
    storage_items: list[CableFootageItem],
    *,
    auto_stamp: bool,
    coax_rounding_increment: int,
    review_flags: list[str],
) -> CableFootageLine:
    path_subtotal = sum(item.feet for item in path_segments)
    storage_subtotal = sum(item.feet for item in storage_items)
    rounding = "ceil_100"
    if family == "coax":
        storage_subtotal = 0.0
        rounding = f"ceil_{max(1, int(coax_rounding_increment))}"
        review_flags.append("Coax source path must be validated before automatic stamping.")
    subtotal = path_subtotal + storage_subtotal
    total_ft: int | None = None
    material_line = ""
    if path_subtotal > 0 and part_number and family:
        increment = 100 if family == "fiber" else max(1, int(coax_rounding_increment))
        total_ft = _ceil_to_increment(subtotal * 1.10, increment)
        material_line = f"{part_number} ({display_type}) - {total_ft}'"
    source_pages = sorted({item.page for item in [*path_segments, *storage_items] if item.page})
    return CableFootageLine(
        callout=key,
        display_type=display_type,
        part_number=part_number,
        family=family or "unknown",
        path_segments=path_segments,
        storage_items=storage_items if family != "coax" else [],
        path_subtotal=path_subtotal,
        storage_subtotal=storage_subtotal,
        buffer=1.10,
        rounding=rounding,
        total_ft=total_ft,
        material_line=material_line,
        eligible_for_stamp=bool(auto_stamp and material_line and not review_flags),
        source_pages=source_pages,
        confidence=0.92 if material_line and not review_flags else 0.55,
        review_flags=review_flags,
    )


def _clean_label(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).title().replace("Eol", "EOL")


def _number(value: str) -> float:
    return float(value.replace(",", ""))


def _ceil_to_increment(value: float, increment: int) -> int:
    increment = max(1, int(increment))
    return int(math.ceil(value / increment) * increment)


def _is_explicitly_not_pulled(line: str) -> bool:
    text = line.lower()
    return "not counted" in text or "not pulled" in text or "not be pulled" in text


def _display_path_code(raw_code: str) -> str:
    compact = re.sub(r"\s+", "", raw_code.strip())
    match = re.match(r"([A-Za-z]+)-?(\d{1,3})", compact)
    if not match:
        return compact
    prefix, number = match.groups()
    return f"{prefix.title()}-{number}"
