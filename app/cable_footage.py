from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.additional_materials import ADDITIONAL_MATERIAL_PARTS, round_half_up_to_increment
from app.models import CableFootageItem, CableFootageLine, SummaryIssue
from app.pdf_parser import (
    TextBlock,
    _clean_text,
    _is_non_billing_context,
    derive_code_total_map,
    field_evidence_blocks,
)
from app.rate_cards import CODE_TEXT_PATTERN, QTY_TEXT_PATTERN, CodeKey, code_key


PART_MAP = {
    ".625": ("coax", ".625", "220-9236"),
    ".875": ("coax", ".875", "220-6999"),
    "48ct": ("fiber", "48Ct", "605-3277"),
    "144ct": ("fiber", "144Ct", "605-1502"),
    "288ct": ("fiber", "288Ct", "605-1503"),
    "drop_f": ("drop_fiber", "Drop F", "240-0318"),
}

TYPE_TEXT = r"(?:Drop\s+F|\.\s*(?:625|875)|0?(?:48|144|288)\s*(?:ct|count))"
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
MARKER_PAIR_PATTERN = re.compile(r"\b(?P<a>[DT]\d{4,6})\s*-\s*(?P<b>[DT]\d{4,6})\b", re.I)
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
MATERIAL_ROW_QTY_TEXT = r"(?:\d[\d,]*(?:\.\d+)?|VERIFY)"
MATERIAL_PART_TYPE_ROW_PATTERN = re.compile(
    rf"^\s*(?P<part>{MATERIAL_PART_TEXT})\s*\(\s*(?P<type>{TYPE_TEXT})\s*\)\s*-\s*"
    rf"{MATERIAL_ROW_QTY_TEXT}\s*(?:'|ft\b|feet\b)?\s*$",
    re.I,
)
MATERIAL_PART_DESCRIPTOR_ROW_PATTERN = re.compile(
    rf"^\s*(?P<part>{MATERIAL_PART_TEXT})\s*-\s*(?P<type>{TYPE_TEXT})\s*"
    rf"(?:fiber|cable)?\s*-\s*{MATERIAL_ROW_QTY_TEXT}\s*(?:'|ft\b|feet\b)?\s*$",
    re.I,
)
MATERIAL_PART_ONLY_ROW_PATTERN = re.compile(
    rf"^\s*(?P<part>{MATERIAL_PART_TEXT})\s*-\s*{MATERIAL_ROW_QTY_TEXT}\s*(?:'|ft\b|feet\b)?\s*$",
    re.I,
)
MATERIAL_BARE_TYPE_ROW_PATTERN = re.compile(
    rf"^\s*(?P<type>{TYPE_TEXT})\s*-\s*{MATERIAL_ROW_QTY_TEXT}\s*(?:'|ft\b|feet\b)?\s*$",
    re.I,
)
CABLE_ROW_FOOTAGE_PATTERN = re.compile(
    r"(\d[\d,]*(?:\.\d+)?\s*(?:'|ft\b|feet\b)?)\s*$",
    re.I,
)
ADDITIONAL_MATERIAL_PART_TEXT = "|".join(re.escape(part) for part in sorted(ADDITIONAL_MATERIAL_PARTS)) or r"(?!x)x"
ADDITIONAL_MATERIAL_ROW_PATTERN = re.compile(
    rf"^\s*(?P<part>{ADDITIONAL_MATERIAL_PART_TEXT})\s*(?:\([^)]*\))?\s*-\s*"
    rf"{MATERIAL_ROW_QTY_TEXT}\s*(?:'|ft\b|feet\b|ea\b)?\s*$",
    re.I,
)
ADDITIONAL_MATERIAL_ALIAS_ROW_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^\s*innerduct\s*-\s*\d[\d,]*(?:\.\d+)?\s*(?:'|ft\b|feet\b)?\s*$", re.I),
        "part:470-0349",
    ),
)


