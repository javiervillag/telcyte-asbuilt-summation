from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import re

import fitz
from PIL import Image

from app.box_titles import (
    is_previously_billed_totals_box,
    is_tool_new_totals_box,
    starts_with_job_totals_title,
    starts_with_materials_title,
    starts_with_page_totals_title,
)
from app.cable_footage import extract_material_rows, material_row_key, merge_material_rows
from app.models import SummaryResult
from app.rate_cards import total_line_key

BOX_FILL = (0.78, 1.0, 0.63)
TEXT_RED = (1.0, 0.0, 0.0)
MATERIAL_TEXT = (0.0, 0.0, 0.0)
MATERIAL_BORDER_BLUE = (0.0, 0.0, 1.0)
NEW_TOTALS_FILL = (1.0, 1.0, 0.45)
REGULAR_FONT_ENV = "TELCYTE_PDF_REGULAR_FONT_PATH"
BOLD_NARROW_FONT_ENV = "TELCYTE_PDF_BOLD_NARROW_FONT_PATH"
MAX_SAFE_PLACEMENT_SCORE = 1.35
BORDER_WIDTH = 2.0
FONT_SCALE_DIVISOR = 80.0
RIGHT_SIDE_PENALTY = 0.12
# A candidate is "acceptable" when it passes all three checks below; the
# left-corner candidates are tried first and the first acceptable one wins
# (Nick, BI-945043 2026-06-10: box went upper-right although upper-left
# had room - left must win unless it is actually blocked). Base-map line
# work (plat parcels, boundary lines) may be covered, so the density cap
# tolerates moderate ink.
MAX_ACCEPTABLE_DENSITY = 0.30
MAX_TEXT_OVERLAP_RATIO = 0.18
MAX_ANNOTATION_OVERLAP_RATIO = 0.01


REGULAR_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
]
BOLD_NARROW_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Narrow_Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSansNarrow-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
]


@dataclass(frozen=True)
class TextStyle:
    size: float
    color: tuple[float, float, float]
    bold_narrow: bool = False
    rotate: int = 0


class PlacementReviewRequired(RuntimeError):
    def __init__(self) -> None:
        super().__init__("No low-impact location was found for the summary box.")


@dataclass(frozen=True)
class PlacementScore:
    total: float
    density: float
    text_overlap_ratio: float
    annotation_overlap_ratio: float
    rect: fitz.Rect


@dataclass(frozen=True)
class ExistingTotalsBox:
    xref: int
    rect: fitz.Rect
    content: str
    font_size: float | None = None


@dataclass(frozen=True)
class AddedFreeTextBox:
    xref: int
    font_size: float


def _render_page_for_density(page: fitz.Page, scale: float = 0.12) -> Image.Image:
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _ink_density(img: Image.Image, page: fitz.Page, rect: fitz.Rect, scale: float) -> float:
    left = max(0, int(rect.x0 * scale))
    top = max(0, int(rect.y0 * scale))
    right = min(img.width, int(rect.x1 * scale))
    bottom = min(img.height, int(rect.y1 * scale))
    if right <= left or bottom <= top:
        return 1.0
    crop = img.crop((left, top, right, bottom)).convert("L")
    pixels = list(crop.getdata())
    if not pixels:
        return 1.0
    return sum(1 for p in pixels if p < 245) / len(pixels)


def _box_metrics(page: fitz.Page, lines: list[str]) -> tuple[float, float, float, float]:
    # Font scales with sheet size so the box reads large on any map scale
    # without a fixed point size (Nick Evans, 2026-06-09: "font size 20 might
    # be gigantic on one map and tiny on another"). Roughly 2x the old size.
    font_size = max(10.0, min(40.0, page.rect.width / FONT_SCALE_DIVISOR))
    # Shrink-to-fit: long line lists (totals + future materials box) must
    # never silently drop lines from the stamp. Reduce the font until every
    # line fits within the height cap (floor 8pt).
    max_height = page.rect.height * 0.82
    if lines:
        fitting = (max_height - font_size) / (len(lines) * 1.18 + 1.0)
        font_size = max(8.0, min(font_size, fitting))
    line_height = font_size * 1.18
    padding = font_size * 0.5
    longest = max(
        (fitz.get_text_length(line, fontname="helv", fontsize=font_size) for line in lines),
        default=font_size * 10,
    )
    # Tight fit around the measured text to minimize blank space in the box.
    width = min(page.rect.width * 0.34, longest + padding * 2 + font_size * 0.8)
    max_text_width = max(font_size * 4, width - padding * 2)
    rendered_count = sum(len(_wrap_line(line, max_text_width, font_size)) for line in lines)
    height = min(page.rect.height * 0.82, rendered_count * line_height + padding * 2 + font_size * 0.4)
    return width, height, font_size, padding


def _placement_box_metrics(
    page: fitz.Page, lines: list[str], display_space: bool = False
) -> tuple[float, float, float, float]:
    return _placement_box_metrics_for_font(page, lines, display_space=display_space)


def _placement_box_metrics_for_font(
    page: fitz.Page,
    lines: list[str],
    display_space: bool = False,
    font_size: float | None = None,
) -> tuple[float, float, float, float]:
    width, height, font_size, padding = _box_metrics_for_font(page, lines, font_size)
    if not display_space and page.rotation in {90, 270}:
        return height, width, font_size, padding
    return width, height, font_size, padding


