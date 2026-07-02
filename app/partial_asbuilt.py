from __future__ import annotations

from app.box_titles import is_previously_billed_totals_box, starts_with_job_totals_title
from app.pdf_parser import TextBlock
from app.rate_cards import CodeKey, total_line_key


def extract_previously_billed_job_totals(blocks: list[TextBlock]) -> dict[CodeKey, float]:
    totals: dict[CodeKey, float] = {}
    for block in blocks:
        if block.source != "annotation":
            continue
        if not is_previously_billed_totals_box(block.text):
            continue
        if not starts_with_job_totals_title(block.text):
            continue
        for line in block.text.splitlines():
            parsed = total_line_key(line)
            if not parsed:
                continue
            key, qty, _unit = parsed
            totals[key] = totals.get(key, 0.0) + float(qty.replace(",", ""))
    return totals


def derive_new_totals(
    cumulative_rows: list[str],
    previous_totals: dict[CodeKey, float],
) -> tuple[list[str], list[str]]:
    if not previous_totals:
        return [], []

    rows: list[str] = []
    warnings: list[str] = []
    seen_previous = set(previous_totals)
    for row in cumulative_rows:
        parsed = total_line_key(row)
        if not parsed:
            continue
        key, cumulative_qty_text, _unit = parsed
        previous_qty = previous_totals.get(key)
        if previous_qty is None:
            continue
        seen_previous.discard(key)
        cumulative_qty = float(cumulative_qty_text.replace(",", ""))
        delta = cumulative_qty - previous_qty
        if delta < 0:
            warnings.append(
                f"Previously billed total is higher than the recomputed total for {key[0]}-{key[1]}; "
                "new-work delta needs review."
            )
            continue
        if delta == 0:
            continue
        rows.append(f"{_display_from_row(row)} - {_format_number(delta)}")

    if seen_previous:
        labels = ", ".join(f"{prefix}-{number}" for prefix, number in sorted(seen_previous))
        warnings.append(f"Previously billed code(s) were not found in recomputed totals: {labels}.")
    return rows, warnings


def _display_from_row(row: str) -> str:
    parsed = total_line_key(row)
    if not parsed:
        return row.split("-", 1)[0].strip()
    key, qty, _unit = parsed
    marker = f" - {qty}"
    if marker in row:
        return row.split(marker, 1)[0].strip()
    prefix, number = key
    return f"{prefix.title() if prefix == 'COMP' else prefix}-{number}"


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"
