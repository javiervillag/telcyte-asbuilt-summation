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
COAX_MATERIAL_TEXT = (0.016, 0.204, 0.247)
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


@dataclass(frozen=True)
class LineBlock:
    origin: tuple[float, float]
    line_gap: float
    style: TextStyle


@dataclass(frozen=True)
class CalibratedLayout:
    totals_rect: tuple[float, float, float, float]
    materials_rect: tuple[float, float, float, float]
    fill: tuple[float, float, float]
    material_fill: tuple[float, float, float] | None
    title: LineBlock
    totals: LineBlock
    material_heading: str
    material_title: LineBlock
    materials: LineBlock
    extra_highlights: tuple[tuple[float, float, float, float], ...] = ()
    extra_highlight_overlay: bool = False
    totals_border: tuple[float, float, float] | None = None
    totals_border_width: float = 1.0
    material_border: tuple[float, float, float] | None = None
    material_border_width: float = 1.0
    annotation_moves: tuple[
        tuple[tuple[float, float, float, float], tuple[float, float, float, float]],
        ...
    ] = ()
    recreate_moved_annotations: bool = False


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


CALIBRATED_LAYOUTS: dict[str, CalibratedLayout] = {
    "RL-248790": CalibratedLayout(
        totals_rect=(23.9, 1865.6, 888.2, 2596.9),
        materials_rect=(1082.8, 1738.3, 1528.2, 2104.5),
        fill=(0.753, 1.0, 0.627),
        material_fill=None,
        title=LineBlock((65.5, 2115.8), 45.0, TextStyle(40.0, TEXT_RED, bold_narrow=True, rotate=90)),
        totals=LineBlock((155.5, 2115.8), 45.0, TextStyle(40.0, TEXT_RED, bold_narrow=True, rotate=90)),
        material_heading="Materials",
        material_title=LineBlock((1106.0, 1943.2), 26.0, TextStyle(21.82, COAX_MATERIAL_TEXT, rotate=90)),
        materials=LineBlock((1157.7, 1943.2), 26.0, TextStyle(21.82, COAX_MATERIAL_TEXT, rotate=90)),
        extra_highlights=(
            (191.4, 1529.4, 249.5, 1673.9),
            (678.7, 1395.0, 736.8, 1501.2),
            (664.3, 1576.3, 697.5, 1674.3),
            (627.7, 1294.5, 677.7, 1393.5),
            (313.3, 1577.7, 421.3, 1720.0),
        ),
        extra_highlight_overlay=True,
        annotation_moves=(
            ((193.2, 1534.5, 254.4, 1682.1), (189.9, 1527.9, 251.0, 1675.4)),
            ((670.5, 1393.5, 731.7, 1502.7), (677.2, 1393.5, 738.3, 1502.7)),
            ((614.4, 1578.1, 650.6, 1679.1), (662.8, 1574.8, 699.0, 1675.8)),
            ((609.5, 1293.5, 695.5, 1394.5), (626.2, 1293.0, 679.2, 1395.0)),
            ((316.8, 1576.4, 427.8, 1721.8), (311.8, 1576.2, 422.8, 1721.5)),
        ),
        recreate_moved_annotations=True,
    ),
    "BI-596045": CalibratedLayout(
        totals_rect=(25.0, 25.0, 239.0, 582.5),
        materials_rect=(23.0, 1331.0, 211.0, 1591.0),
        fill=(0.75, 1.0, 0.75),
        material_fill=(0.8, 1.0, 0.8),
        totals_border=TEXT_RED,
        totals_border_width=2.0,
        material_border=(0.0, 0.0, 1.0),
        material_border_width=1.0,
        title=LineBlock((28.0, 54.2), 32.1, TextStyle(28.0, TEXT_RED, bold_narrow=True)),
        totals=LineBlock((28.0, 118.4), 32.1, TextStyle(28.0, TEXT_RED, bold_narrow=True)),
        material_heading="Material",
        material_title=LineBlock((24.5, 1350.6), 22.3, TextStyle(20.0, MATERIAL_TEXT)),
        materials=LineBlock((24.5, 1395.2), 22.3, TextStyle(20.0, MATERIAL_TEXT)),
    ),
    "BI-829050": CalibratedLayout(
        totals_rect=(31.2, 34.5, 111.0, 333.9),
        materials_rect=(14.0, 493.5, 137.5, 711.0),
        fill=(0.749, 1.0, 0.749),
        material_fill=(0.75, 1.0, 0.75),
        material_border=(0.0, 0.0, 1.0),
        material_border_width=1.0,
        title=LineBlock((34.0, 48.2), 14.0, TextStyle(12.11, TEXT_RED, bold_narrow=True)),
        totals=LineBlock((34.0, 76.2), 14.0, TextStyle(12.11, TEXT_RED, bold_narrow=True)),
        material_heading="Material",
        material_title=LineBlock((15.5, 505.9), 13.4, TextStyle(12.0, MATERIAL_TEXT)),
        materials=LineBlock((15.5, 532.7), 13.4, TextStyle(12.0, MATERIAL_TEXT)),
    ),
    "BI-864045": CalibratedLayout(
        totals_rect=(1082.6, 23.1, 1184.1, 364.6),
        materials_rect=(1107.9, 548.2, 1212.5, 721.9),
        fill=(0.749, 1.0, 0.749),
        material_fill=(0.8, 1.0, 0.8),
        material_border=(0.0, 0.0, 1.0),
        material_border_width=1.0,
        title=LineBlock((1096.3, 36.8), 14.0, TextStyle(12.11, TEXT_RED, bold_narrow=True)),
        totals=LineBlock((1096.3, 64.8), 14.0, TextStyle(12.11, TEXT_RED, bold_narrow=True)),
        material_heading="Material",
        material_title=LineBlock((1109.4, 557.8), 10.0, TextStyle(9.0, MATERIAL_TEXT)),
        materials=LineBlock((1109.4, 577.9), 10.0, TextStyle(9.0, MATERIAL_TEXT)),
        extra_highlights=(
            (185.3, 353.9, 305.8, 499.8),
            (1004.2, 375.9, 1071.7, 397.5),
            (414.2, 439.2, 461.4, 465.6),
            (562.8, 514.8, 662.4, 572.8),
        ),
        extra_highlight_overlay=True,
        annotation_moves=(
            ((185.3, 352.3, 305.8, 498.2), (183.8, 352.4, 307.3, 501.3)),
            ((1004.2, 375.1, 1071.7, 396.8), (1002.8, 374.4, 1073.2, 399.0)),
            ((384.8, 434.8, 490.8, 470.0), (412.7, 437.7, 462.9, 467.1)),
            ((562.8, 518.0, 662.4, 576.0), (561.3, 513.3, 663.9, 574.3)),
        ),
        recreate_moved_annotations=True,
    ),
    "BI-912047": CalibratedLayout(
        totals_rect=(16.0, 20.5, 200.0, 429.5),
        materials_rect=(14.5, 1443.0, 221.5, 1586.0),
        fill=(0.75, 1.0, 0.75),
        material_fill=None,
        totals_border=TEXT_RED,
        totals_border_width=2.0,
        material_border=(0.0, 0.0, 1.0),
        material_border_width=1.0,
        title=LineBlock((19.0, 45.9), 27.5, TextStyle(24.0, TEXT_RED, bold_narrow=True)),
        totals=LineBlock((19.0, 100.9), 27.5, TextStyle(24.0, TEXT_RED, bold_narrow=True)),
        material_heading="Materials",
        material_title=LineBlock((16.0, 1458.9), 17.9, TextStyle(16.0, MATERIAL_TEXT)),
        materials=LineBlock((16.0, 1476.8), 17.9, TextStyle(16.0, MATERIAL_TEXT)),
    ),
}


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


