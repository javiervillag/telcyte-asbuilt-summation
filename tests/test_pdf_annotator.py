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
        text = doc[0].get_text("text")
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


def test_rotated_pdf_summary_is_selectable_upright_annotation() -> None:
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
        summary_annots = [annot for annot in page.annots() or [] if (annot.info or {}).get("content", "").startswith("MKR Job Totals")]
        assert len(summary_annots) == 1
        summary_annot = summary_annots[0]
        assert summary_annot.type[1] == "FreeText"
        assert "UG-56 - 168'" in summary_annot.info["content"]
        assert "/Rotate 90" in doc.xref_object(summary_annot.xref, compressed=False)
        assert "MKR Job Totals" in page.get_text("text")
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
