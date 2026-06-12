from pathlib import Path

import fitz
from PIL import Image

from app.models import SummaryResult
from app.pdf_annotator import PlacementReviewRequired, annotate_pdf, choose_box_rect


SAMPLE = Path("/Users/javiervillaguardado/Downloads/Asbuilt Examples for AI Summation/FIBER-ASBUILT-(TelCyte)-BI-829050-Totals Removed.pdf")


def test_annotate_pdf_adds_totals_text() -> None:
    summary = SummaryResult(
        model="test-model",
        confidence=0.9,
        job_totals=["UG-56 - 170'", "COMP-15 - 348'"],
        materials=["605-3277 48Ct - 750'"],
    )
    output = annotate_pdf(SAMPLE.read_bytes(), summary)
    doc = fitz.open(stream=output, filetype="pdf")
    try:
        text = _page_text_with_annotations(doc[0])
        baked_content = doc[0].read_contents()
    finally:
        doc.close()
    assert "MKR Job Totals" in text
    assert "UG-56 - 170'" in text
    assert "605-3277 48Ct - 750'" in text
    assert "\u00ad" not in text
    # The box is annotation-only: no baked page-content duplicate underneath
    # (drag-duplicate bug, Nick Evans email 2026-06-09, BI-304069).
    assert b"MKR Job Totals" not in baked_content


def test_annotate_pdf_keeps_selected_extras_separate() -> None:
    summary = SummaryResult(
        model="test-model",
        confidence=0.9,
        job_totals=["UG-56 - 170'"],
        extra_totals=["PC-02 - 1", "TL-06 - 1"],
        extra_notes=["PC-02: White lining confirmed.", "TL-06: Approved HFC troubleshooting."],
    )
    output = annotate_pdf(SAMPLE.read_bytes(), summary)
    doc = fitz.open(stream=output, filetype="pdf")
    try:
        text = _page_text_with_annotations(doc[0])
    finally:
        doc.close()
    assert "User-selected extra totals" in text
    assert "PC-02 - 1" in text
    assert "TL-06 - 1" in text
    assert "Extra notes" in text
    assert "PC-02: White lining confirmed." in text


def test_choose_box_rect_avoids_existing_annotation() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    blocker = fitz.Rect(14, 18, 170, 70)
    page.add_rect_annot(blocker)

    rect = choose_box_rect(page, ["MKR Job Totals", "UG-56 - 170'"])

    doc.close()
    # Left side is preferred (Nick, BI-945043 2026-06-10): the box slides
    # down the left column past the blocking annotation instead of jumping
    # to the right corner.
    assert rect.x0 < 300
    assert (rect & blocker).is_empty
    assert rect.y0 <= 792 * 0.3


def test_choose_box_rect_stays_top_side_even_when_candidates_touch_annotations() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.add_rect_annot(fitz.Rect(0, 0, 612, 792))

    try:
        rect = choose_box_rect(page, ["MKR Job Totals", "UG-56 - 170'"])
        assert rect.y0 <= page.rect.height * 0.3
        assert rect.x0 <= page.rect.width * 0.2 or rect.x1 >= page.rect.width * 0.8
    finally:
        doc.close()


def test_generic_output_preserves_existing_green_annotations() -> None:
    input_pdf = SAMPLE.parent.joinpath("COAX-ASBUILT-(TelCyte)-RL-248790-Totals Removed.pdf")
    before_doc = fitz.open(input_pdf)
    try:
        before_annotations = _annotation_snapshot(before_doc[0])
    finally:
        before_doc.close()

    summary = SummaryResult(
        model="parser-test",
        confidence=1.0,
        job_totals=["UG-83 - 140'"],
        materials=[],
    )
    output = annotate_pdf(
        input_pdf.read_bytes(),
        summary,
        source_name="COAX-ASBUILT-(TelCyte)-RL-248790-Totals Removed.pdf",
    )
    doc = fitz.open(stream=output, filetype="pdf")
    try:
        after_annotations = _annotation_snapshot(doc[0])
        summary_annotations = _summary_annotations(doc[0])
    finally:
        doc.close()

    assert after_annotations[: len(before_annotations)] == before_annotations
    assert len(after_annotations) == len(before_annotations) + 1
    assert summary_annotations == ["MKR Job Totals\nUG-83 - 140'"]


