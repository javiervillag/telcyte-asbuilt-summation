from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

import fitz

from app.box_titles import starts_with_materials_title, starts_with_totals_title
from app.rate_cards import (
    CODE_PATTERN,
    CODE_TEXT_PATTERN,
    KNOWN_PREFIX_SET,
    NON_BILLING_PREFIXES,
    QTY_TEXT_PATTERN,
    CodeKey,
    code_key,
)


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
    informational_notes: list[str]


# Permit drawings span many sheets with billing callouts on later pages
# (NR-702749 PRJ52: pages 4-5 held ~90% of the codes, 2026-06-10). Parse
# every page, bounded only as a safety cap for pathological files.
DEFAULT_MAX_PARSE_PAGES = 12

MIN_READABLE_BLOCKS = 5
MIN_READABLE_CHARS = 120
MIN_QUANTITY_LINES = 2
UNRESOLVED_CALLOUT_PATTERN = re.compile(
    r"\b(?:EOL|Tie\s*Point|Storage|Pull\s*through|Pull-through)\b",
    re.I,
)


def _clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    # Normalize typographic characters so authored maps match the patterns:
    # en/em/minus dashes -> hyphen, multiplication sign -> x.
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    text = text.replace("\u00d7", "x").replace("\u00a0", " ")
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


def extract_text_blocks(pdf_bytes: bytes, max_pages: int = DEFAULT_MAX_PARSE_PAGES) -> list[TextBlock]:
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
                # A previously stamped totals box is kept as a block (so the
                # reviewer sees it) but must NOT feed the page-text dedup
                # sets: a genuine field callout that happens to equal one of
                # its lines would otherwise be silently dropped.
                if not starts_with_totals_title(cleaned):
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


def _aggregate_code_rows(
    blocks: list[TextBlock],
    catalog: dict[CodeKey, str],
    excluded_lines: list[str] | None,
    catalog_misses: list[str],
) -> tuple[list[str], list[CodeKey]]:
    """Two-pass billing-code aggregation over already-filtered field blocks.

    Returns ``(rows, ordered_keys)``. Shared by ``derive_code_totals`` (job
    totals) and ``derive_code_totals_by_page`` (per-page totals) so the two can
    never diverge in how they read the same callouts. Callers own exclusion
    (``field_evidence_blocks``) and notes; this is pure aggregation.
    """
    direct_pattern = re.compile(
        rf"\b({CODE_TEXT_PATTERN})\s*-\s*({QTY_TEXT_PATTERN})(\s*(?:'|sqft))?",
        re.I,
    )
    quantity_first_pattern = re.compile(
        rf"\b({QTY_TEXT_PATTERN})\s*x\s*({CODE_TEXT_PATTERN})\b",
        re.I,
    )
    totals: dict[CodeKey, float] = defaultdict(float)
    display: dict[CodeKey, str] = {}
    order: list[CodeKey] = []

    def _record_exclusion(line: str) -> None:
        cleaned = line.strip()
        if excluded_lines is not None and cleaned and cleaned not in excluded_lines:
            excluded_lines.append(cleaned)

    def _record_catalog_miss(line: str) -> None:
        cleaned = line.strip()
        if cleaned and cleaned not in catalog_misses:
            catalog_misses.append(cleaned)

    for block in blocks:
        for line in block.text.splitlines():
            for match in direct_pattern.finditer(line):
                raw_code, raw_qty, _raw_unit = match.groups()
                normalized_key = code_key(raw_code)
                if not normalized_key:
                    continue
                if catalog and normalized_key not in catalog:
                    _record_catalog_miss(line)
                    continue
                if _is_non_billing_context(line, match.start()):
                    _record_exclusion(line)
                    continue
                key = normalized_key
                if key not in totals:
                    order.append(key)
                    display[key] = catalog.get(normalized_key, _display_code(raw_code, normalized_key))
                totals[key] += float(raw_qty.replace(",", ""))

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
                    _record_catalog_miss(line)
                    continue
                key = normalized_key
                if key in direct_keys:
                    continue
                if key not in totals:
                    order.append(key)
                    display[key] = catalog.get(normalized_key, _display_code(raw_code, normalized_key))
                totals[key] += float(raw_qty.replace(",", ""))

    rows = [f"{display[key]} - {_format_number(totals[key])}" for key in order]
    return rows, order


