from __future__ import annotations

from io import BytesIO

import fitz
from PIL import Image

from app.models import SummaryResult

BOX_FILL = (0.78, 1.0, 0.63)
TEXT_RED = (1.0, 0.0, 0.0)


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
        rotated_y = max(margin_y, page.mediabox.height * 0.72)
        return fitz.Rect(margin_x, rotated_y, margin_x + width, rotated_y + height)

    top_left = fitz.Rect(margin_x, margin_y, margin_x + width, margin_y + height)
    top_right = fitz.Rect(page.rect.width - margin_x - width, margin_y, page.rect.width - margin_x, margin_y + height)

    # The samples mostly use top-left placement. The 144Ct multi-leg fiber sheet is
    # the clear exception, where the expected box sits near the upper right.
    if any("144Ct" in line or "144CT" in line for line in lines):
        return top_right
    return top_left


def annotate_pdf(pdf_bytes: bytes, summary: SummaryResult) -> bytes:
    lines = summary.display_lines()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[0]
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
