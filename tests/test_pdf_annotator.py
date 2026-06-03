from pathlib import Path

import fitz

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


def test_choose_box_rect_requires_review_when_all_candidates_touch_annotations() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.add_rect_annot(fitz.Rect(0, 0, 612, 792))

    try:
        try:
            choose_box_rect(page, ["MKR Job Totals", "UG-56 - 170'"])
        except PlacementReviewRequired:
            pass
        else:
            raise AssertionError("Expected placement to require manual review")
    finally:
        doc.close()


def test_generic_output_preserves_existing_green_annotations() -> None:
    input_pdf = SAMPLE.parent.joinpath("COAX-ASBUILT-(TelCyte)-RL-248790-Totals Removed.pdf")
    before_doc = fitz.open(input_pdf)
    try:
        before_annotations = _annotation_snapshot(before_doc[0])
        before_green_fills = _green_fill_count(before_doc[0])
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
        after_green_fills = _green_fill_count(doc[0])
    finally:
        doc.close()

    assert after_annotations[: len(before_annotations)] == before_annotations
    added_annotations = after_annotations[len(before_annotations) :]
    assert added_annotations == [
        (
            "FreeText",
            added_annotations[0][1],
            "MKR Job Totals\nUG-83 - 140'",
        )
    ]
    assert after_green_fills - before_green_fills in {0, 1}


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


def _green_fill_count(page: fitz.Page) -> int:
    count = 0
    for drawing in page.get_drawings():
        fill = drawing.get("fill")
        if not fill or len(fill) < 3:
            continue
        r, g, b = fill[:3]
        if g > 0.7 and r > 0.6 and b < 0.8:
            count += 1
    return count