def derive_code_totals(
    blocks: list[TextBlock],
    code_catalog: dict[CodeKey, str] | None = None,
    excluded_lines: list[str] | None = None,
    notes: list[str] | None = None,
    warnings: list[str] | None = None,
) -> list[str]:
    """Aggregate billing-code totals from text blocks.

    Unit markers (' and sqft) are consumed but ignored: per Nick Evans
    (email 2026-06-09, BI-304069) quantities for the same code always total
    together and the output rows carry no unit suffix ("UG-80 - 258").
    Lines skipped as non-billing context are appended to ``excluded_lines``
    (when provided) so exclusions are never silent.
    """
    catalog = code_catalog or {}
    catalog_misses: list[str] = []

    # Re-run safety: a previously stamped "MKR Job/Page Totals" box (ours or a
    # manual one) must never be counted as field callouts. The same applies to a
    # stamped Materials box now that cable output can be re-uploaded.
    blocks, skipped_total_boxes, skipped_material_boxes = field_evidence_blocks(blocks)
    rows, order = _aggregate_code_rows(blocks, catalog, excluded_lines, catalog_misses)

    if notes is not None:
        if skipped_total_boxes:
            notes.append(
                f"Ignored {skipped_total_boxes} existing 'MKR Job/Page/New Totals' summary box(es) on the "
                "drawing (re-run detected, or a manual 'New Totals' box); totals were recomputed from the "
                "field callouts only."
            )
        if skipped_material_boxes:
            notes.append(
                f"Found {skipped_material_boxes} existing 'Materials' box(es); ignored as calculation evidence "
                "so re-runs do not double-count. The visible box is preserved and merged during stamping."
            )
        if catalog_misses:
            preview = "; ".join(catalog_misses[:6])
            if len(catalog_misses) > 6:
                preview += f"; plus {len(catalog_misses) - 6} more"
            notes.append(
                f"Codes visible on the drawing but NOT in the loaded rate card (not totaled): {preview}."
            )
        # New code prefixes totaled via the generic pattern deserve a flag:
        # they may be brand-new Cox codes (fine) or a new non-billing marker
        # (not fine) - a human should confirm the first time one appears.
        novel = sorted({key[0] for key in order if key[0] not in KNOWN_PREFIX_SET})
        if novel and not catalog:
            message = (
                "Unrecognized code prefixes totaled via the generic pattern - verify: "
                + ", ".join(novel) + "."
            )
            if warnings is not None:
                warnings.append(message)
            else:
                notes.append(message)
    return rows


def derive_code_totals_by_page(
    blocks: list[TextBlock],
    code_catalog: dict[CodeKey, str] | None = None,
) -> dict[int, list[str]]:
    """Per-page billing-code totals for multi-page as-builts (Page Totals box).

    Billing codes ONLY - no materials and no user-selected extras (those belong
    to the page-1 Job Totals box). Reuses the same exclusion
    (``field_evidence_blocks``) and the same aggregation (``_aggregate_code_rows``)
    as the job totals, so a page total can never diverge from how the job total
    counts the same callouts, and the per-page totals sum to the job total.

    Returns ``{page: rows}`` keyed by the 1-based page number, for pages that
    carry at least one billing code; pages with none are omitted.
    """
    catalog = code_catalog or {}
    field_blocks, _skipped_total, _skipped_material = field_evidence_blocks(blocks)
    by_page: dict[int, list[TextBlock]] = defaultdict(list)
    for block in field_blocks:
        by_page[block.page].append(block)
    result: dict[int, list[str]] = {}
    for page in sorted(by_page):
        rows, _order = _aggregate_code_rows(by_page[page], catalog, None, [])
        if rows:
            result[page] = rows
    return result