@dataclass
class CableFootageResult:
    lines: list[CableFootageLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    informational_notes: list[str] = field(default_factory=list)
    issues: list[SummaryIssue] = field(default_factory=list)
    handled_callout_lines: set[str] = field(default_factory=set)


def normalize_cable_type(value: str) -> str | None:
    text = re.sub(r"\s+", "", value.strip().lower())
    coax = re.search(r"\.(625|875)", text)
    if coax:
        return f".{coax.group(1)}"
    fiber = re.search(r"^(0?48|144|288)(?:ct|count)$", text)
    if fiber:
        return f"{int(fiber.group(1))}ct"
    if text in {"dropf", "dropfiber"}:
        return "drop_f"
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

    match = MATERIAL_PART_DESCRIPTOR_ROW_PATTERN.match(text)
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
    if match:
        return f"part:{match.group('part')}"
    for pattern, key in ADDITIONAL_MATERIAL_ALIAS_ROW_PATTERNS:
        if pattern.match(text):
            return key
    return None


def material_row_key(line: str) -> str | None:
    cable_key = cable_material_key(line)
    if cable_key:
        entry = PART_MAP.get(cable_key)
        if entry and entry[2] in ADDITIONAL_MATERIAL_PARTS:
            return f"part:{entry[2]}"
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
        already_buffered = _is_labeled_final_fiber_material_row(text) and feet % FIBER_ROUNDING_INCREMENT == 0
        if not already_buffered:
            return f"{part_number} ({display_type}) - {buffered_cable_footage(feet, family)}'"
    return f"{part_number} ({display_type}) - {footage_text}"


def merge_material_rows(existing_rows: list[str], computed_rows: list[str]) -> list[str]:
    computed_by_key: dict[str, str] = {}
    computed_other: list[str] = []

    for row in extract_material_rows("\n".join(computed_rows)):
        key = material_row_key(row)
        if key:
            existing = computed_by_key.get(key)
            if existing is None or (_is_verify_material_row(existing) and not _is_verify_material_row(row)):
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
            computed = computed_by_key[key]
            if _is_verify_material_row(computed):
                cable_key = cable_material_key(row)
                add(canonicalize_cable_material_row(row, apply_buffer=True) if _should_canonicalize_cable_key(cable_key) else row)
            else:
                add(computed)
            used_keys.add(key)
        elif _should_canonicalize_cable_key(cable_material_key(row)):
            add(canonicalize_cable_material_row(row, apply_buffer=True))
        else:
            add(row)

    for key, row in computed_by_key.items():
        if key not in used_keys:
            add(row)

    for row in computed_other:
        add(row)

    return merged


def _is_verify_material_row(line: str) -> bool:
    return bool(re.search(r"\s-\s*VERIFY\s*(?:'|ft\b|feet\b|ea\b)?\s*$", line or "", re.I))


def _should_canonicalize_cable_key(key: str | None) -> bool:
    if not key:
        return False
    entry = PART_MAP.get(key)
    return bool(entry and entry[2] not in ADDITIONAL_MATERIAL_PARTS)


def _is_labeled_final_fiber_material_row(text: str) -> bool:
    return (
        MATERIAL_PART_TYPE_ROW_PATTERN.match(text) is not None
        or MATERIAL_PART_DESCRIPTOR_ROW_PATTERN.match(text) is not None
    )


def derive_cable_footage(
    blocks: list[TextBlock],
    *,
    auto_stamp: bool = False,
    path_code: str = "Comp-15",
    fallback_path_codes: list[str] | tuple[str, ...] = (),
    coax_rounding_increment: int = 10,
) -> CableFootageResult:
    try:
        return _derive_cable_footage(
            blocks,
            auto_stamp=auto_stamp,
            path_code=path_code,
            fallback_path_codes=fallback_path_codes,
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
    fallback_path_codes: list[str] | tuple[str, ...],
    coax_rounding_increment: int,
) -> CableFootageResult:
    field_blocks, _skipped_total_boxes, _skipped_material_boxes = field_evidence_blocks(blocks)
    target_code = code_key(path_code)
    if not target_code:
        message = f"Cable footage path code is not a supported code: {path_code}."
        return CableFootageResult(
            warnings=[message],
            issues=[SummaryIssue(severity="action", code="invalid_cable_path_code", message=message)],
        )

    handled_callout_lines: set[str] = set()
    type_evidence: dict[str, set[str]] = defaultdict(set)
    storage_by_type: dict[str, list[CableFootageItem]] = defaultdict(list)
    marker_evidence_by_type: dict[str, list[tuple[CableFootageItem, str]]] = defaultdict(list)
    marker_warnings_by_type: dict[str, list[str]] = defaultdict(list)
    path_segments: list[CableFootageItem] = []
    primary_path_segments: list[CableFootageItem] = []
    path_includes_storage = True

    for block in field_blocks:
        block_lines = [_clean_text(raw_line) for raw_line in block.text.splitlines()]
        for index, line in enumerate(block_lines):
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
                marker_line = block_lines[index + 1] if index + 1 < len(block_lines) else ""
                if marker_line:
                    marker = MARKER_PAIR_PATTERN.search(marker_line)
                    if marker:
                        marker_evidence_by_type[key].append((item, marker.group(0)))
                        handled_callout_lines.add(marker_line)
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

    primary_path_segments = list(path_segments)
    fallback_warnings: list[str] = []
    using_fallback_path = False
    if not primary_path_segments and fallback_path_codes:
        path_segments, fallback_warnings = _fallback_path_segments_from_code_totals(
            blocks,
            field_blocks,
            fallback_path_codes,
        )
        path_includes_storage = False
        using_fallback_path = bool(path_segments)

    marker_segments_by_type: dict[str, CableFootageItem] = {}
    for key, evidence in marker_evidence_by_type.items():
        if key != "drop_f":
            continue
        marker_segment, marker_warnings = _station_marker_path_segment(key, evidence)
        marker_warnings_by_type[key].extend(marker_warnings)
        if marker_segment:
            marker_segments_by_type[key] = marker_segment

    if not path_segments and not storage_by_type and not type_evidence:
        return CableFootageResult()

    result = CableFootageResult(handled_callout_lines=handled_callout_lines)
    result.warnings.extend(fallback_warnings)
    result.issues.extend(
        SummaryIssue(severity="action", code="invalid_fallback_path_code", message=warning)
        for warning in fallback_warnings
    )

    type_keys = sorted(type_evidence or storage_by_type)
    if not type_keys:
        message = "Cable path footage was found, but no cable type was clear enough to assign it."
        result.warnings.append(message)
        result.issues.append(
            SummaryIssue(severity="action", code="cable_type_unclear", message=message)
        )
        return result

    assigned_path_type = type_keys[0] if len(type_keys) == 1 else ""
    if path_segments and not assigned_path_type:
        message = "Cable path footage was found with multiple cable types nearby; path ownership needs review."
        result.warnings.append(message)
        result.issues.append(
            SummaryIssue(severity="notice", code="cable_path_ownership", message=message)
        )

    for key in type_keys:
        family, display_type, part_number = PART_MAP.get(key, ("", key, ""))
        storage_items = storage_by_type.get(key, [])
        assigned_segments = path_segments if key == assigned_path_type else []
        if key in marker_segments_by_type:
            assigned_segments = [marker_segments_by_type[key]]
            storage_items = []
        if not assigned_segments and not storage_items:
            continue
        review_flags: list[str] = []
        review_flags.extend(marker_warnings_by_type.get(key, []))
        if using_fallback_path and family != "fiber" and key not in marker_segments_by_type:
            assigned_segments = []
            review_flags.append("Fallback UG/DP pull-code footage is only validated for fiber cable types.")
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
            include_storage_in_total=path_includes_storage,
            review_flags=review_flags,
        )
        result.lines.append(line)

    for line in result.lines:
        for flag in line.review_flags:
            message = f"Cable material needs review for {line.display_type}: {flag}"
            if message not in result.warnings:
                result.warnings.append(message)
                result.issues.append(
                    SummaryIssue(
                        severity="action",
                        code="cable_material_review",
                        message=message,
                        subject=line.callout,
                    )
                )
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
    include_storage_in_total: bool = True,
    review_flags: list[str],
) -> CableFootageLine:
    path_subtotal = sum(item.feet for item in path_segments)
    storage_subtotal = sum(item.feet for item in storage_items)
    rounding = "ceil_100"
    if family == "coax":
        storage_subtotal = 0.0
        rounding = f"ceil_{max(1, int(coax_rounding_increment))}"
        review_flags.append("Coax source path must be validated before automatic stamping.")
    subtotal = path_subtotal + (storage_subtotal if include_storage_in_total else 0.0)
    total_ft: int | None = None
    material_line = ""
    review_material_line = ""
    if path_subtotal > 0 and part_number and family:
        total_ft = buffered_cable_footage(subtotal, family, coax_rounding_increment)
        material_line = f"{part_number} ({display_type}) - {total_ft}'"
    elif part_number and family in {"fiber", "drop_fiber"} and storage_subtotal > 0:
        review_material_line = f"{part_number} ({display_type}) - VERIFY"
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
        review_material_line=review_material_line,
        eligible_for_stamp=bool(auto_stamp and material_line and not review_flags),
        source_pages=source_pages,
        confidence=0.92 if material_line and not review_flags else 0.55,
        review_flags=review_flags,
    )


