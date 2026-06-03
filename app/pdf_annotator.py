from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path

import fitz
from PIL import Image

from app.models import SummaryResult

BOX_FILL = (0.78, 1.0, 0.63)
TEXT_RED = (1.0, 0.0, 0.0)
MATERIAL_TEXT = (0.0, 0.0, 0.0)
REGULAR_FONT_ENV = "TELCYTE_PDF_REGULAR_FONT_PATH"
BOLD_NARROW_FONT_ENV = "TELCYTE_PDF_BOLD_NARROW_FONT_PATH"
MAX_SAFE_PLACEMENT_SCORE = 1.35
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
    font_size = max(7.0, min(18.0, page.rect.width / 160))
    line_height = font_size * 1.16
    longest = max((len(line) for line in lines), default=18)
    width = max(page.rect.width * 0.105, min(page.rect.width * 0.24, longest * font_size * 0.54 + font_size * 1.8))
    height = min(page.rect.height * 0.82, len(lines) * line_height + font_size * 1.6)
    padding = font_size * 0.55
    return width, height, font_size, padding


def _placement_box_metrics(page: fitz.Page, lines: list[str]) -> tuple[float, float, float, float]:
    width, height, font_size, padding = _box_metrics(page, lines)
    if page.rotation in {90, 270}:
        return height, width, font_size, padding
    return width, height, font_size, padding


def choose_box_rect(page: fitz.Page, lines: list[str]) -> fitz.Rect:
    width, height, _, _ = _placement_box_metrics(page, lines)
    margin_x = max(14.0, page.rect.width * 0.014)
    margin_y = max(18.0, page.rect.height * 0.018)
    if page.rotation == 90:
        # Rotated PDFs report page coordinates differently from the viewer. Keep
        # candidates in visible corners and let density scoring choose the least
        # disruptive one.
        margin_y = max(margin_y, page.mediabox.height * 0.02)

    candidates = _candidate_rects(page, width, height, margin_x, margin_y)
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
    scored.sort(key=lambda row: row.total)
    return scored[0].rect


def _candidate_rects(
    page: fitz.Page,
    width: float,
    height: float,
    margin_x: float,
    margin_y: float,
) -> list[fitz.Rect]:
    max_x = max(margin_x, page.rect.width - margin_x - width)
    y_rows = _top_section_y_rows(page, height, margin_y)
    return [
        fitz.Rect(x, y, x + width, y + height)
        for y in y_rows
        for x in (margin_x, max_x)
    ]


def _top_section_y_rows(page: fitz.Page, height: float, margin_y: float) -> list[float]:
    max_y = max(margin_y, page.rect.height - margin_y - height)
    if page.rotation in {90, 180}:
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
        if _rect_area(rect) / page_area > 0.35:
            continue
        color = drawing.get("fill") or drawing.get("color")
        if color and _is_colored_markup(color):
            rects.append(rect)
    return rects


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
    total = (
        off_page_penalty
        + density
        + overlap_ratio * 2.0
        + annotation_overlap_ratio * 4.0
        + _position_preference_penalty(page, candidate)
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
        rect = choose_box_rect(page, lines)
        _add_summary_page_content(page, rect, lines)
        _add_summary_annotation(page, rect, lines)

        buffer = BytesIO()
        doc.save(buffer, garbage=4, deflate=True)
        return buffer.getvalue()
    finally:
        doc.close()


def _add_summary_annotation(page: fitz.Page, rect: fitz.Rect, lines: list[str]) -> None:
    rendered_lines, font_size, _ = _summary_rendering(page, rect, lines)
    annot = page.add_freetext_annot(
        rect,
        "\n".join(rendered_lines),
        fontsize=font_size,
        fontname="helv",
        text_color=TEXT_RED,
        fill_color=BOX_FILL,
        rotate=_annotation_rotation(page),
    )
    annot.set_border(width=0)
    annot.update(fontname="helv", fontsize=font_size, text_color=TEXT_RED, fill_color=BOX_FILL)


def _add_summary_page_content(page: fitz.Page, rect: fitz.Rect, lines: list[str]) -> None:
    page.draw_rect(rect, color=None, fill=BOX_FILL, overlay=True)
    _, _, base_font_size, _ = _box_metrics(page, lines)
    font_size = base_font_size
    for _ in range(8):
        rendered_lines, fitted_font_size, padding = _summary_rendering(page, rect, lines, font_size=font_size)
        remaining_space = page.insert_textbox(
            rect + (padding, padding, -padding, -padding),
            "\n".join(rendered_lines),
            fontsize=fitted_font_size,
            fontname="helv",
            color=TEXT_RED,
            rotate=_annotation_rotation(page),
            overlay=True,
        )
        if remaining_space >= 0:
            return
        font_size *= 0.9


def _summary_rendering(
    page: fitz.Page,
    rect: fitz.Rect,
    lines: list[str],
    font_size: float | None = None,
) -> tuple[list[str], float, float]:
    _, _, default_font_size, _ = _box_metrics(page, lines)
    font_size = font_size or default_font_size
    padding = font_size * 0.55
    text_width, text_height = _annotation_text_space(page, rect)
    line_height = font_size * 1.16
    max_lines = max(1, int((text_height - padding * 2) / line_height))
    rendered_lines: list[str] = []
    remaining = max_lines
    max_chars = max(12, int((text_width - padding * 2) / (font_size * 0.54)))
    for line in lines:
        if remaining <= 0:
            break
        wrapped = _wrap_line(line, max_chars)
        rendered_lines.extend(wrapped[:remaining])
        remaining -= len(wrapped[:remaining])
    return rendered_lines, font_size, padding


def _annotation_rotation(page: fitz.Page) -> int:
    return page.rotation if page.rotation in {90, 180, 270} else 0


def _annotation_text_space(page: fitz.Page, rect: fitz.Rect) -> tuple[float, float]:
    if page.rotation in {90, 270}:
        return rect.height, rect.width
    return rect.width, rect.height


def _wrap_line(line: str, max_chars: int) -> list[str]:
    if len(line) <= max_chars:
        return [line]
    words = line.split()
    rows: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
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