def field_evidence_blocks(blocks: list[TextBlock]) -> tuple[list[TextBlock], int, int]:
    """Return field blocks with prior output boxes (and their flattened remnants) removed.

    A stamped box that survives as a single text block - our live FreeText output, or a
    PyMuPDF/Adobe ``bake``-style flatten - is caught by the title prefix alone. But some
    editors re-flow the box so the title and EACH code line become separate, individually
    positioned page-text blocks. The title-only block no longer geometrically contains the
    code lines below it, so those lines used to leak back in as field callouts and double
    the totals (Nick Evans, June-23 sync: 29.76 vs 14.88). To handle that, when the title
    arrives as its own small block we absorb the contiguous column of remnant lines directly
    beneath it (same page, overlapping the title's column, vertically adjacent), regardless
    of source. A contiguous box (title + codes already in one block) is left untouched, so
    the already-correct path does not change.
    """
    skipped_total_boxes = 0
    skipped_material_boxes = 0
    anchors: list[int] = []
    for i, block in enumerate(blocks):
        if starts_with_totals_title(block.text):
            skipped_total_boxes += 1
            anchors.append(i)
        elif starts_with_materials_title(block.text):
            skipped_material_boxes += 1
            anchors.append(i)

    excluded: set[int] = set(anchors)
    regions: list[tuple[int, float, float, float, float]] = []
    for i in anchors:
        anchor = blocks[i]
        x0, y0, x1, y1 = anchor.bbox
        line_count = len([ln for ln in anchor.text.splitlines() if ln.strip()]) or 1
        # Only a title-only remnant (the flattened case) needs region growth; a
        # contiguous box already carries its code lines inside the one block.
        if line_count <= 2:
            # Absorb the contiguous run of remnant lines directly beneath the
            # title. The gap window (~1.8 line-heights) is deliberately tight: it
            # matches the internal line spacing of a real flattened box and stops
            # at the larger gap between the box and the field callouts below it.
            # A wider window was tried and REGRESSED real rotated multi-page files
            # (NR-1138768, NR-996825): the title-only baked block sits in an edge
            # column and a greedy downward walk cascaded into genuine callouts,
            # halving totals. Real-world flattens never space box lines that far
            # apart, so the tight window is both correct and safe here; a
            # hypothetical loose flatten would fall to manual review, not silent
            # double-count, because its remnant lines stay visible field evidence.
            line_h = max(6.0, (y1 - y0) / line_count)
            grew = True
            while grew:
                grew = False
                for j, b in enumerate(blocks):
                    if j in excluded or b.page != anchor.page:
                        continue
                    bx0, by0, bx1, by1 = b.bbox
                    if min(x1, bx1) - max(x0, bx0) <= 0:  # must share the box's column
                        continue
                    if by0 < y1 - line_h * 0.5 or by0 > y1 + line_h * 1.8:  # next row down only
                        continue
                    excluded.add(j)
                    x0, x1 = min(x0, bx0), max(x1, bx1)
                    y1 = max(y1, by1)
                    grew = True
        regions.append((anchor.page, x0, y0, x1, y1))

    def _inside_region(block: TextBlock) -> bool:
        cx = (block.bbox[0] + block.bbox[2]) / 2
        cy = (block.bbox[1] + block.bbox[3]) / 2
        for page, rx0, ry0, rx1, ry1 in regions:
            if block.page == page and rx0 <= cx <= rx1 and ry0 <= cy <= ry1 and (rx1 - rx0) > 1:
                return True
        return False

    field_blocks = [
        b for k, b in enumerate(blocks)
        if k not in excluded and not (b.source == "page" and _inside_region(b))
    ]
    return field_blocks, skipped_total_boxes, skipped_material_boxes


def _display_code(raw_code: str, normalized_key: CodeKey) -> str:
    # Rebuild the display form from the normalized key so spacing variants in
    # the raw callout ("UG- 6", "UG - 84") never leak into the totals box.
    prefix, number = normalized_key
    if prefix != "COMP":
        if number.isdigit() and int(number) < 10:
            return f"{prefix}-{int(number):02d}"
        return f"{prefix}-{number}"
    # COMP keeps its original number shape (Comp-9 != Comp-09) and case.
    compact = re.sub(r"\s+", "", raw_code.strip())
    match = re.match(r"([A-Za-z]+)-?(\d{1,3})", compact)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return compact


_UTILITY_CONTEXT_RE = re.compile(
    rf"\b(?:{'|'.join(sorted(p.lower() for p in NON_BILLING_PREFIXES if p != 'ELI'))})\s*[-@]\s*$"
)


