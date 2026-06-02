from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

import fitz

from app.rate_cards import code_key


@dataclass(frozen=True)
class TextBlock:
    page: int
    bbox: tuple[float, float, float, float]
    text: str


@dataclass(frozen=True)
class ExtractionDiagnostics:
    block_count: int
    text_chars: int
    quantity_line_count: int
    code_total_count: int
    material_candidate_count: int
    review_required: bool
    warnings: list[str]


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
    try:
        for page_index, page in enumerate(doc[:max_pages], start=1):
            for raw in page.get_text("blocks", sort=True):
                x0, y0, x1, y1, text, *_ = raw
                cleaned = _clean_text(text)
                if _is_noise(cleaned):
                    continue
                blocks.append(
                    TextBlock(
                        page=page_index,
                        bbox=(round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)),
                        text=cleaned,
                    )
                )
    finally:
        doc.close()
    return blocks


def extract_likely_quantity_lines(blocks: list[TextBlock]) -> list[str]:
    patterns = [
        re.compile(r"\b(?:UG|CD|MDU|COMP|Comp|FB|FX|PC|TL|CX|PT|SMC)-?\d+\b"),
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
    code_catalog: dict[tuple[str, int], str] | None = None,
) -> list[str]:
    pattern = re.compile(
        r"\b((?:UG|CD|MDU|COMP|Comp|FB|FX|PC|TL|CX|PT|SMC)-?\d+)\s*-\s*([0-9]+(?:\.[0-9]+)?)(\s*(?:'|sqft))?",
        re.I,
    )
    catalog = code_catalog or {}
    totals: dict[tuple[tuple[str, int], str], float] = defaultdict(float)
    display: dict[tuple[tuple[str, int], str], str] = {}
    order: list[tuple[tuple[str, int], str]] = []
    for block in blocks:
        for line in block.text.splitlines():
            for match in pattern.finditer(line):
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
                    display[key] = catalog.get(normalized_key, _display_code(raw_code))
                totals[key] += float(raw_qty)

    rows: list[str] = []
    for key in order:
        code = display[key]
        unit = key[1]
        rows.append(f"{code} - {_format_number(totals[key])}{unit}")
    return rows


def _display_code(raw_code: str) -> str:
    raw_code = raw_code.strip()
    if "-" in raw_code:
        return raw_code
    match = re.match(r"([A-Za-z]+)(\d+)", raw_code)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return raw_code


def _is_non_billing_context(line: str, match_start: int) -> bool:
    prefix = line[:match_start].strip().lower()
    full = line.lower()
    if "bore@" in full or "trench@" in full:
        return True
    if re.search(r"\b(?:dirt|concrete|asphalt|pwr|wtr|swr|irr|cox|stl)\s*-\s*$", prefix):
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
    warnings: list[str] = []

    if len(blocks) < 5 or text_chars < 120:
        warnings.append("This PDF does not have enough readable text for automatic summation.")
    elif len(quantity_lines) < 2:
        warnings.append("The PDF text layer has very few readable quantity lines.")

    if not code_totals:
        warnings.append("No supported billing-code totals were found in the parsed text.")

    review_required = not code_totals and (len(blocks) < 5 or text_chars < 120 or len(quantity_lines) < 2)
    if review_required:
        warnings.append("Manual review is required; the app did not add unsupported totals.")

    return ExtractionDiagnostics(
        block_count=len(blocks),
        text_chars=text_chars,
        quantity_line_count=len(quantity_lines),
        code_total_count=len(code_totals),
        material_candidate_count=len(material_candidates),
        review_required=review_required,
        warnings=warnings,
    )


def build_pdf_context(
    pdf_bytes: bytes,
    max_chars: int = 26000,
    code_catalog: dict[tuple[str, int], str] | None = None,
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