def test_rotated_pdf_summary_page_is_normalized_and_upright() -> None:
    # Rotated sheets are rewritten to native rotation-0 in the output so PDF
    # editors never sideways-flip a dragged box (NR-1138768, 2026-06-11).
    input_pdf = SAMPLE.parent.joinpath("COAX-ASBUILT-(TelCyte)-RL-248790-Totals Removed.pdf")
    summary = SummaryResult(
        model="parser-test",
        confidence=1.0,
        job_totals=["UG-83 - 140'", "UG-56 - 168'"],
        materials=[],
    )

    output = annotate_pdf(input_pdf.read_bytes(), summary)
    doc = fitz.open(stream=output, filetype="pdf")
    try:
        page = doc[0]
        assert page.rotation == 0
        summary_annots = [annot for annot in page.annots() or [] if (annot.info or {}).get("content", "").startswith("MKR Job Totals")]
        assert len(summary_annots) == 1
        summary_annot = summary_annots[0]
        assert summary_annot.type[1] == "FreeText"
        assert "UG-56 - 168'" in summary_annot.info["content"]
        assert summary_annot.flags & fitz.PDF_ANNOT_IS_NO_ROTATE
        assert _green_pixels_with_annotations(page) > 1000
    finally:
        doc.close()


def test_summary_box_stays_in_top_left_or_top_right_section() -> None:
    input_pdf = SAMPLE.parent.joinpath("FIBER-ASBUILT-(TelCyte)-BI-596045-Totals Removed.pdf")
    summary = SummaryResult(
        model="parser-test",
        confidence=1.0,
        job_totals=[
            "MDU-11 - 102'",
            "UG-56 - 132'",
            "MDU-21 - 2",
            "MDU-05 - 2",
            "COMP-15 - 724'",
            "UG-65 - 2",
            "FX-11 - 2",
            "UG-07 - 1",
            "CD-01 - 1",
        ],
        extra_totals=["PC-01 - 1", "PC-02 - 1"],
        warnings=[
            "Readable construction callouts require rate-card/composite interpretation: EOL - 48Ct - 30'; Storage - 48Ct - 2'."
        ],
    )

    output = annotate_pdf(input_pdf.read_bytes(), summary)
    doc = fitz.open(stream=output, filetype="pdf")
    try:
        page = doc[0]
        summary_annots = [annot for annot in page.annots() or [] if (annot.info or {}).get("content", "").startswith("MKR Job Totals")]
        assert len(summary_annots) == 1
        rect = summary_annots[0].rect
        assert rect.y0 <= page.rect.height * 0.3
        assert rect.x0 <= page.rect.width * 0.2 or rect.x1 >= page.rect.width * 0.8
    finally:
        doc.close()


def _annotation_snapshot(page: fitz.Page) -> list[tuple[str, tuple[float, float, float, float], str]]:
    rows = []
    for annot in page.annots() or []:
        rows.append(
            (
                annot.type[1],
                tuple(round(v, 1) for v in annot.rect),
                str((annot.info or {}).get("content") or "").replace("\r", "\n"),
            )
        )
    return rows


def _summary_annotations(page: fitz.Page) -> list[str]:
    rows = []
    for annot in page.annots() or []:
        content = str((annot.info or {}).get("content") or "").replace("\r", "\n")
        if annot.type[1] == "FreeText" and content.startswith("MKR Job Totals"):
            rows.append(content)
    return rows


def _page_text_with_annotations(page: fitz.Page) -> str:
    parts = [page.get_text("text")]
    for annot in page.annots() or []:
        parts.append(str((annot.info or {}).get("content") or ""))
    return "\n".join(parts)


def _green_pixels_with_annotations(page: fitz.Page) -> int:
    pix = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5), annots=True, alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return sum(1 for r, g, b in image.getdata() if g > 220 and 150 < r < 230 and 120 < b < 210)