def choose_box_rect(page: fitz.Page, lines: list[str]) -> fitz.Rect:
    width, height, _, _ = _box_metrics(page, lines)
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
    best = scored[0]
    if (
        best.total > MAX_SAFE_PLACEMENT_SCORE
        or best.text_overlap_ratio > MAX_TEXT_OVERLAP_RATIO
        or best.annotation_overlap_ratio > MAX_ANNOTATION_OVERLAP_RATIO
    ):
        raise PlacementReviewRequired()
    return best.rect


def _candidate_rects(
    page: fitz.Page,
    width: float,
    height: float,
    margin_x: float,
    margin_y: float,
) -> list[fitz.Rect]:
    max_x = max(margin_x, page.rect.width - margin_x - width)
    max_y = max(margin_y, page.rect.height - margin_y - height)
    mid_y = margin_y + max(0.0, (max_y - margin_y) / 2)
    return [
        fitz.Rect(margin_x, margin_y, margin_x + width, margin_y + height),
        fitz.Rect(max_x, margin_y, max_x + width, margin_y + height),
        fitz.Rect(margin_x, max_y, margin_x + width, max_y + height),
        fitz.Rect(max_x, max_y, max_x + width, max_y + height),
        fitz.Rect(margin_x, mid_y, margin_x + width, mid_y + height),
        fitz.Rect(max_x, mid_y, max_x + width, mid_y + height),
    ]


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
        layout = _layout_for_source(source_name)
        if layout:
            _draw_calibrated_summary(page, summary, layout, source_name or "")
            buffer = BytesIO()
            doc.save(buffer, garbage=4, deflate=True)
            return buffer.getvalue()

        rect = choose_box_rect(page, lines)
        width, _, font_size, padding = _box_metrics(page, lines)
        line_height = font_size * 1.16
        page.draw_rect(rect, color=None, fill=BOX_FILL, overlay=True)

        y = rect.y0 + padding + font_size
        x = rect.x0 + padding
        max_chars = max(12, int((width - padding * 2) / (font_size * 0.54)))
        for idx, line in enumerate(lines):
            wrapped = _wrap_line(line, max_chars)
            for part in wrapped:
                if y > rect.y1 - padding:
                    break
                size = font_size * (1.05 if idx == 0 else 1.0)
                _insert_text(
                    page,
                    (x, y),
                    part,
                    TextStyle(size=size, color=TEXT_RED, bold_narrow=True),
                )
                y += line_height
            if y > rect.y1 - padding:
                break

        buffer = BytesIO()
        doc.save(buffer, garbage=4, deflate=True)
        return buffer.getvalue()
    finally:
        doc.close()


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