def _is_non_billing_context(line: str, match_start: int) -> bool:
    """Structural (not lexical) non-billing detection.

    Only two things disqualify a matched billing code:
    1. bore/trench measurement callouts anywhere in the line, and
    2. a utility-crossing marker (NON_BILLING_PREFIXES) immediately before it.

    Surface descriptors such as DIRT-, CONCRETE-, ASPHALT- are display context
    in front of real codes and DO count: "DIRT-UG6-2" totals UG-06 by 2
    (Nick Evans, 2026-06-09 sync, Segment 7 PRJ17 missed-code bug).
    Callers surface every exclusion via derive_code_totals(excluded_lines=...)
    so nothing is dropped silently.
    """
    full = line.lower()
    if "bore@" in full or "trench@" in full:
        return True
    prefix = line[:match_start].strip().lower()
    return bool(_UTILITY_CONTEXT_RE.search(prefix))


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
    excluded_context_lines: list[str] | None = None,
    parser_notes: list[str] | None = None,
    parser_warnings: list[str] | None = None,
    resolved_callout_lines: set[str] | None = None,
    total_pages: int | None = None,
) -> ExtractionDiagnostics:
    quantity_lines = quantity_lines if quantity_lines is not None else extract_likely_quantity_lines(blocks)
    material_candidates = (
        material_candidates if material_candidates is not None else extract_material_candidates(blocks)
    )
    text_chars = sum(len(block.text) for block in blocks)
    annotation_text_count = sum(1 for block in blocks if block.source == "annotation")
    ambiguous_code_line_count = _ambiguous_code_line_count(quantity_lines)
    unresolved_callouts = _unresolved_callout_lines(blocks, resolved_callout_lines=resolved_callout_lines)
    review_callouts = [line for line in unresolved_callouts if _callout_requires_review(line)]
    note_callouts = [line for line in unresolved_callouts if line not in review_callouts]
    unresolved_callout_count = len(unresolved_callouts)
    warnings: list[str] = []
    informational_notes: list[str] = []
    has_weak_text_layer = len(blocks) < MIN_READABLE_BLOCKS or text_chars < MIN_READABLE_CHARS
    has_weak_quantity_context = len(quantity_lines) < MIN_QUANTITY_LINES
    has_strong_page_text = not has_weak_text_layer and not has_weak_quantity_context and bool(code_totals)

    if has_weak_text_layer:
        warnings.append("This PDF does not have enough readable text for automatic summation.")
    elif has_weak_quantity_context:
        warnings.append("The PDF text layer has very few readable quantity lines.")
    if annotation_text_count == 0 and has_strong_page_text:
        informational_notes.append(
            "No readable PDF text-box annotations were found; totals came from readable page text."
        )
    elif annotation_text_count == 0 and len(blocks) < 12:
        warnings.append("No readable PDF text-box annotations were found.")

    if not code_totals:
        warnings.append("No supported billing-code totals were found in the parsed text.")
    if ambiguous_code_line_count:
        warnings.append("Some billing-code text was readable but not complete enough to total automatically.")
    pages_beyond_cap = bool(total_pages and total_pages > DEFAULT_MAX_PARSE_PAGES)
    if pages_beyond_cap:
        warnings.append(
            f"PDF has {total_pages} pages; only the first {DEFAULT_MAX_PARSE_PAGES} were parsed - "
            "verify callouts on the later sheets manually."
        )
    for warning in parser_warnings or []:
        warnings.append(warning)
    for note in parser_notes or []:
        informational_notes.append(note)
    if excluded_context_lines:
        preview = "; ".join(excluded_context_lines[:6])
        if len(excluded_context_lines) > 6:
            preview += f"; plus {len(excluded_context_lines) - 6} more"
        informational_notes.append(
            f"Lines skipped as non-billing context (bore/trench or utility markers): {preview}."
        )
    if review_callouts:
        preview = "; ".join(review_callouts[:6])
        if len(review_callouts) > 6:
            preview += f"; plus {len(review_callouts) - 6} more"
        warnings.append(
            f"Readable construction callouts require rate-card/composite interpretation: {preview}."
        )
    if note_callouts and has_strong_page_text:
        preview = "; ".join(note_callouts[:6])
        if len(note_callouts) > 6:
            preview += f"; plus {len(note_callouts) - 6} more"
        informational_notes.append(
            f"Standalone construction callouts were noted but not treated as billing totals: {preview}."
        )
    elif note_callouts:
        preview = "; ".join(note_callouts[:6])
        if len(note_callouts) > 6:
            preview += f"; plus {len(note_callouts) - 6} more"
        warnings.append(
            f"Readable construction callouts require rate-card/composite interpretation: {preview}."
        )

    review_required = (
        has_weak_text_layer
        or has_weak_quantity_context
        or not code_totals
        or bool(ambiguous_code_line_count)
        or bool(review_callouts)
        or pages_beyond_cap
        or bool(parser_warnings)
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
        informational_notes=informational_notes,
    )