def _fallback_path_segments_from_code_totals(
    blocks: list[TextBlock],
    field_blocks: list[TextBlock],
    fallback_path_codes: list[str] | tuple[str, ...],
) -> tuple[list[CableFootageItem], list[str]]:
    code_labels_by_key: dict[CodeKey, str] = {}
    warnings: list[str] = []
    for raw_code in fallback_path_codes:
        key = code_key(raw_code)
        if not key:
            clean = re.sub(r"\s+", " ", raw_code or "").strip()
            if clean:
                warnings.append(f"Cable fallback path code is not a supported code: {clean}.")
            continue
        code_labels_by_key.setdefault(key, _display_path_code(raw_code))
    if not code_labels_by_key:
        return [], warnings

    totals = derive_code_total_map(blocks, apply_catalog=False)
    pages_by_key = _first_pages_for_codes(field_blocks, set(code_labels_by_key))
    segments: list[CableFootageItem] = []
    for key, label in code_labels_by_key.items():
        feet = totals.get(key, 0.0)
        if feet <= 0:
            continue
        segments.append(
            CableFootageItem(
                label=label,
                page=pages_by_key.get(key, 0),
                feet=feet,
                source=f"{label} - {feet:g}' (billing total)",
            )
        )
    return segments, warnings


def _first_pages_for_codes(
    field_blocks: list[TextBlock],
    target_keys: set[CodeKey],
) -> dict[CodeKey, int]:
    pages: dict[CodeKey, int] = {}
    for block in field_blocks:
        for line in block.text.splitlines():
            for match in DIRECT_CODE_PATTERN.finditer(line):
                key = code_key(match.group(1))
                if key in target_keys and key not in pages:
                    pages[key] = block.page
    return pages