def choose_box_rect(page: fitz.Page, lines: list[str], display_space: bool = False) -> fitz.Rect:
    """Pick the box rect.

    With display_space=True (baked box on rotated sheets) all geometry is in
    the viewer's coordinate system - same top-left-first corner logic as
    normal pages - and the caller transforms the result into page space for
    drawing. The legacy rotation special-casing only applies to the FreeText
    annotation path.
    """
    width, height, _, _ = _placement_box_metrics(page, lines, display_space=display_space)
    margin_x = max(14.0, page.rect.width * 0.014)
    margin_y = max(18.0, page.rect.height * 0.018)
    if not display_space and page.rotation in {90, 270}:
        # Rotated PDFs report page coordinates differently from the viewer. Keep
        # candidates in visible corners and let density scoring choose the least
        # disruptive one.
        margin_y = max(margin_y, page.mediabox.height * 0.02)

    candidates = _candidate_rects(page, width, height, margin_x, margin_y, display_space=display_space)
    density_image = _render_page_for_density(page)
    scale = density_image.width / page.rect.width if page.rect.width else 0.12
    text_blocks = _page_text_rects(page)
    annotation_blocks = _page_annotation_rects(page)
    scored = [
        _placement_score(
            density_image,
            page,
            candidate,
            scale,
            text_blocks,
            annotation_blocks,
        )
        for candidate in candidates
    ]
    # Preference order: all left-column candidates (top to bottom), then the
    # right column. First acceptable candidate wins; density scoring is only
    # the tie-breaking fallback when every corner is genuinely busy.
    for row in scored:
        if (
            page.rect.contains(row.rect)
            and row.density <= MAX_ACCEPTABLE_DENSITY
            and row.text_overlap_ratio <= MAX_TEXT_OVERLAP_RATIO
            and row.annotation_overlap_ratio <= MAX_ANNOTATION_OVERLAP_RATIO
        ):
            return row.rect
    scored.sort(key=lambda row: row.total)
    return scored[0].rect


def choose_material_box_rect(
    page: fitz.Page,
    lines: list[str],
    display_space: bool = False,
    preferred_font_size: float | None = None,
) -> fitz.Rect:
    width, height, _, _ = _placement_box_metrics_for_font(
        page,
        lines,
        display_space=display_space,
        font_size=preferred_font_size,
    )
    margin_x = max(14.0, page.rect.width * 0.014)
    margin_y = max(18.0, page.rect.height * 0.018)
    if not display_space and page.rotation in {90, 270}:
        margin_y = max(margin_y, page.mediabox.height * 0.02)

    candidates = _candidate_rects(
        page,
        width,
        height,
        margin_x,
        margin_y,
        display_space=display_space,
        vertical_preference="bottom",
    )
    density_image = _render_page_for_density(page)
    scale = density_image.width / page.rect.width if page.rect.width else 0.12
    text_blocks = _page_text_rects(page)
    annotation_blocks = _page_annotation_rects(page)
    scored = [
        _placement_score(
            density_image,
            page,
            candidate,
            scale,
            text_blocks,
            annotation_blocks,
            position_penalty=_material_position_preference_penalty,
        )
        for candidate in candidates
    ]
    for row in scored:
        if (
            page.rect.contains(row.rect)
            and row.density <= MAX_ACCEPTABLE_DENSITY
            and row.text_overlap_ratio <= MAX_TEXT_OVERLAP_RATIO
            and row.annotation_overlap_ratio <= MAX_ANNOTATION_OVERLAP_RATIO
        ):
            return row.rect
    scored.sort(key=lambda row: row.total)
    return scored[0].rect


def _candidate_rects(
    page: fitz.Page,
    width: float,
    height: float,
    margin_x: float,
    margin_y: float,
    display_space: bool = False,
    vertical_preference: str = "top",
) -> list[fitz.Rect]:
    max_x = max(margin_x, page.rect.width - margin_x - width)
    if vertical_preference == "bottom":
        y_rows = _bottom_section_y_rows(page, height, margin_y, display_space=display_space)
    else:
        y_rows = _top_section_y_rows(page, height, margin_y, display_space=display_space)
    # x-major order: the whole left column is preferred before any right
    # candidate (see choose_box_rect acceptance loop).
    return [
        fitz.Rect(x, y, x + width, y + height)
        for x in (margin_x, max_x)
        for y in y_rows
    ]


def _top_section_y_rows(
    page: fitz.Page, height: float, margin_y: float, display_space: bool = False
) -> list[float]:
    max_y = max(margin_y, page.rect.height - margin_y - height)
    if not display_space and page.rotation in {90, 180, 270}:
        band_start = max(margin_y, page.rect.height * 0.7 - height)
        top_edge = max_y
        mid = band_start + max(0.0, (top_edge - band_start) / 2)
        return _unique_positions([top_edge, mid, band_start])

    band_end = max(margin_y, min(max_y, page.rect.height * 0.3 - height))
    mid = margin_y + max(0.0, (band_end - margin_y) / 2)
    return _unique_positions([margin_y, mid, band_end])


def _bottom_section_y_rows(
    page: fitz.Page, height: float, margin_y: float, display_space: bool = False
) -> list[float]:
    max_y = max(margin_y, page.rect.height - margin_y - height)
    if not display_space and page.rotation in {90, 180, 270}:
        band_end = max(margin_y, min(max_y, page.rect.height * 0.3 - height))
        mid = margin_y + max(0.0, (band_end - margin_y) / 2)
        return _unique_positions([margin_y, mid, band_end])
    band_start = max(margin_y, page.rect.height * 0.7 - height)
    mid = band_start + max(0.0, (max_y - band_start) / 2)
    return _unique_positions([max_y, mid, band_start])


def _unique_positions(values: list[float]) -> list[float]:
    rows: list[float] = []
    for value in values:
        rounded = round(value, 3)
        if rounded not in rows:
            rows.append(rounded)
    return rows


def _page_text_rects(page: fitz.Page) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    for raw in page.get_text("blocks", sort=False):
        x0, y0, x1, y1, text, *_ = raw
        if text.strip():
            rects.append(fitz.Rect(x0, y0, x1, y1))
    return rects