def _ambiguous_code_line_count(quantity_lines: list[str]) -> int:
    total_pattern = re.compile(
        rf"\b(?:{CODE_TEXT_PATTERN})\s*-\s*(?:{QTY_TEXT_PATTERN})(?:\s*(?:'|sqft))?\b",
        re.I,
    )
    quantity_first_pattern = re.compile(
        rf"\b(?:{QTY_TEXT_PATTERN})\s*x\s*(?:{CODE_TEXT_PATTERN})\b",
        re.I,
    )
    count = 0
    for line in quantity_lines:
        if CODE_PATTERN.search(line) and not total_pattern.search(line) and not quantity_first_pattern.search(line):
            count += 1
    return count


def _unresolved_callout_lines(
    blocks: list[TextBlock],
    resolved_callout_lines: set[str] | None = None,
) -> list[str]:
    resolved = resolved_callout_lines or set()
    seen: set[str] = set()
    callouts: list[str] = []
    for block in blocks:
        for line in block.text.splitlines():
            cleaned = _clean_text(line)
            if cleaned in resolved:
                continue
            if UNRESOLVED_CALLOUT_PATTERN.search(cleaned) and not CODE_PATTERN.search(cleaned):
                if cleaned not in seen:
                    seen.add(cleaned)
                    callouts.append(cleaned)
    return callouts


def _callout_requires_review(line: str) -> bool:
    if CODE_PATTERN.search(line):
        return True
    if re.search(r"\d", line):
        return True
    if re.search(r"(?:'|\bft\b|\bfeet\b|\bsqft\b|\bct\b|\bcount\b)", line, re.I):
        return True
    return False


def build_pdf_context(
    pdf_bytes: bytes,
    max_chars: int = 26000,
    code_catalog: dict[CodeKey, str] | None = None,
) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_summaries: list[str] = []
    try:
        for idx, page in enumerate(doc[:DEFAULT_MAX_PARSE_PAGES], start=1):
            page_summaries.append(
                f"Page {idx}: width={page.rect.width:.0f}, height={page.rect.height:.0f}, rotation={page.rotation}"
            )
    finally:
        doc.close()

    blocks = extract_text_blocks(pdf_bytes)
    quantity_lines = extract_likely_quantity_lines(blocks)
    code_totals = derive_code_totals(blocks, code_catalog=code_catalog)
    material_candidates = extract_material_candidates(blocks)

    # High-signal sections are always included in full (they are already
    # capped); the positioned-block section gets whatever budget remains,
    # spread fairly across ALL pages. The old approach truncated the tail
    # blindly, which silently dropped exactly the later pages where permit
    # drawings keep their billing callouts (NR-702749, 2026-06-10).
    head = [
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
        "Positioned text blocks (sampled evenly across pages; code-bearing blocks first):",
    ]
    head_text = "\n".join(head)
    remaining = max_chars - len(head_text) - 200  # reserve room for omission notes

    def _block_row(block: TextBlock) -> str:
        x0, y0, x1, y1 = block.bbox
        return f"p{block.page} [{x0},{y0},{x1},{y1}] " + block.text.replace("\n", " | ")

    def _signal(block: TextBlock) -> int:
        # Code-bearing blocks first, then anything with digits, then prose.
        if CODE_PATTERN.search(block.text):
            return 0
        if any(ch.isdigit() for ch in block.text):
            return 1
        return 2

    by_page: dict[int, list[TextBlock]] = defaultdict(list)
    for block in blocks:
        by_page[block.page].append(block)
    for page_blocks in by_page.values():
        page_blocks.sort(key=_signal)

    block_rows: list[str] = []
    omitted: dict[int, int] = defaultdict(int)
    queues = {page: list(page_blocks) for page, page_blocks in sorted(by_page.items())}
    while remaining > 0 and any(queues.values()):
        progressed = False
        for page in sorted(queues):
            if not queues[page]:
                continue
            row = _block_row(queues[page].pop(0))
            if len(row) + 1 > remaining:
                continue
            block_rows.append(row)
            remaining -= len(row) + 1
            progressed = True
        if not progressed:
            break
    for page, queue in queues.items():
        if queue:
            omitted[page] = len(queue)

    notes = [
        f"[page {page}: {count} lower-signal blocks omitted for budget]"
        for page, count in sorted(omitted.items())
    ]
    return "\n".join([head_text, *block_rows, *notes])
