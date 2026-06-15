from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import re

import fitz
from PIL import Image

from app.models import SummaryResult
from app.rate_cards import total_line_key

BOX_FILL = (0.78, 1.0, 0.63)
TEXT_RED = (1.0, 0.0, 0.0)
MATERIAL_TEXT = (0.0, 0.0, 0.0)
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
    height = min(page.rect.height * 0.82, len(lines) * line_height + padding * 2 + font_size * 0.4)
    return width, height, font_size, padding


def _placement_box_metrics(
    page: fitz.Page, lines: list[str], display_space: bool = False
) -> tuple[float, float, float, float]:
    width, height, font_size, padding = _box_metrics(page, lines)
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


def _candidate_rects(
    page: fitz.Page,
    width: float,
    height: float,
    margin_x: float,
    margin_y: float,
    display_space: bool = False,
) -> list[fitz.Rect]:
    max_x = max(margin_x, page.rect.width - margin_x - width)
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
) -> PlacementScore:
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
        + _position_preference_penalty(page, candidate)
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


def _rect_area(rect: fitz.Rect) -> float:
    return max(0.0, rect.width) * max(0.0, rect.height)


def annotate_pdf(pdf_bytes: bytes, summary: SummaryResult, source_name: str | None = None) -> bytes:
    lines = summary.display_lines()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[0]
        existing_boxes = _existing_totals_boxes(page)
        if existing_boxes:
            replacement = existing_boxes[0]
            _record_replaced_total_deltas(summary, replacement.content)
            _delete_annotations_by_xref(page, [box.xref for box in existing_boxes])
            _add_summary_annotation(
                page,
                replacement.rect,
                lines,
                preferred_font_size=replacement.font_size,
            )
        else:
            rect = choose_box_rect(page, lines)
            # Single FreeText annotation: movable in PDF editors, with no
            # baked page-content copy underneath. Dual rendering caused the
            # "duplicate box when dragged" bug and the Adobe-red /
            # Nitro-black mismatch (Nick Evans email, 2026-06-09, BI-304069).
            # Rotated sheets deliberately stay in this annotation path: NR-1138768
            # had a working movable /Rotate 90 FreeText box, while baking made
            # the new box invisible to Adobe's Comments pane and impossible to drag.
            _add_summary_annotation(page, rect, lines)

        buffer = BytesIO()
        doc.save(buffer, garbage=4, deflate=True)
        return buffer.getvalue()
    finally:
        doc.close()


def _existing_totals_boxes(page: fitz.Page) -> list[ExistingTotalsBox]:
    boxes: list[ExistingTotalsBox] = []
    doc = page.parent
    for annot in page.annots() or []:
        content = str((annot.info or {}).get("content") or "").replace("\r", "\n")
        if not _starts_with_totals_title(content):
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


def _starts_with_totals_title(text: str) -> bool:
    for line in text.splitlines():
        if line.strip():
            return line.strip().lower().startswith("mkr job totals")
    return False


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
        if page.rotation in {90, 180, 270} and rendered_lines and font_size:
            _write_freetext_appearance(page, ap_xref, rect, rendered_lines, font_size)
        else:
            doc.xref_set_key(ap_xref, "BBox", f"[0 0 {_pdf_num(rect.width)} {_pdf_num(rect.height)}]")
    if (doc.xref_get_key(annot.xref, "CL")[1] or "null") != "null":
        doc.xref_set_key(annot.xref, "CL", "null")