def _page_annotation_rects(page: fitz.Page) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    for annot in page.annots() or []:
        rects.append(fitz.Rect(annot.rect))

    page_area = max(_rect_area(page.rect), 1.0)
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing.get("rect") or fitz.Rect())
        if rect.is_empty:
            continue
        area_ratio = _rect_area(rect) / page_area
        fill = drawing.get("fill")
        # Only FILLED shapes in Telcyte markup colors (red stamps, green
        # callout boxes) block placement. Stroke-only drawings are route /
        # plat-boundary lines whose bounding rect is mostly empty space, and
        # blue-dominant fills are Cox base-design labels - Nick's team
        # covers both freely (BI-945043 snips, 2026-06-10).
        if fill and _is_markup_fill(fill) and area_ratio <= 0.35:
            rects.append(rect)
    return rects


def _is_markup_fill(color: tuple[float, ...]) -> bool:
    if len(color) < 3 or not _is_colored_markup(color):
        return False
    r, g, b = color[:3]
    # Blue-dominant fills are base-design (Cox) text/labels, not markup.
    return not (b > r + 0.1 and b > g + 0.1)


def _is_colored_markup(color: tuple[float, ...]) -> bool:
    if len(color) < 3:
        return False
    r, g, b = color[:3]
    return max(r, g, b) - min(r, g, b) > 0.18


def _placement_score(
    img: Image.Image,
    page: fitz.Page,
    candidate: fitz.Rect,
    scale: float,
    text_blocks: list[fitz.Rect],
    annotation_blocks: list[fitz.Rect],
    position_penalty=None,
) -> PlacementScore:
    if position_penalty is None:
        position_penalty = _position_preference_penalty
    rect_area = max(_rect_area(candidate), 1.0)
    overlap_area = 0.0
    for block in text_blocks:
        overlap = candidate & block
        if not overlap.is_empty:
            overlap_area += _rect_area(overlap)
    overlap_ratio = min(1.0, overlap_area / rect_area)
    annotation_overlap_area = 0.0
    for block in annotation_blocks:
        overlap = candidate & block
        if not overlap.is_empty:
            annotation_overlap_area += _rect_area(overlap)
    annotation_overlap_ratio = min(1.0, annotation_overlap_area / rect_area)
    density = _ink_density(img, page, candidate, scale)
    off_page_penalty = 10.0 if not page.rect.contains(candidate) else 0.0
    right_side_penalty = RIGHT_SIDE_PENALTY if candidate.x0 > page.rect.width / 2 else 0.0
    total = (
        off_page_penalty
        + density
        + overlap_ratio * 2.0
        + annotation_overlap_ratio * 4.0
        + position_penalty(page, candidate)
        + right_side_penalty
    )
    return PlacementScore(
        total=total,
        density=density,
        text_overlap_ratio=overlap_ratio,
        annotation_overlap_ratio=annotation_overlap_ratio,
        rect=candidate,
    )


def _position_preference_penalty(page: fitz.Page, candidate: fitz.Rect) -> float:
    if candidate.y0 <= page.rect.height * 0.12:
        return 0.0
    if candidate.y0 <= page.rect.height * 0.6:
        return 0.2
    return 0.35


def _material_position_preference_penalty(page: fitz.Page, candidate: fitz.Rect) -> float:
    if candidate.y1 >= page.rect.height * 0.88:
        return 0.0
    if candidate.y1 >= page.rect.height * 0.4:
        return 0.2
    return 0.35


def _rect_area(rect: fitz.Rect) -> float:
    return max(0.0, rect.width) * max(0.0, rect.height)


def annotate_pdf(pdf_bytes: bytes, summary: SummaryResult, source_name: str | None = None) -> bytes:
    lines = summary.totals_box_lines()
    material_lines = summary.material_box_lines()
    new_total_lines = summary.new_totals_box_lines()
    touched_xrefs: set[int] = set()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[0]
        existing_material_boxes = _existing_material_boxes(page)
        existing_material_rows = [
            row
            for box in existing_material_boxes
            for row in extract_material_rows(box.content)
        ]
        summary.final_material_rows = list(existing_material_rows)
        should_update_material_box = bool(material_lines) or bool(
            existing_material_boxes and summary.cable_footage
        )
        force_summary_stream = should_update_material_box
        existing_boxes = _existing_job_totals_boxes(page)
        totals_font_size: float | None = None
        if new_total_lines:
            totals_font_size = None
        elif existing_boxes:
            replacement = existing_boxes[0]
            _record_replaced_total_deltas(summary, replacement.content)
            _delete_annotations_by_xref(page, [box.xref for box in existing_boxes])
            added = _add_summary_annotation(
                page,
                _replacement_rect_for_content(page, replacement.rect, lines, replacement.font_size),
                lines,
                preferred_font_size=replacement.font_size,
                force_custom_stream=force_summary_stream,
            )
            totals_font_size = added.font_size
            touched_xrefs.add(added.xref)
        else:
            rect = choose_box_rect(page, lines)
            # Single FreeText annotation: movable in PDF editors, with no
            # baked page-content copy underneath. Dual rendering caused the
            # "duplicate box when dragged" bug and the Adobe-red /
            # Nitro-black mismatch (Nick Evans email, 2026-06-09, BI-304069).
            # Rotated sheets deliberately stay in this annotation path: NR-1138768
            # had a working movable /Rotate 90 FreeText box, while baking made
            # the new box invisible to Adobe's Comments pane and impossible to drag.
            added = _add_summary_annotation(
                page,
                rect,
                lines,
                force_custom_stream=force_summary_stream,
            )
            totals_font_size = added.font_size
            touched_xrefs.add(added.xref)

        if new_total_lines:
            _stamp_new_totals(page, new_total_lines, touched_xrefs)

        if should_update_material_box:
            if existing_material_boxes:
                material_replacement = existing_material_boxes[0]
                computed_material_rows = [line.strip() for line in summary.materials if line.strip()]
                merged_material_rows = merge_material_rows(existing_material_rows, computed_material_rows)
                summary.final_material_rows = list(merged_material_rows)
                merged_material_lines = ["Materials", *merged_material_rows]
                _record_material_merge_note(summary, existing_material_rows, merged_material_rows)
                _delete_annotations_by_xref(page, [box.xref for box in existing_material_boxes])
                added = _add_material_annotation(
                    page,
                    _replacement_rect_for_content(
                        page,
                        material_replacement.rect,
                        merged_material_lines,
                        totals_font_size,
                    ),
                    merged_material_lines,
                    preferred_font_size=totals_font_size,
                )
                touched_xrefs.add(added.xref)
            else:
                summary.final_material_rows = [line.strip() for line in summary.materials if line.strip()]
                material_rect = choose_material_box_rect(
                    page,
                    material_lines,
                    preferred_font_size=totals_font_size,
                )
                added = _add_material_annotation(
                    page,
                    material_rect,
                    material_lines,
                    preferred_font_size=totals_font_size,
                )
                touched_xrefs.add(added.xref)

        # Per-page "MKR Page Totals" boxes for multi-page as-builts. Page 1 (above)
        # keeps the Job Totals + Materials boxes; every later page with billing
        # codes gets its own page-totals box.
        if not new_total_lines:
            _stamp_page_totals(doc, summary, touched_xrefs)

        buffer = BytesIO()
        _force_tracked_output_box_appearances(doc, touched_xrefs)
        doc.save(buffer, garbage=4, deflate=True)
        return buffer.getvalue()
    finally:
        doc.close()


