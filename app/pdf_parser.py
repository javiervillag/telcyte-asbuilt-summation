from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

import fitz


@dataclass(frozen=True)
class TextBlock:
    page: int
    bbox: tuple[float, float, float, float]
    text: str


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


def derive_code_totals(blocks: list[TextBlock]) -> list[str]:
    pattern = re.compile(
        r"\b((?:UG|CD|MDU|COMP|Comp|FB|FX|PC|TL|CX|PT|SMC)-?\d+)\s*-\s*([0-9]+(?:\.[0-9]+)?)(\s*(?:'|sqft))?",
        re.I,
    )
    totals: dict[tuple[str, str], float] = defaultdict(float)
    display: dict[tuple[str, str], str] = {}
    order: list[tuple[str, str]] = []
    for block in blocks:
        for match in pattern.finditer(block.text):
            raw_code, raw_qty, raw_unit = match.groups()
            code = raw_code.upper()
            unit = (raw_unit or "").strip()
            key = (code, unit)
            if key not in totals:
                order.append(key)
                display[key] = code
            totals[key] += float(raw_qty)

    rows: list[str] = []
    for key in order:
        code = display[key]
        unit = key[1]
        rows.append(f"{code} - {_format_number(totals[key])}{unit}")
    return rows


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


def build_pdf_context(pdf_bytes: bytes, max_chars: int = 26000) -> str:
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
    code_totals = derive_code_totals(blocks)
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
