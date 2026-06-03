from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

import fitz

from app.rate_cards import CODE_PATTERN, CODE_TEXT_PATTERN, CodeKey, code_key


@dataclass(frozen=True)
class TextBlock:
    page: int
    bbox: tuple[float, float, float, float]
    text: str
    source: str = "page"


@dataclass(frozen=True)
class ExtractionDiagnostics:
    block_count: int
    text_chars: int
    annotation_text_count: int
    quantity_line_count: int
    ambiguous_code_line_count: int
    unresolved_callout_count: int
    unresolved_callouts: list[str]
    code_total_count: int
    material_candidate_count: int
    review_required: bool
    warnings: list[str]


MIN_READABLE_BLOCKS = 5
MIN_READABLE_CHARS = 120
MIN_QUANTITY_LINES = 2
UNRESOLVED_CALLOUT_PATTERN = re.compile(
    r"\b(?:EOL|Tie\s*Point|Storage|Pull\s*through|Pull-through)\b",
    re.I,
)


def _clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_noise(text: str) -> bool:
    value = text.strip()
    if not value:
        return True
    if len(value) == 1 and not value.isalnum():
        return True
    return False


def extract_text_blocks(pdf_bytes: bytes, max_pages: int = 3) -> list[TextBlock]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    blocks: list[TextBlock] = []
    seen: set[tuple[int, tuple[float, float, float, float], str]] = set()
    try:
        for page_index, page in enumerate(doc[:max_pages], start=1):
            annotation_texts: set[str] = set()
            annotation_lines: set[str] = set()
            for annot in page.annots() or []:
                cleaned = _clean_text(str((annot.info or {}).get("content") or "").replace("\r", "\n"))
                if _is_noise(cleaned):
                    continue
                rect = annot.rect
                bbox = (round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1))
                key = (page_index, bbox, cleaned)
                if key in seen:
                    continue
                seen.add(key)
                annotation_texts.add(cleaned)
                annotation_lines.update(line for line in cleaned.splitlines() if line)
                blocks.append(TextBlock(page=page_index, bbox=bbox, text=cleaned, source="annotation"))
            for raw in page.get_text("blocks", sort=True):
                x0, y0, x1, y1, text, *_ = raw
                cleaned = _clean_text(text)
                if _is_noise(cleaned):
                    continue
                if cleaned in annotation_texts:
                    continue
                cleaned = _remove_duplicate_annotation_lines(cleaned, annotation_lines)
                if _is_noise(cleaned):
                    continue
                bbox = (round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1))
                key = (page_index, bbox, cleaned)
                if key in seen:
                    continue
                seen.add(key)
                blocks.append(
                    TextBlock(
                        page=page_index,
                        bbox=bbox,
                        text=cleaned,
                    )
                )
    finally:
        doc.close()
    blocks.sort(key=lambda block: (block.page, block.bbox[1], block.bbox[0], block.source))
    return blocks


def _remove_duplicate_annotation_lines(text: str, annotation_lines: set[str]) -> str:
    if not annotation_lines:
        return text
    lines = [line for line in text.splitlines() if line.strip() not in annotation_lines]
    return _clean_text("\n".join(lines))


