from pathlib import Path

import fitz

from app.models import SummaryResult
from app.pdf_annotator import choose_box_rect, annotate_pdf


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
        text = doc[0].get_text("text")
    finally:
        doc.close()
    assert "MKR Job Totals" in text
    assert "UG-56 - 170'" in text
    assert "605-3277 48Ct - 750'" in text
    assert "\u00ad" not in text


def test_choose_box_rect_avoids_existing_annotation() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.add_rect_annot(fitz.Rect(14, 18, 170, 70))

    rect = choose_box_rect(page, ["MKR Job Totals", "UG-56 - 170'"])

    doc.close()
    assert rect.x0 > 300
    assert rect.y0 < 120


def test_calibrated_output_moves_known_green_annotations() -> None:
    summary = SummaryResult(
        model="example-calibration",
        confidence=1.0,
        job_totals=["UG-83 - 140'"],
        materials=[],
    )
    output = annotate_pdf(
        SAMPLE.parent.joinpath("COAX-ASBUILT-(TelCyte)-RL-248790-Totals Removed.pdf").read_bytes(),
        summary,
        source_name="COAX-ASBUILT-(TelCyte)-RL-248790-Totals Removed.pdf",
    )
    doc = fitz.open(stream=output, filetype="pdf")
    try:
        rects = [tuple(round(v, 1) for v in annot.rect) for annot in doc[0].annots() or []]
    finally:
        doc.close()

    assert any(rect[1:] == (1574.8, 699.0, 1675.8) for rect in rects)
    assert (614.4, 1578.1, 650.6, 1679.1) not in rects
