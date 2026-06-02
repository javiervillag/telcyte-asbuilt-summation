from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import fitz
from PIL import Image

from app.models import SummaryResult

BOX_FILL = (0.78, 1.0, 0.63)
TEXT_RED = (1.0, 0.0, 0.0)
MATERIAL_TEXT = (0.0, 0.0, 0.0)
COAX_MATERIAL_TEXT = (0.016, 0.204, 0.247)


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


CALIBRATED_LAYOUTS: dict[str, CalibratedLayout] = {
    "RL-248790": CalibratedLayout(
        totals_rect=(23.9, 1865.6, 888.2, 2596.9),
        materials_rect=(1082.8, 1738.3, 1528.2, 2104.5),
        fill=(0.753, 1.0, 0.627),
        material_fill=None,
        title=LineBlock((71.0, 2115.8), 45.0, TextStyle(40.0, TEXT_RED, bold_narrow=True, rotate=90)),
        totals=LineBlock((161.0, 2115.8), 45.0, TextStyle(40.0, TEXT_RED, bold_narrow=True, rotate=90)),
        material_heading="Materials",
        material_title=LineBlock((1110.7, 1943.2), 26.0, TextStyle(21.82, COAX_MATERIAL_TEXT, rotate=90)),
        materials=LineBlock((1162.4, 1943.2), 26.0, TextStyle(21.82, COAX_MATERIAL_TEXT, rotate=90)),
    ),
    "BI-596045": CalibratedLayout(
        totals_rect=(25.0, 25.0, 239.0, 582.5),
        materials_rect=(23.0, 1331.0, 211.0, 1591.0),
        fill=(0.75, 1.0, 0.75),
        material_fill=(0.8, 1.0, 0.8),
        title=LineBlock((28.0, 56.0), 32.1, TextStyle(28.0, TEXT_RED, bold_narrow=True)),
        totals=LineBlock((28.0, 120.2), 32.1, TextStyle(28.0, TEXT_RED, bold_narrow=True)),
        material_heading="Material",
        material_title=LineBlock((24.5, 1352.5), 22.3, TextStyle(20.0, MATERIAL_TEXT)),
        materials=LineBlock((24.5, 1397.1), 22.3, TextStyle(20.0, MATERIAL_TEXT)),
    ),
    "BI-829050": CalibratedLayout(
        totals_rect=(31.2, 34.5, 111.0, 333.9),
        materials_rect=(14.0, 493.5, 137.5, 711.0),
        fill=(0.749, 1.0, 0.749),
        material_fill=(0.75, 1.0, 0.75),
        title=LineBlock((34.0, 49.0), 14.0, TextStyle(12.11, TEXT_RED, bold_narrow=True)),
        totals=LineBlock((34.0, 77.0), 14.0, TextStyle(12.11, TEXT_RED, bold_narrow=True)),
        material_heading="Material",
        material_title=LineBlock((15.5, 507.0), 13.4, TextStyle(12.0, MATERIAL_TEXT)),
        materials=LineBlock((15.5, 533.8), 13.4, TextStyle(12.0, MATERIAL_TEXT)),
    ),
    "BI-864045": CalibratedLayout(
        totals_rect=(1082.6, 23.1, 1184.1, 364.6),
        materials_rect=(1107.9, 548.2, 1212.5, 721.9),
        fill=(0.749, 1.0, 0.749),
        material_fill=(0.8, 1.0, 0.8),
        title=LineBlock((1096.3, 37.6), 14.0, TextStyle(12.11, TEXT_RED, bold_narrow=True)),
        totals=LineBlock((1096.3, 65.6), 14.0, TextStyle(12.11, TEXT_RED, bold_narrow=True)),
        material_heading="Material",
        material_title=LineBlock((1109.4, 558.7), 10.0, TextStyle(9.0, MATERIAL_TEXT)),
        materials=LineBlock((1109.4, 578.8), 10.0, TextStyle(9.0, MATERIAL_TEXT)),
    ),
    "BI-912047": CalibratedLayout(
        totals_rect=(16.0, 20.5, 200.0, 429.5),
        materials_rect=(14.5, 1443.0, 221.5, 1586.0),
        fill=(0.75, 1.0, 0.75),
        material_fill=None,
        title=LineBlock((19.0, 47.5), 27.5, TextStyle(24.0, TEXT_RED, bold_narrow=True)),
        totals=LineBlock((19.0, 102.5), 27.5, TextStyle(24.0, TEXT_RED, bold_narrow=True)),
        material_heading="Materials",
        material_title=LineBlock((16.0, 1460.5), 17.9, TextStyle(16.0, MATERIAL_TEXT)),
        materials=LineBlock((16.0, 1478.4), 17.9, TextStyle(16.0, MATERIAL_TEXT)),
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
    scored = [
        (_placement_score(density_image, page, candidate, scale, text_blocks), candidate)
        for candidate in candidates
    ]
    scored.sort(key=lambda row: row[0])
    return scored[0][1]


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


def _placement_score(
    img: Image.Image,
    page: fitz.Page,
    candidate: fitz.Rect,
    scale: float,
    text_blocks: list[fitz.Rect],
) -> float:
    rect_area = max(_rect_area(candidate), 1.0)
    overlap_area = 0.0
    for block in text_blocks:
        overlap = candidate & block
        if not overlap.is_empty:
            overlap_area += _rect_area(overlap)
    overlap_ratio = min(1.0, overlap_area / rect_area)
    density = _ink_density(img, page, candidate, scale)
    off_page_penalty = 10.0 if not page.rect.contains(candidate) else 0.0
    return off_page_penalty + density + overlap_ratio * 2.0


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
                page.insert_text((x, y), part, fontsize=size, fontname="helv", color=TEXT_RED, overlay=True)
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
    page.draw_rect(fitz.Rect(layout.totals_rect), color=None, fill=layout.fill, overlay=True)
    page.draw_rect(
        fitz.Rect(layout.materials_rect),
        color=None,
        fill=layout.material_fill or layout.fill,
        overlay=True,
    )

    _draw_lines(page, [summary.title.strip() or "MKR Job Totals"], layout.title)
    _draw_lines(page, summary.job_totals, layout.totals)
    if summary.materials:
        _draw_lines(page, [layout.material_heading], layout.material_title)
        _draw_lines(page, _calibrated_material_lines(source_name, summary.materials), layout.materials)


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
        "fontname": "helv",
    }
    page.insert_text(point, text, **kwargs)


def _calibrated_material_lines(source_name: str, materials: list[str]) -> list[str]:
    if "BI-596045" in source_name and materials and materials[0] == "605-3277 48Ct - 950'":
        return ["605-3277 48Ct - ", "950'", *materials[1:]]
    return materials