def extract_likely_quantity_lines(blocks: list[TextBlock]) -> list[str]:
    patterns = [
        CODE_PATTERN,
        re.compile(r"\b\d{3}-\d{4}\b"),
        re.compile(r"\b\d+(?:\.\d+)?\s*(?:'|sqft|Ct|ct|x)\b"),
        re.compile(r"\b(?:PVC|Vault|Ped|Rod|Wire|Mule|Tape|Lube|Seal|Conduit|Duct|Panel|D-Case)\b", re.I),
    ]
    seen: set[str] = set()
    lines: list[str] = []
    for block in blocks:
        for line in block.text.splitlines():
            cleaned = _clean_text(line)
            if not cleaned or cleaned in seen:
                continue
            if any(pattern.search(cleaned) for pattern in patterns):
                seen.add(cleaned)
                lines.append(cleaned)
    return lines


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def derive_code_totals(
    blocks: list[TextBlock],
    code_catalog: dict[CodeKey, str] | None = None,
) -> list[str]:
    direct_pattern = re.compile(
        rf"\b({CODE_TEXT_PATTERN})\s*-\s*([0-9]+(?:\.[0-9]+)?)(\s*(?:'|sqft))?",
        re.I,
    )
    quantity_first_pattern = re.compile(
        rf"\b([0-9]+(?:\.[0-9]+)?)\s*x\s*({CODE_TEXT_PATTERN})\b",
        re.I,
    )
    catalog = code_catalog or {}
    totals: dict[tuple[CodeKey, str], float] = defaultdict(float)
    display: dict[tuple[CodeKey, str], str] = {}
    order: list[tuple[CodeKey, str]] = []
    for block in blocks:
        for line in block.text.splitlines():
            for match in direct_pattern.finditer(line):
                if _is_non_billing_context(line, match.start()):
                    continue
                raw_code, raw_qty, raw_unit = match.groups()
                normalized_key = code_key(raw_code)
                if not normalized_key:
                    continue
                if catalog and normalized_key not in catalog:
                    continue
                unit = (raw_unit or "").strip()
                key = (normalized_key, unit)
                if key not in totals:
                    order.append(key)
                    display[key] = catalog.get(normalized_key, _display_code(raw_code, normalized_key))
                totals[key] += float(raw_qty)

    direct_keys = set(totals)
    for block in blocks:
        for line in block.text.splitlines():
            if direct_pattern.search(line):
                continue
            for match in quantity_first_pattern.finditer(line):
                raw_qty, raw_code = match.groups()
                normalized_key = code_key(raw_code)
                if not normalized_key:
                    continue
                if catalog and normalized_key not in catalog:
                    continue
                key = (normalized_key, "")
                if key in direct_keys:
                    continue
                if key not in totals:
                    order.append(key)
                    display[key] = catalog.get(normalized_key, _display_code(raw_code, normalized_key))
                totals[key] += float(raw_qty)

    rows: list[str] = []
    for key in order:
        code = display[key]
        unit = key[1]
        rows.append(f"{code} - {_format_number(totals[key])}{unit}")
    return rows


def _display_code(raw_code: str, normalized_key: CodeKey) -> str:
    raw_code = raw_code.strip()
    prefix, number = normalized_key
    if prefix != "COMP" and number.isdigit() and int(number) < 10:
        return f"{prefix}-{int(number):02d}"
    if "-" in raw_code:
        return raw_code.upper() if prefix != "COMP" else raw_code
    match = re.match(r"([A-Za-z]+)(\d+)", raw_code)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return raw_code


def _is_non_billing_context(line: str, match_start: int) -> bool:
    prefix = line[:match_start].strip().lower()
    full = line.lower()
    if "bore@" in full or "trench@" in full:
        return True
    if re.search(r"\b(?:dirt|pwr|wtr|swr|irr|cox|stl)\s*-\s*$", prefix):
        return True
    return False


def extract_material_candidates(blocks: list[TextBlock]) -> list[str]:
    material_patterns = [
        re.compile(r"\b\d{3}-\d{4}\b"),
        re.compile(r"\b(?:PVC|Vault|Ped|Rod|Wire|Mule|Tape|Lube|Seal|Conduit|Duct|Panel|D-Case|Fiber|Fbr|Pulling)\b", re.I),
        re.compile(r"\b\d+(?:\.\d+)?\s*(?:Ct|ct)\b"),
    ]
    seen: set[str] = set()
    rows: list[str] = []
    for block in blocks:
        for line in block.text.splitlines():
            cleaned = _clean_text(line)
            if not cleaned or cleaned in seen:
                continue
            if any(pattern.search(cleaned) for pattern in material_patterns):
                seen.add(cleaned)
                rows.append(cleaned)
    return rows


