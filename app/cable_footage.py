from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.additional_materials import ADDITIONAL_MATERIAL_PARTS
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
# Cox renumbered cable parts in MCA Rate Card Amendment 3 (~2026-06-09); field as-builts
# still print the OLD part number. Map each legacy part to its current cable type so
# canonicalize_cable_material_row re-emits the CURRENT PART_MAP number. Extend as Nick
# confirms more old->new pairs (source: Nick email, BI-872022).
LEGACY_PART_TO_KEY = {
    "605-3324": "144ct",  # old Cox 144Ct fiber part -> current 605-1502
}
MATERIAL_PART_TO_KEY = {
    **{part: key for key, (_family, _display, part) in PART_MAP.items() if part},
    **LEGACY_PART_TO_KEY,
}
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
ADDITIONAL_MATERIAL_PART_TEXT = "|".join(re.escape(part) for part in sorted(ADDITIONAL_MATERIAL_PARTS)) or r"(?!x)x"
ADDITIONAL_MATERIAL_ROW_PATTERN = re.compile(
    rf"^\s*(?P<part>{ADDITIONAL_MATERIAL_PART_TEXT})\s*(?:\([^)]*\))?\s*-\s*"
    rf"\d[\d,]*(?:\.\d+)?\s*(?:'|ft\b|feet\b|ea\b)?\s*$",
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


def additional_material_key(line: str) -> str | None:
    text = re.sub(r"\s+", " ", line or "").strip()
    match = ADDITIONAL_MATERIAL_ROW_PATTERN.match(text)
    if not match:
        return None
    return f"part:{match.group('part')}"


def material_row_key(line: str) -> str | None:
    cable_key = cable_material_key(line)
    if cable_key:
        return f"cable:{cable_key}"
    return additional_material_key(line)


def canonicalize_cable_material_row(line: str, *, apply_buffer: bool = False) -> str:
    """Normalize a recognized cable material row to the CURRENT part number + cable-type
    label (e.g. legacy "605-3324 - 1810'" -> "605-1502 (144Ct) - 1810'").

    With apply_buffer=True (used when building the stamped Materials box) the fiber
    footage is also turned into an order quantity: +10% buffer rounded UP to the next
    100' (Nick, BI-872022). Idempotent - a row already carrying a (NNCt) label whose
    footage is already a multiple of 100' is treated as a prior tool output and left as
    is, so re-running an output never re-buffers. Coax is relabeled but never
    auto-buffered (its source path still needs validation; see _build_line).
    """
    key = cable_material_key(line)
    if not key:
        return line
    entry = PART_MAP.get(key)
    if not entry:
        return line
    family, display_type, part_number = entry
    if not part_number:
        return line
    text = re.sub(r"\s+", " ", line or "").strip()
    match = CABLE_ROW_FOOTAGE_PATTERN.search(text)
    if not match:
        return line
    footage_text = match.group(1).strip()
    feet = _footage_feet(footage_text)
    if apply_buffer and family == "fiber" and feet is not None:
        # Deliberate tradeoff: a row that already carries a (NNCt) label AND whose
        # footage is a round multiple of 100' is assumed to be a prior tool output
        # and is NOT re-buffered, so re-running an output is idempotent (a hard
        # invariant - re-buffering would inflate footage on every pass). The known
        # cost: a RAW source callout that happens to mimic our canonical output
        # exactly (e.g. "605-1502 (144Ct) - 2000'") is left unbuffered. That format
        # is what THIS tool emits, not how field/Cox callouts are printed (those
        # carry the legacy bare part or an unrounded measurement), so the collision
        # is unlikely; idempotency is the safer default. Pinned by
        # test_canonicalize_buffer_does_not_rebuffer_canonical_round_label.
        already_buffered = (
            MATERIAL_PART_TYPE_ROW_PATTERN.match(text) is not None
            and feet % FIBER_ROUNDING_INCREMENT == 0
        )
        if not already_buffered:
            return f"{part_number} ({display_type}) - {buffered_cable_footage(feet, family)}'"
    return f"{part_number} ({display_type}) - {footage_text}"


def merge_material_rows(existing_rows: list[str], computed_rows: list[str]) -> list[str]:
    computed_by_key: dict[str, str] = {}
    computed_other: list[str] = []

    for row in extract_material_rows("\n".join(computed_rows)):
        key = material_row_key(row)
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
        key = material_row_key(row)
        if key and key in computed_by_key:
            add(computed_by_key[key])
            used_keys.add(key)
        elif cable_material_key(row):
            add(canonicalize_cable_material_row(row, apply_buffer=True))
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
        total_ft = buffered_cable_footage(subtotal, family, coax_rounding_increment)
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
        buffer=CABLE_BUFFER,
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


CABLE_BUFFER = 1.10
FIBER_ROUNDING_INCREMENT = 100


def buffered_cable_footage(feet: float, family: str, coax_increment: int = 10) -> int:
    """Cable order quantity = field footage + 10% buffer, rounded UP to the family's
    increment (fiber -> next 100'; coax -> COAX_ROUNDING_INCREMENT). Single source of
    truth for the buffer rule, shared by the cable-footage derive path (_build_line) and
    the Materials-box canonicalizer (Nick, BI-872022)."""
    increment = FIBER_ROUNDING_INCREMENT if family == "fiber" else max(1, int(coax_increment))
    return _ceil_to_increment(feet * CABLE_BUFFER, increment)


def _footage_feet(text: str) -> float | None:
    """Leading footage number from a possibly unit-suffixed string ("1,810'" -> 1810.0)."""
    match = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
    return float(match.group(0).replace(",", "")) if match else None


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
