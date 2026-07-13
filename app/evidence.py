from __future__ import annotations

import re
from decimal import Decimal

from app.cable_footage import material_row_key
from app.models import BillingEvidence, EvidencePart, MaterialEvidence, SummaryResult
from app.rate_cards import CodeKey, total_line_key


def code_evidence_key(key: CodeKey) -> str:
    return f"{key[0]}:{key[1]}"


def decimal_text(value: Decimal | float | int | str) -> str:
    number = value if isinstance(value, Decimal) else Decimal(str(value).replace(",", ""))
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def build_billing_evidence(
    rows: list[str],
    contributions: dict[CodeKey, list[EvidencePart]],
) -> list[BillingEvidence]:
    evidence: list[BillingEvidence] = []
    for row in rows:
        parsed = total_line_key(row)
        if not parsed:
            continue
        key, quantity, _unit = parsed
        marker = f" - {quantity}"
        display = row.split(marker, 1)[0].strip() if marker in row else f"{key[0]}-{key[1]}"
        evidence.append(
            BillingEvidence(
                key=code_evidence_key(key),
                display=display,
                total=decimal_text(quantity),
                parts=list(contributions.get(key, [])),
            )
        )
    return evidence


def finalize_material_evidence(summary: SummaryResult) -> None:
    """Describe the actual Materials-box rows after the annotator's merge."""
    evidence = list(summary.evidence.materials)
    represented_keys = {
        key
        for item in evidence
        if (key := material_row_key(item.result))
    }
    represented_rows = {_normalized(item.result) for item in evidence}
    cable_numeric_keys = {
        key
        for line in summary.cable_footage
        if line.material_line and (key := material_row_key(line.material_line))
    }
    cable_rows = {
        _normalized(row)
        for line in summary.cable_footage
        for row in (line.material_line, line.review_material_line)
        if row
    }
    cable_review_keys = {
        key
        for line in summary.cable_footage
        if line.review_material_line and (key := material_row_key(line.review_material_line))
    }
    generated_keys = {
        key for row in summary.materials if (key := material_row_key(row))
    }
    generated_rows = {_normalized(row) for row in summary.materials}

    for row in summary.final_material_rows:
        normalized = _normalized(row)
        key = material_row_key(row)
        if (
            normalized in represented_rows
            or (key and key in represented_keys)
            or normalized in cable_rows
            or (key and key in cable_numeric_keys)
        ):
            continue
        generated = normalized in generated_rows or (
            key and key in generated_keys and key not in cable_review_keys
        )
        evidence.append(
            MaterialEvidence(
                part=_part_number(row),
                display=row,
                rule="generated material row" if generated else "preserved from existing Materials box",
                result=row,
            )
        )
        represented_rows.add(normalized)
        if key:
            represented_keys.add(key)

    summary.evidence.materials = evidence


def _part_number(row: str) -> str:
    match = re.search(r"\b\d{3}-\d{4}\b", row)
    return match.group(0) if match else ""


def _normalized(row: str) -> str:
    return re.sub(r"\s+", " ", row or "").strip().casefold()