def diagnose_extraction(
    blocks: list[TextBlock],
    code_totals: list[str],
    material_candidates: list[str] | None = None,
    quantity_lines: list[str] | None = None,
) -> ExtractionDiagnostics:
    quantity_lines = quantity_lines if quantity_lines is not None else extract_likely_quantity_lines(blocks)
    material_candidates = (
        material_candidates if material_candidates is not None else extract_material_candidates(blocks)
    )
    text_chars = sum(len(block.text) for block in blocks)
    annotation_text_count = sum(1 for block in blocks if block.source == "annotation")
    ambiguous_code_line_count = _ambiguous_code_line_count(quantity_lines)
    unresolved_callouts = _unresolved_callout_lines(blocks)
    unresolved_callout_count = len(unresolved_callouts)
    warnings: list[str] = []
    has_weak_text_layer = len(blocks) < MIN_READABLE_BLOCKS or text_chars < MIN_READABLE_CHARS
    has_weak_quantity_context = len(quantity_lines) < MIN_QUANTITY_LINES

    if has_weak_text_layer:
        warnings.append("This PDF does not have enough readable text for automatic summation.")
    elif has_weak_quantity_context:
        warnings.append("The PDF text layer has very few readable quantity lines.")
    if annotation_text_count == 0 and len(blocks) < 12:
        warnings.append("No readable PDF text-box annotations were found.")

    if not code_totals:
        warnings.append("No supported billing-code totals were found in the parsed text.")
    if ambiguous_code_line_count:
        warnings.append("Some billing-code text was readable but not complete enough to total automatically.")
    if unresolved_callout_count:
        preview = "; ".join(unresolved_callouts[:6])
        if len(unresolved_callouts) > 6:
            preview += f"; plus {len(unresolved_callouts) - 6} more"
        warnings.append(
            f"Readable construction callouts require rate-card/composite interpretation: {preview}."
        )

    review_required = (
        has_weak_text_layer
        or has_weak_quantity_context
        or not code_totals
        or bool(ambiguous_code_line_count)
        or bool(unresolved_callout_count)
    )
    if review_required:
        warnings.append("Manual review is required; the app did not add unsupported totals.")

    return ExtractionDiagnostics(
        block_count=len(blocks),
        text_chars=text_chars,
        annotation_text_count=annotation_text_count,
        quantity_line_count=len(quantity_lines),
        ambiguous_code_line_count=ambiguous_code_line_count,
        unresolved_callout_count=unresolved_callout_count,
        unresolved_callouts=unresolved_callouts,
        code_total_count=len(code_totals),
        material_candidate_count=len(material_candidates),
        review_required=review_required,
        warnings=warnings,
    )


def _ambiguous_code_line_count(quantity_lines: list[str]) -> int:
    total_pattern = re.compile(
        rf"\b(?:{CODE_TEXT_PATTERN})\s*-\s*[0-9]+(?:\.[0-9]+)?(?:\s*(?:'|sqft))?\b",
        re.I,
    )
    quantity_first_pattern = re.compile(
        rf"\b[0-9]+(?:\.[0-9]+)?\s*x\s*(?:{CODE_TEXT_PATTERN})\b",
        re.I,
    )
    count = 0
    for line in quantity_lines:
        if CODE_PATTERN.search(line) and not total_pattern.search(line) and not quantity_first_pattern.search(line):
            count += 1
    return count


def _unresolved_callout_lines(blocks: list[TextBlock]) -> list[str]:
    seen: set[str] = set()
    callouts: list[str] = []
    for block in blocks:
        for line in block.text.splitlines():
            cleaned = _clean_text(line)
            if UNRESOLVED_CALLOUT_PATTERN.search(cleaned) and not CODE_PATTERN.search(cleaned):
                if cleaned not in seen:
                    seen.add(cleaned)
                    callouts.append(cleaned)
    return callouts


def build_pdf_context(
    pdf_bytes: bytes,
    max_chars: int = 26000,
    code_catalog: dict[CodeKey, str] | None = None,
) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_summaries: list[str] = []
    try:
        for idx, page in enumerate(doc[:3], start=1):
            page_summaries.append(
                f"Page {idx}: width={page.rect.width:.0f}, height={page.rect.height:.0f}, rotation={page.rotation}"
            )
    finally:
        doc.close()

    blocks = extract_text_blocks(pdf_bytes)
    quantity_lines = extract_likely_quantity_lines(blocks)
    code_totals = derive_code_totals(blocks, code_catalog=code_catalog)
    material_candidates = extract_material_candidates(blocks)

    block_rows = []
    for block in blocks[:650]:
        x0, y0, x1, y1 = block.bbox
        text = block.text.replace("\n", " | ")
        block_rows.append(f"p{block.page} [{x0},{y0},{x1},{y1}] {text}")

    context = [
        "PDF page metadata:",
        *page_summaries,
        "",
        "Likely quantity/material lines extracted from text layer:",
        *quantity_lines[:450],
        "",
        "Deterministic code totals aggregated from repeated drawing labels:",
        *code_totals[:200],
        "",
        "Material candidates extracted from part numbers and material words:",
        *material_candidates[:250],
        "",
        "Positioned text blocks:",
        *block_rows,
    ]
    joined = "\n".join(context)
    if len(joined) > max_chars:
        joined = joined[:max_chars] + "\n[truncated]"
    return joined