def _stamp_page_totals(doc: fitz.Document, summary: SummaryResult, touched_xrefs: set[int]) -> None:
    """Stamp an "MKR Page Totals" box (billing codes only) on each page after the
    first that carries per-page totals.

    Page 1 keeps the Job Totals box (all pages + materials); page totals are never
    stamped there. Existing page-totals boxes on a page are replaced so re-runs
    stay idempotent. Reuses the page-1 placement and rendering path, so rotated
    sheets (e.g. NR-996825 rot=270) are handled identically and the box stays a
    single movable FreeText annotation. Placement always succeeds: choose_box_rect
    falls back to the least-busy candidate (exactly like the page-1 box), so there
    is no per-page skip path.
    """
    if not summary.page_totals:
        return
    for idx in range(1, doc.page_count):
        lines = summary.page_totals_box_lines(idx + 1)  # page_totals keyed 1-based
        if not lines:
            continue
        page = doc[idx]
        existing = _existing_page_totals_boxes(page)
        if existing:
            replacement = existing[0]
            _delete_annotations_by_xref(page, [box.xref for box in existing])
            added = _add_summary_annotation(
                page,
                _replacement_rect_for_content(page, replacement.rect, lines, replacement.font_size),
                lines,
                preferred_font_size=replacement.font_size,
            )
            touched_xrefs.add(added.xref)
        else:
            added = _add_summary_annotation(page, choose_box_rect(page, lines), lines)
            touched_xrefs.add(added.xref)


def _stamp_new_totals(page: fitz.Page, lines: list[str], touched_xrefs: set[int]) -> None:
    existing = _existing_tool_new_totals_boxes(page)
    if existing:
        replacement = existing[0]
        _delete_annotations_by_xref(page, [box.xref for box in existing])
        added = _add_new_totals_annotation(
            page,
            _new_totals_rect(
                page,
                _replacement_rect_for_content(page, replacement.rect, lines, replacement.font_size),
            ),
            lines,
            preferred_font_size=replacement.font_size,
        )
        touched_xrefs.add(added.xref)
        return
    added = _add_new_totals_annotation(page, _new_totals_rect(page, choose_box_rect(page, lines)), lines)
    touched_xrefs.add(added.xref)