def _clean_label(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).title().replace("Eol", "EOL")


def _number(value: str) -> float:
    return float(value.replace(",", ""))


def _station_marker_path_segment(
    key: str,
    evidence: list[tuple[CableFootageItem, str]],
) -> tuple[CableFootageItem | None, list[str]]:
    d_values: list[int] = []
    terminal_slack = 0.0
    warnings: list[str] = []
    sources: list[str] = []
    pages: list[int] = []
    for item, marker_text in evidence:
        marker = MARKER_PAIR_PATTERN.search(marker_text)
        if not marker:
            continue
        a_type, a_value = _station_marker_parts(marker.group("a"))
        b_type, b_value = _station_marker_parts(marker.group("b"))
        if a_type == "D":
            d_values.append(a_value)
        if b_type == "D":
            d_values.append(b_value)
        if {a_type, b_type} == {"D", "T"}:
            diff = abs(a_value - b_value)
            if abs(diff - item.feet) > 0.51:
                warnings.append(
                    f"Station marker distance for {item.source} ({marker_text}) does not match the labeled footage."
                )
            terminal_slack += diff
        sources.append(f"{item.source} / {marker_text}")
        if item.page:
            pages.append(item.page)
    if warnings:
        return None, warnings
    if len(d_values) < 2:
        return None, [f"Station markers for {key} did not include enough design markers to calculate cable footage."]
    feet = (max(d_values) - min(d_values)) + terminal_slack
    if feet <= 0:
        return None, [f"Station markers for {key} did not produce a positive cable footage."]
    return (
        CableFootageItem(
            label="Station markers",
            page=min(pages) if pages else 0,
            feet=feet,
            source="; ".join(sources),
        ),
        [],
    )


def _station_marker_parts(value: str) -> tuple[str, int]:
    text = value.strip().upper()
    return text[0], int(text[1:])


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
    if family == "drop_fiber":
        return round_half_up_to_increment(feet * CABLE_BUFFER, 1)
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
