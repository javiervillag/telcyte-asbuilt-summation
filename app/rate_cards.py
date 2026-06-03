from __future__ import annotations

import re
from pathlib import Path

KNOWN_COMPACT_PREFIXES = "UG|CD|MDU|COMP|FB|FX|PC|TL|CX|PT|SMC|SME|DP"
CODE_TEXT_PATTERN = rf"(?:{KNOWN_COMPACT_PREFIXES})-?\d{{1,3}}|[A-Z]{{2,5}}-\d{{1,3}}"
CODE_PATTERN = re.compile(rf"\b({CODE_TEXT_PATTERN})(?!\.\d)\b", re.I)
CodeKey = tuple[str, str]
TotalKey = tuple[CodeKey, str, str]


def code_key(code: str) -> CodeKey | None:
    match = CODE_PATTERN.search(code)
    if not match:
        return None
    parsed = re.match(r"([A-Za-z]+)-?(\d{1,3})$", match.group(1), re.I)
    if not parsed:
        return None
    prefix = parsed.group(1).upper()
    if prefix == "ELI":
        return None
    number = parsed.group(2)
    if prefix != "COMP":
        number = str(int(number))
    return (prefix, number)


def extract_codes_from_text(text: str) -> list[str]:
    codes: list[str] = []
    seen: set[CodeKey] = set()
    for match in CODE_PATTERN.finditer(text):
        raw = match.group(0)
        key = code_key(raw)
        if key and key not in seen:
            seen.add(key)
            codes.append(_format_code(key, raw))
    return codes


def total_line_key(line: str) -> TotalKey | None:
    code_match = CODE_PATTERN.search(line)
    if not code_match:
        return None
    key = code_key(code_match.group(0))
    if not key:
        return None
    remainder = line[code_match.end() :]
    qty_match = re.match(r"\s*-\s*([0-9]+(?:\.[0-9]+)?)(\s*(?:'|sqft))?", remainder, re.I)
    if not qty_match:
        return None
    qty = _normalize_quantity(qty_match.group(1))
    unit = (qty_match.group(2) or "").strip().lower()
    return (key, qty, unit)


def load_code_catalog(raw_codes: str = "", paths: str = "") -> dict[CodeKey, str]:
    catalog: dict[CodeKey, str] = {}
    for code in extract_codes_from_text(raw_codes):
        key = code_key(code)
        if key:
            catalog.setdefault(key, code)

    for raw_path in [p.strip() for p in paths.split(",") if p.strip()]:
        path = Path(raw_path)
        if not path.exists():
            continue
        for code in _codes_from_path(path):
            key = code_key(code)
            if key:
                catalog.setdefault(key, code)
    return catalog


def _codes_from_path(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".csv", ".tsv"}:
        return extract_codes_from_text(path.read_text(errors="ignore"))
    if suffix == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError:
            return []
        workbook = load_workbook(path, data_only=True, read_only=False)
        highlighted_parts: list[str] = []
        highlighted_sheet_parts: list[str] = []
        all_parts: list[str] = []
        try:
            for sheet in workbook.worksheets:
                sheet_is_highlighted = bool(sheet.sheet_properties.tabColor)
                for row in sheet.iter_rows():
                    for cell in row:
                        if cell.value is None:
                            continue
                        text = str(cell.value)
                        all_parts.append(text)
                        if _has_highlight_fill(cell.fill):
                            highlighted_parts.append(text)
                        if sheet_is_highlighted:
                            highlighted_sheet_parts.append(text)
        finally:
            workbook.close()
        if highlighted_parts:
            return extract_codes_from_text("\n".join(highlighted_parts))
        if highlighted_sheet_parts:
            return extract_codes_from_text("\n".join(highlighted_sheet_parts))
        return extract_codes_from_text("\n".join(all_parts))
    return extract_codes_from_text(path.read_text(errors="ignore"))


def _format_code(key: CodeKey, raw: str) -> str:
    raw = raw.strip()
    if "-" in raw:
        return raw
    prefix, number = key
    return f"{prefix}-{number}"


def _normalize_quantity(value: str) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _has_highlight_fill(fill) -> bool:
    if not fill or not fill.fill_type or fill.fill_type == "none":
        return False
    color = fill.fgColor
    if not color:
        return True
    if color.type == "rgb" and color.rgb in {"00000000", "00FFFFFF", "FFFFFFFF"}:
        return False
    if color.type == "indexed" and color.indexed in {64, 65}:
        return False
    return True