def _new_totals_rect(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect:
    if page.rotation not in {90, 270}:
        return rect
    bounds = _annotation_bounds(page)
    min_width = min(bounds.width * 0.32, 230.0)
    if rect.width >= min_width:
        return _clamp_annotation_rect(page, rect)
    expanded = fitz.Rect(rect)
    expanded.x1 = expanded.x0 + min_width
    return _clamp_annotation_rect(page, expanded)


def _existing_job_totals_boxes(page: fitz.Page) -> list[ExistingTotalsBox]:
    return _existing_output_boxes(
        page,
        lambda content: starts_with_job_totals_title(content) and not is_previously_billed_totals_box(content),
    )


def _existing_page_totals_boxes(page: fitz.Page) -> list[ExistingTotalsBox]:
    return _existing_output_boxes(
        page,
        lambda content: starts_with_page_totals_title(content) and not is_previously_billed_totals_box(content),
    )


def _existing_tool_new_totals_boxes(page: fitz.Page) -> list[ExistingTotalsBox]:
    return _existing_output_boxes(page, is_tool_new_totals_box)


def _existing_material_boxes(page: fitz.Page) -> list[ExistingTotalsBox]:
    return _existing_output_boxes(page, starts_with_materials_title)


def _existing_output_boxes(page: fitz.Page, predicate) -> list[ExistingTotalsBox]:
    boxes: list[ExistingTotalsBox] = []
    doc = page.parent
    for annot in page.annots() or []:
        content = str((annot.info or {}).get("content") or "").replace("\r", "\n")
        if not predicate(content):
            continue
        boxes.append(
            ExistingTotalsBox(
                xref=annot.xref,
                rect=fitz.Rect(annot.rect),
                content=content,
                font_size=_annotation_font_size(doc, annot.xref),
            )
        )
    boxes.sort(key=lambda box: (box.rect.x0, box.rect.y0))
    return boxes


def _replacement_rect_for_content(
    page: fitz.Page,
    anchor: fitz.Rect,
    lines: list[str],
    preferred_font_size: float | None = None,
) -> fitz.Rect:
    """Keep the old box's POSITION, but size it to fit the new content.

    Reuses the exact rotation-aware sizing of a fresh stamp
    (_placement_box_metrics_for_font, which swaps width/height on 90/270 sheets),
    so a re-stamp and a fresh stamp produce identically-shaped boxes. The old box
    location is the re-run signal; its stale dimensions are not. The previous code
    returned the stale rectangle verbatim on rotated pages, which kept a too-narrow
    box and wrapped the longer "MKR Page Totals" title onto two lines (NR-996825,
    rot=270). On non-rotated pages _placement_box_metrics_for_font returns the same
    dimensions as the old _box_metrics_for_font call, so that path is unchanged.
    """
    width, height, _, _ = _placement_box_metrics_for_font(page, lines, font_size=preferred_font_size)
    rect = fitz.Rect(anchor.x0, anchor.y0, anchor.x0 + width, anchor.y0 + height)
    return _clamp_annotation_rect(page, rect)


def _annotation_bounds(page: fitz.Page) -> fitz.Rect:
    bounds = fitz.Rect(page.rect) * page.derotation_matrix
    bounds.normalize()
    return bounds


def _clamp_annotation_rect(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect:
    bounds = _annotation_bounds(page)
    out = fitz.Rect(rect)
    if out.width > bounds.width:
        out.x0, out.x1 = bounds.x0, bounds.x1
    else:
        if out.x0 < bounds.x0:
            out.x1 += bounds.x0 - out.x0
            out.x0 = bounds.x0
        if out.x1 > bounds.x1:
            out.x0 -= out.x1 - bounds.x1
            out.x1 = bounds.x1
    if out.height > bounds.height:
        out.y0, out.y1 = bounds.y0, bounds.y1
    else:
        if out.y0 < bounds.y0:
            out.y1 += bounds.y0 - out.y0
            out.y0 = bounds.y0
        if out.y1 > bounds.y1:
            out.y0 -= out.y1 - bounds.y1
            out.y1 = bounds.y1
    out.x0 = max(bounds.x0, out.x0)
    out.y0 = max(bounds.y0, out.y0)
    out.x1 = min(bounds.x1, out.x1)
    out.y1 = min(bounds.y1, out.y1)
    return out


def _box_metrics_for_font(
    page: fitz.Page,
    lines: list[str],
    font_size: float | None = None,
) -> tuple[float, float, float, float]:
    if font_size is None:
        return _box_metrics(page, lines)
    line_height = font_size * 1.18
    padding = font_size * 0.5
    longest = max(
        (fitz.get_text_length(line, fontname="helv", fontsize=font_size) for line in lines),
        default=font_size * 10,
    )
    width = min(page.rect.width * 0.34, longest + padding * 2 + font_size * 0.8)
    max_text_width = max(font_size * 4, width - padding * 2)
    rendered_count = sum(len(_wrap_line(line, max_text_width, font_size)) for line in lines)
    height = min(page.rect.height * 0.82, rendered_count * line_height + padding * 2 + font_size * 0.4)
    return width, height, font_size, padding


def _annotation_font_size(doc: fitz.Document, xref: int) -> float | None:
    da = doc.xref_get_key(xref, "DA")[1] or ""
    matches = re.findall(r"([-+]?\d+(?:\.\d+)?)\s+Tf\b", da)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def _delete_annotations_by_xref(page: fitz.Page, xrefs: list[int]) -> None:
    for xref in xrefs:
        for annot in page.annots() or []:
            if annot.xref == xref:
                page.delete_annot(annot)
                break


def _record_replaced_total_deltas(summary: SummaryResult, old_content: str) -> None:
    old_totals = _totals_by_code(old_content.splitlines())
    new_totals = _totals_by_code(summary.job_totals)
    deltas: list[str] = []
    for key in sorted(set(old_totals) & set(new_totals)):
        old_qty, old_line = old_totals[key]
        new_qty, new_line = new_totals[key]
        if old_qty != new_qty:
            deltas.append(f"previous box showed {old_line}; recomputed drawing total is {new_line}")
    if not deltas:
        return
    note = "Replaced an existing totals box: " + "; ".join(deltas[:4]) + "."
    if len(deltas) > 4:
        note += f" Plus {len(deltas) - 4} more changed totals."
    if note not in summary.informational_notes:
        summary.informational_notes.append(note)


def _record_material_merge_note(
    summary: SummaryResult,
    existing_rows: list[str],
    merged_rows: list[str],
) -> None:
    old_by_key = {
        key: row
        for row in existing_rows
        if (key := material_row_key(row))
    }
    new_by_key = {
        key: row
        for row in merged_rows
        if (key := material_row_key(row))
    }
    changed: list[str] = []
    changed_keys: list[str] = []
    added: list[str] = []
    added_keys: list[str] = []
    for key, new_row in sorted(new_by_key.items()):
        old_row = old_by_key.get(key)
        if old_row and old_row != new_row:
            changed.append(f"{old_row} -> {new_row}")
            changed_keys.append(key)
        elif not old_row:
            added.append(new_row)
            added_keys.append(key)

    preserved_count = sum(1 for row in existing_rows if not material_row_key(row))
    parts: list[str] = []
    if changed:
        label = "cable material" if all(key.startswith("cable:") for key in changed_keys) else "material"
        parts.append(f"normalized {len(changed)} {label} row(s)")
    if added:
        label = "cable footage" if all(key.startswith("cable:") for key in added_keys) else "material"
        parts.append(f"added {label} " + "; ".join(added))
    if preserved_count:
        parts.append(f"kept {preserved_count} existing material line(s)")
    if not parts:
        return

    note = "Updated the existing Materials box: " + "; ".join(parts) + "."
    if note not in summary.informational_notes:
        summary.informational_notes.append(note)


def _totals_by_code(lines: list[str]) -> dict[tuple[str, str], tuple[str, str]]:
    totals: dict[tuple[str, str], tuple[str, str]] = {}
    for line in lines:
        parsed = total_line_key(line)
        if not parsed:
            continue
        code, qty, _unit = parsed
        totals[code] = (qty, line.strip())
    return totals


def _repair_freetext_appearance(
    page: fitz.Page,
    annot: fitz.Annot,
    rect: fitz.Rect,
    rendered_lines: list[str] | None = None,
    font_size: float | None = None,
    text_color: tuple[float, float, float] = TEXT_RED,
    border_color: tuple[float, float, float] = TEXT_RED,
    fill_color: tuple[float, float, float] = BOX_FILL,
    force_custom_stream: bool = False,
) -> None:
    """Work around PyMuPDF 1.25.x FreeText appearance defects.

    PyMuPDF writes the appearance-stream /BBox in page coordinates while the
    stream content draws from the origin, so viewers that honor the /BBox clip
    (including MuPDF itself) show an empty box; viewers that regenerate the
    appearance from /DA show their own styling instead. This was the root
    cause of the red-in-Adobe / black-in-Nitro mismatch (Nick Evans email,
    2026-06-09). It also emits an unrequested /CL callout line. Rewriting the
    /BBox to origin and dropping /CL makes the single authored appearance
    render identically everywhere.
    """
    doc = page.parent
    ap_ref = doc.xref_get_key(annot.xref, "AP")[1] or ""
    match = re.search(r"(\d+) 0 R", ap_ref)
    if match:
        ap_xref = int(match.group(1))
        if force_custom_stream and rendered_lines and font_size:
            _write_freetext_appearance(
                page,
                ap_xref,
                rect,
                rendered_lines,
                font_size,
                text_color=text_color,
                border_color=border_color,
                fill_color=fill_color,
            )
        else:
            doc.xref_set_key(ap_xref, "BBox", f"[0 0 {_pdf_num(rect.width)} {_pdf_num(rect.height)}]")
    if (doc.xref_get_key(annot.xref, "CL")[1] or "null") != "null":
        doc.xref_set_key(annot.xref, "CL", "null")


def _add_summary_annotation(
    page: fitz.Page,
    rect: fitz.Rect,
    lines: list[str],
    preferred_font_size: float | None = None,
    force_custom_stream: bool = False,
) -> AddedFreeTextBox:
    return _add_freetext_box(
        page,
        rect,
        lines,
        preferred_font_size=preferred_font_size,
        text_color=TEXT_RED,
        border_color=TEXT_RED,
        fill_color=BOX_FILL,
        force_custom_stream=force_custom_stream or page.rotation != 0,
    )


def _force_tracked_output_box_appearances(doc: fitz.Document, touched_xrefs: set[int]) -> None:
    if not touched_xrefs:
        return
    for page in doc:
        for annot in page.annots() or []:
            if annot.type[1] != "FreeText":
                continue
            content = str((annot.info or {}).get("content") or "").replace("\r", "\n")
            if annot.xref not in touched_xrefs and not is_tool_new_totals_box(content):
                continue
            lines = [line for line in content.splitlines() if line.strip()]
            if not lines:
                continue
            text_color, border_color, fill_color = _style_for_output_box(content)
            font_size = _annotation_font_size(doc, annot.xref)
            rendered_lines, font_size, _ = _summary_rendering(page, annot.rect, lines, font_size=font_size)
            _repair_freetext_appearance(
                page,
                annot,
                annot.rect,
                rendered_lines,
                font_size,
                text_color=text_color,
                border_color=border_color,
                fill_color=fill_color,
                force_custom_stream=True,
            )
            _set_editor_text_style(page, annot, rendered_lines, font_size, text_color)
            _pin_annotation_orientation(page, annot)


def _style_for_output_box(content: str) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    if starts_with_materials_title(content):
        return MATERIAL_TEXT, MATERIAL_BORDER_BLUE, BOX_FILL
    if is_tool_new_totals_box(content):
        return TEXT_RED, TEXT_RED, NEW_TOTALS_FILL
    return TEXT_RED, TEXT_RED, BOX_FILL


def _add_material_annotation(
    page: fitz.Page,
    rect: fitz.Rect,
    lines: list[str],
    preferred_font_size: float | None = None,
) -> AddedFreeTextBox:
    return _add_freetext_box(
        page,
        rect,
        lines,
        preferred_font_size=preferred_font_size,
        text_color=MATERIAL_TEXT,
        border_color=MATERIAL_BORDER_BLUE,
        fill_color=BOX_FILL,
        force_custom_stream=True,
    )


def _add_new_totals_annotation(
    page: fitz.Page,
    rect: fitz.Rect,
    lines: list[str],
    preferred_font_size: float | None = None,
) -> AddedFreeTextBox:
    return _add_freetext_box(
        page,
        rect,
        lines,
        preferred_font_size=preferred_font_size,
        text_color=TEXT_RED,
        border_color=TEXT_RED,
        fill_color=NEW_TOTALS_FILL,
        force_custom_stream=True,
    )


def _add_freetext_box(
    page: fitz.Page,
    rect: fitz.Rect,
    lines: list[str],
    preferred_font_size: float | None = None,
    text_color: tuple[float, float, float] = TEXT_RED,
    border_color: tuple[float, float, float] = TEXT_RED,
    fill_color: tuple[float, float, float] = BOX_FILL,
    force_custom_stream: bool = False,
) -> AddedFreeTextBox:
    logical_lines = [line for line in lines if line.strip()]
    rendered_lines, font_size, _ = _summary_rendering(page, rect, lines, font_size=preferred_font_size)
    annot = page.add_freetext_annot(
        rect,
        "\n".join(logical_lines),
        fontsize=font_size,
        fontname="helv",
        text_color=text_color,
        fill_color=fill_color,
        rotate=_annotation_rotation(page),
    )
    # Border = yes, size 2 (Nick Evans email, 2026-06-09). FreeText supports
    # base-14 fonts only, so Helvetica (metrically identical to Arial) is used;
    # text color lives in /DA so all viewers (Adobe, Nitro) render it red.
    annot.set_border(width=BORDER_WIDTH)
    update_kwargs = {
        "fontname": "helv",
        "fontsize": font_size,
        "text_color": text_color,
        "fill_color": fill_color,
    }
    if page.rotation == 0:
        update_kwargs["border_color"] = border_color
    try:
        annot.update(**update_kwargs)
    except ValueError:
        update_kwargs.pop("border_color", None)
        annot.update(**update_kwargs)
    _repair_freetext_appearance(
        page,
        annot,
        rect,
        rendered_lines,
        font_size,
        text_color=text_color,
        border_color=border_color,
        fill_color=fill_color,
        force_custom_stream=force_custom_stream,
    )
    _set_editor_text_style(page, annot, rendered_lines, font_size, text_color)
    _pin_annotation_orientation(page, annot)
    return AddedFreeTextBox(xref=annot.xref, font_size=font_size)


def _write_freetext_appearance(
    page: fitz.Page,
    ap_xref: int,
    rect: fitz.Rect,
    lines: list[str],
    font_size: float,
    text_color: tuple[float, float, float] = TEXT_RED,
    border_color: tuple[float, float, float] = TEXT_RED,
    fill_color: tuple[float, float, float] = BOX_FILL,
) -> None:
    doc = page.parent
    rotation = _annotation_rotation(page)
    if rotation in {90, 270}:
        bbox_width = rect.height
        bbox_height = rect.width
    else:
        bbox_width = rect.width
        bbox_height = rect.height

    if rotation == 90:
        matrix = [0, 1, -1, 0, rect.width, 0]
    elif rotation == 270:
        matrix = [0, -1, 1, 0, 0, rect.height]
    elif rotation == 180:
        matrix = [-1, 0, 0, -1, rect.width, rect.height]
    else:
        matrix = [1, 0, 0, 1, 0, 0]

    doc.xref_set_key(ap_xref, "BBox", f"[0 0 {_pdf_num(bbox_width)} {_pdf_num(bbox_height)}]")
    doc.xref_set_key(ap_xref, "Matrix", "[" + " ".join(_pdf_num(value) for value in matrix) + "]")
    doc.xref_set_key(
        ap_xref,
        "Resources",
        "<< /Font << /Helv << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>",
    )
    doc.update_stream(
        ap_xref,
        _freetext_appearance_stream(
            bbox_width,
            bbox_height,
            lines,
            font_size,
            text_color=text_color,
            border_color=border_color,
            fill_color=fill_color,
        ).encode("latin-1"),
    )


def _freetext_appearance_stream(
    width: float,
    height: float,
    lines: list[str],
    font_size: float,
    text_color: tuple[float, float, float] = TEXT_RED,
    border_color: tuple[float, float, float] = TEXT_RED,
    fill_color: tuple[float, float, float] = BOX_FILL,
) -> str:
    padding = max(4.0, font_size * 0.18)
    line_height = font_size * 1.16
    x = padding
    y = max(font_size, height - padding - font_size * 1.05)
    fill = " ".join(_pdf_num(v) for v in fill_color)
    border = " ".join(_pdf_num(v) for v in border_color)
    text = " ".join(_pdf_num(v) for v in text_color)
    box_w = max(1.0, width - 2.0)
    box_h = max(1.0, height - 2.0)
    rows = [
        f"{fill} rg",
        f"{border} RG",
        f"{_pdf_num(BORDER_WIDTH)} w",
        f"1 1 {_pdf_num(box_w)} {_pdf_num(box_h)} re",
        "B",
        f"{text} rg",
        "BT",
        f"/Helv {_pdf_num(font_size)} Tf",
        f"{_pdf_num(x)} {_pdf_num(y)} Td",
        f"{_pdf_num(line_height)} TL",
    ]
    for index, line in enumerate(lines):
        operator = "Tj" if index == 0 else "'"
        rows.append(f"{_pdf_literal(line)} {operator}")
    rows.append("ET")
    return "\n".join(rows) + "\n"


def _pdf_literal(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    escaped = escaped.replace("\r", " ").replace("\n", " ")
    return f"({escaped})"


def _pdf_num(value: float) -> str:
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _add_baked_summary(page: fitz.Page, rect_display: fitz.Rect, lines: list[str]) -> None:
    """Stamp the totals box as page content on rotated sheets.

    ``rect_display`` is in the viewer's (display) coordinate system; the
    Shape/insert APIs work in unrotated page space, so the rect is mapped
    through the derotation matrix before drawing (NR-1138768 follow-up,
    2026-06-11: the box landed mid-sheet without this mapping).
    """
    page_rect = fitz.Rect(rect_display) * page.derotation_matrix
    page_rect.normalize()
    page.draw_rect(page_rect, color=TEXT_RED, fill=BOX_FILL, width=BORDER_WIDTH, overlay=True)

    style = TextStyle(size=10.0, color=TEXT_RED, bold_narrow=True)
    font_file = _font_file(style)
    font_kwargs: dict = {"fontname": "helv"}
    if font_file:
        font_kwargs = {"fontname": _font_name(style), "fontfile": str(font_file)}

    _, _, font_size, _ = _box_metrics(page, lines)
    for _ in range(10):
        padding = font_size * 0.5
        max_width = max(font_size * 4, rect_display.width - padding * 2)
        max_lines = max(1, int((rect_display.height - padding * 2) / (font_size * 1.18)))
        rendered: list[str] = []
        for line in lines:
            rendered.extend(_wrap_line(line, max_width, font_size))
        inset = fitz.Rect(
            page_rect.x0 + padding, page_rect.y0 + padding,
            page_rect.x1 - padding, page_rect.y1 - padding,
        )
        leftover = page.insert_textbox(
            inset,
            "\n".join(rendered),
            fontsize=font_size,
            color=TEXT_RED,
            rotate=_annotation_rotation(page),
            overlay=True,
            **font_kwargs,
        )
        if leftover >= 0 and len(rendered) <= max_lines:
            return
        font_size *= 0.88


def _pin_annotation_orientation(page: fitz.Page, annot: fitz.Annot) -> None:
    """Set the NoRotate flag on unrotated pages.

    Nick reports his PDF editor auto-rotates the totals box (and drops to
    black text) when dragging/copy-pasting on some permit drawings
    (2026-06-11). NoRotate (PDF annotation flag bit 5) is the standard way
    to declare that an annotation keeps its own orientation. Only applied
    when the page itself is unrotated - on rotated sheets our /Rotate key
    keeps the text upright and must stay in charge.
    """
    if page.rotation == 0:
        annot.set_flags(annot.flags | fitz.PDF_ANNOT_IS_NO_ROTATE)


def _set_editor_text_style(
    page: fitz.Page,
    annot: fitz.Annot,
    lines: list[str],
    font_size: float,
    text_color: tuple[float, float, float],
) -> None:
    """Keep the text color when a PDF editor regenerates the appearance.

    Editors (Nitro, Bluebeam, Adobe) rebuild a FreeText appearance from /DA,
    /DS (and /RC if present) when the box is moved or edited. PyMuPDF writes /DA with a
    nonstandard lowercase font name and no /DS or /RC, so some editors fell
        back to black text on move (Nick, BI-945043, 2026-06-10). Write all three
        in standard form with the intended color.
    """
    if page.rotation != 0:
        return
    doc = page.parent
    size = round(float(font_size), 2)
    doc.xref_set_key(annot.xref, "DA", fitz.get_pdf_str(f"/Helv {size} Tf {_rgb_operator(text_color)} rg"))
    doc.xref_set_key(
        annot.xref,
        "DS",
        fitz.get_pdf_str(f"font: {size}pt Helvetica, sans-serif; color:{_rgb_hex(text_color)}"),
    )
    # Deliberately NO /RC: editors prefer rich content over the plain
    # Contents when regenerating a moved box, and they join its paragraphs
    # into one run-on line (Javier drag test, 2026-06-10). With only the
    # standard-form red /DA and /DS, editors rebuild from Contents and the
    # line breaks survive the move.
    if (doc.xref_get_key(annot.xref, "RC")[1] or "null") != "null":
        doc.xref_set_key(annot.xref, "RC", "null")


def _rgb_operator(color: tuple[float, float, float]) -> str:
    return " ".join(_pdf_num(value) for value in color)


def _rgb_hex(color: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(value * 255))):02X}" for value in color[:3])


def _summary_rendering(
    page: fitz.Page,
    rect: fitz.Rect,
    lines: list[str],
    font_size: float | None = None,
) -> tuple[list[str], float, float]:
    _, _, default_font_size, _ = _box_metrics(page, lines)
    font_size = font_size or default_font_size
    for _ in range(16):
        padding = font_size * 0.5
        text_width, text_height = _annotation_text_space(page, rect)
        line_height = font_size * 1.16
        max_lines = max(1, int((text_height - padding * 2) / line_height))
        max_width = max(font_size * 4, text_width - padding * 2)
        rendered_lines: list[str] = []
        for line in lines:
            rendered_lines.extend(_wrap_line(line, max_width, font_size))
        if len(rendered_lines) <= max_lines or font_size <= 8.01:
            return rendered_lines, font_size, padding
        font_size = max(8.0, font_size * 0.9)
    return rendered_lines, font_size, padding


def _annotation_rotation(page: fitz.Page) -> int:
    return page.rotation if page.rotation in {90, 180, 270} else 0


def _annotation_text_space(page: fitz.Page, rect: fitz.Rect) -> tuple[float, float]:
    if page.rotation in {90, 270}:
        return rect.height, rect.width
    return rect.width, rect.height


def _wrap_line(line: str, max_width: float, font_size: float) -> list[str]:
    """Wrap by measured text width (same metrics as _box_metrics) so lines
    that fit the computed box are never wrapped or truncated."""

    def _fits(text: str) -> bool:
        return fitz.get_text_length(text, fontname="helv", fontsize=font_size) <= max_width

    if _fits(line):
        return [line]
    words = line.split()
    rows: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if _fits(candidate):
            current = candidate
        else:
            if current:
                rows.append(current)
            current = word
    if current:
        rows.append(current)
    return rows


def _insert_text(page: fitz.Page, point: tuple[float, float], text: str, style: TextStyle) -> None:
    kwargs = {
        "fontsize": style.size,
        "color": style.color,
        "overlay": True,
        "rotate": style.rotate,
    }
    font_file = _font_file(style)
    if font_file:
        kwargs.update(
            {
                "fontname": _font_name(style),
                "fontfile": str(font_file),
                "set_simple": 1,
            }
        )
    else:
        kwargs["fontname"] = "helv"
    page.insert_text(point, text, **kwargs)


def _font_file(style: TextStyle) -> Path | None:
    env_var = BOLD_NARROW_FONT_ENV if style.bold_narrow else REGULAR_FONT_ENV
    candidates = [os.environ.get(env_var, "")]
    candidates.extend(BOLD_NARROW_FONT_CANDIDATES if style.bold_narrow else REGULAR_FONT_CANDIDATES)
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def _font_name(style: TextStyle) -> str:
    return "TelcyteNarrowBold" if style.bold_narrow else "TelcyteRegular"


def describe_pdf_fonts() -> dict[str, dict[str, str | bool]]:
    return {
        "regular": _font_status(TextStyle(size=10.0, color=MATERIAL_TEXT)),
        "bold_narrow": _font_status(TextStyle(size=10.0, color=TEXT_RED, bold_narrow=True)),
    }


def _font_status(style: TextStyle) -> dict[str, str | bool]:
    path = _font_file(style)
    if not path:
        return {
            "available": False,
            "exact_arial": False,
            "name": "helv",
            "source": "built-in PDF fallback",
        }
    name = path.name.lower()
    exact_arial = "arial" in name and "liberation" not in name
    return {
        "available": True,
        "exact_arial": exact_arial,
        "name": _font_name(style),
        "source": str(path),
    }