def _add_summary_annotation(
    page: fitz.Page,
    rect: fitz.Rect,
    lines: list[str],
    preferred_font_size: float | None = None,
) -> None:
    rendered_lines, font_size, _ = _summary_rendering(page, rect, lines, font_size=preferred_font_size)
    annot = page.add_freetext_annot(
        rect,
        "\n".join(rendered_lines),
        fontsize=font_size,
        fontname="helv",
        text_color=TEXT_RED,
        fill_color=BOX_FILL,
        rotate=_annotation_rotation(page),
    )
    # Border = yes, size 2 (Nick Evans email, 2026-06-09). FreeText supports
    # base-14 fonts only, so Helvetica (metrically identical to Arial) is used;
    # text color lives in /DA so all viewers (Adobe, Nitro) render it red.
    annot.set_border(width=BORDER_WIDTH)
    update_kwargs = {
        "fontname": "helv",
        "fontsize": font_size,
        "text_color": TEXT_RED,
        "fill_color": BOX_FILL,
    }
    if page.rotation == 0:
        update_kwargs["border_color"] = TEXT_RED
    try:
        annot.update(**update_kwargs)
    except ValueError:
        update_kwargs.pop("border_color", None)
        annot.update(**update_kwargs)
    _repair_freetext_appearance(page, annot, rect, rendered_lines, font_size)
    _set_editor_text_style(page, annot, rendered_lines, font_size)
    _pin_annotation_orientation(page, annot)


def _write_freetext_appearance(
    page: fitz.Page,
    ap_xref: int,
    rect: fitz.Rect,
    lines: list[str],
    font_size: float,
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
    doc.update_stream(
        ap_xref,
        _freetext_appearance_stream(bbox_width, bbox_height, lines, font_size).encode("latin-1"),
    )


def _freetext_appearance_stream(width: float, height: float, lines: list[str], font_size: float) -> str:
    padding = max(4.0, font_size * 0.18)
    line_height = font_size * 1.16
    x = padding
    y = max(font_size, height - padding - font_size * 1.05)
    fill = " ".join(_pdf_num(v) for v in BOX_FILL)
    red = " ".join(_pdf_num(v) for v in TEXT_RED)
    box_w = max(1.0, width - 2.0)
    box_h = max(1.0, height - 2.0)
    rows = [
        f"{fill} rg",
        f"{red} RG",
        f"{_pdf_num(BORDER_WIDTH)} w",
        f"1 1 {_pdf_num(box_w)} {_pdf_num(box_h)} re",
        "B",
        f"{red} rg",
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


def _set_editor_text_style(page: fitz.Page, annot: fitz.Annot, lines: list[str], font_size: float) -> None:
    """Keep the text red when a PDF editor regenerates the appearance.

    Editors (Nitro, Bluebeam, Adobe) rebuild a FreeText appearance from /DA,
    /DS (and /RC if present) when the box is moved or edited. PyMuPDF writes /DA with a
    nonstandard lowercase font name and no /DS or /RC, so some editors fell
    back to black text on move (Nick, BI-945043, 2026-06-10). Write all three
    in standard form with the red color.
    """
    doc = page.parent
    size = round(float(font_size), 2)
    doc.xref_set_key(annot.xref, "DA", fitz.get_pdf_str(f"/Helv {size} Tf 1 0 0 rg"))
    doc.xref_set_key(
        annot.xref,
        "DS",
        fitz.get_pdf_str(f"font: {size}pt Helvetica, sans-serif; color:#FF0000"),
    )
    # Deliberately NO /RC: editors prefer rich content over the plain
    # Contents when regenerating a moved box, and they join its paragraphs
    # into one run-on line (Javier drag test, 2026-06-10). With only the
    # standard-form red /DA and /DS, editors rebuild from Contents and the
    # line breaks survive the move.
    if (doc.xref_get_key(annot.xref, "RC")[1] or "null") != "null":
        doc.xref_set_key(annot.xref, "RC", "null")


def _summary_rendering(
    page: fitz.Page,
    rect: fitz.Rect,
    lines: list[str],
    font_size: float | None = None,
) -> tuple[list[str], float, float]:
    _, _, default_font_size, _ = _box_metrics(page, lines)
    font_size = font_size or default_font_size
    padding = font_size * 0.5
    text_width, text_height = _annotation_text_space(page, rect)
    line_height = font_size * 1.16
    max_lines = max(1, int((text_height - padding * 2) / line_height))
    rendered_lines: list[str] = []
    remaining = max_lines
    max_width = max(font_size * 4, text_width - padding * 2)
    for line in lines:
        if remaining <= 0:
            break
        wrapped = _wrap_line(line, max_width, font_size)
        rendered_lines.extend(wrapped[:remaining])
        remaining -= len(wrapped[:remaining])
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
