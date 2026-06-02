from __future__ import annotations

import csv
import re
from pathlib import Path

CODE_PATTERN = re.compile(r"\b(UG|CD|MDU|COMP|FB|FX|PC|TL|CX|PT|SMC)-?(\d{1,3})\b", re.I)
ZERO_PAD_EQUIVALENT_PREFIXES = {"UG", "CD", "MDU", "FB", "FX", "PC", "TL", "CX", "PT", "SMC"}
CodeKey = tuple[str, str]


def code_key(code: str) -> CodeKey | None:
    match = CODE_PATTERN.search(code)
    if not match:
        return None
    prefix = match.group(1).upper()
    number = match.group(2)
    if prefix in ZERO_PAD_EQUIVALENT_PREFIXES:
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
            codes.append(_format_code(match.group(1), match.group(2), raw))
    return codes


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
        workbook = load_workbook(path, data_only=True, read_only=True)
        text_parts: list[str] = []
        try:
            for sheet in workbook.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    text_parts.extend(str(cell) for cell in row if cell is not None)
        finally:
            workbook.close()
        return extract_codes_from_text("\n".join(text_parts))
    return extract_codes_from_text(path.read_text(errors="ignore"))


def _format_code(prefix: str, number: str, raw: str) -> str:
    raw = raw.strip()
    if "-" in raw:
        return raw
    return f"{prefix}-{number}"