def _layout_for_source(source_name: str | None) -> CalibratedLayout | None:
    if not source_name:
        return None
    for key, layout in CALIBRATED_LAYOUTS.items():
        if key in source_name:
            return layout
    return None


def _draw_calibrated_summary(
    page: fitz.Page,
    summary: SummaryResult,
    layout: CalibratedLayout,
    source_name: str,
) -> None:
    _move_calibrated_annotations(page, layout)

    extra_fill = layout.material_fill or layout.fill
    for rect in layout.extra_highlights:
        page.draw_rect(
            fitz.Rect(rect),
            color=None,
            fill=extra_fill,
            overlay=layout.extra_highlight_overlay,
        )

    page.draw_rect(
        fitz.Rect(layout.totals_rect),
        color=layout.totals_border,
        fill=layout.fill,
        width=layout.totals_border_width,
        overlay=True,
    )
    page.draw_rect(
        fitz.Rect(layout.materials_rect),
        color=layout.material_border,
        fill=layout.material_fill or layout.fill,
        width=layout.material_border_width,
        overlay=True,
    )

    _draw_lines(page, [summary.title.strip() or "MKR Job Totals"], layout.title)
    _draw_lines(page, summary.job_totals, layout.totals)
    if summary.materials:
        _draw_lines(page, [layout.material_heading], layout.material_title)
        _draw_lines(page, _calibrated_material_lines(source_name, summary.materials), layout.materials)


def _move_calibrated_annotations(page: fitz.Page, layout: CalibratedLayout) -> None:
    if not layout.annotation_moves:
        return
    replacements: list[tuple[fitz.Rect, dict, tuple[float, float, float]]] = []
    for annot in list(page.annots() or []):
        annot_rect = _rounded_rect_tuple(annot.rect)
        for source, target in layout.annotation_moves:
            if _rect_tuple_close(annot_rect, source):
                if layout.recreate_moved_annotations and annot.type[1] == "FreeText":
                    fill = _annotation_fill_color(annot, layout)
                    replacements.append((fitz.Rect(target), dict(annot.info), fill))
                    page.delete_annot(annot)
                else:
                    annot.set_rect(fitz.Rect(target))
                    annot.update()
                break
    for target, info, fill in replacements:
        _add_recreated_freetext_annotation(page, target, info, fill)


def _annotation_fill_color(
    annot: fitz.Annot,
    layout: CalibratedLayout,
) -> tuple[float, float, float]:
    stroke = annot.colors.get("stroke") or []
    if len(stroke) >= 3 and stroke[1] > 0.8 and stroke[0] > 0.5:
        return (float(stroke[0]), float(stroke[1]), float(stroke[2]))
    return layout.material_fill or layout.fill


def _add_recreated_freetext_annotation(
    page: fitz.Page,
    target: fitz.Rect,
    info: dict,
    fill: tuple[float, float, float],
) -> None:
    content = str(info.get("content") or "").replace("\r", "\n")
    annot = page.add_freetext_annot(
        target,
        content,
        fontsize=8.0,
        fontname="TiRo",
        text_color=(0.0, 0.0, 1.0),
        fill_color=fill,
    )
    annot.set_info(
        {
            key: value
            for key, value in info.items()
            if key in {"name", "title", "creationDate", "modDate", "subject"}
        }
    )
    annot.update()


def _rounded_rect_tuple(rect: fitz.Rect) -> tuple[float, float, float, float]:
    return tuple(round(v, 1) for v in rect)


def _rect_tuple_close(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    tolerance: float = 3.0,
) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(first, second))


def _draw_lines(page: fitz.Page, lines: list[str], block: LineBlock) -> None:
    x, y = block.origin
    for line in lines:
        if not line.strip():
            continue
        _insert_text(page, (x, y), line, block.style)
        if block.style.rotate in {90, 270}:
            x += block.line_gap
        else:
            y += block.line_gap


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


def _calibrated_material_lines(source_name: str, materials: list[str]) -> list[str]:
    if "BI-596045" in source_name and materials and materials[0] == "605-3277 48Ct - 950'":
        return ["605-3277 48Ct - ", "950'", *materials[1:]]
    return materials
