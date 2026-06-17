from pathlib import Path
import re

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
        summary_annotations = _summary_annotations(doc[0])
        material_annotations = _material_annotations(doc[0])
    finally:
        doc.close()
    assert "MKR Job Totals" in text
    assert "UG-56 - 170'" in text
    assert "605-3277 48Ct - 750'" in text
    assert "\u00ad" not in text
    assert "605-3277 48Ct - 750'" not in summary_annotations[0]
    assert material_annotations == ["Materials\n605-3277 48Ct - 750'"]
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


def test_materials_box_is_separate_bottom_left_and_sample_styled() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    source = doc.tobytes()
    doc.close()
    summary = SummaryResult(
        model="parser-test",
        confidence=1.0,
        job_totals=["UG-56 - 170'"],
        materials=["220-9236 (.625) - 140'"],
    )

    output = annotate_pdf(source, summary)
    doc = fitz.open(stream=output, filetype="pdf")
    try:
        page = doc[0]
        material_annots = [
            annot for annot in page.annots() or []
            if str((annot.info or {}).get("content", "")).startswith("Materials")
        ]
        assert len(material_annots) == 1
        material = material_annots[0]
        assert material.type[1] == "FreeText"
        assert material.rect.x0 < page.rect.width * 0.25
        assert material.rect.y1 > page.rect.height * 0.75
        assert "0 0 0 rg" in (doc.xref_get_key(material.xref, "DA")[1] or "")
        assert b"0 0 1 RG" in _appearance_stream(doc, material)
    finally:
        doc.close()


def test_unrotated_mkr_box_keeps_standard_appearance_stream() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    source = doc.tobytes()
    doc.close()
    summary = SummaryResult(
        model="parser-test",
        confidence=1.0,
        job_totals=["UG-85 - 10"],
    )

    output = annotate_pdf(source, summary)
    doc = fitz.open(stream=output, filetype="pdf")
    try:
        page = doc[0]
        summary_annots = [
            annot for annot in page.annots() or []
            if str((annot.info or {}).get("content", "")).startswith("MKR Job Totals")
        ]
        assert len(summary_annots) == 1
        stream = _appearance_stream(doc, summary_annots[0])
        assert b"1 1 " not in stream
        assert b" re\nB\n" not in stream
    finally:
        doc.close()


def test_split_title_existing_totals_box_is_replaced_in_place() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    original_rect = fitz.Rect(20, 24, 180, 180)
    page.add_freetext_annot(original_rect, "MKR Job\nTotals\nUG-06 - 2", fontsize=12)
    source = doc.tobytes()
    doc.close()
    summary = SummaryResult(
        model="parser-test",
        confidence=1.0,
        job_totals=["UG-85 - 10"],
    )

    output = annotate_pdf(source, summary)
    doc = fitz.open(stream=output, filetype="pdf")
    try:
        page = doc[0]
        summary_annots = [
            annot for annot in page.annots() or []
            if "MKR Job" in str((annot.info or {}).get("content", ""))
        ]
        assert len(summary_annots) == 1
        assert summary_annots[0].info["content"] == "MKR Job Totals\nUG-85 - 10"
        assert "UG-06 - 2" not in summary_annots[0].info["content"]
        assert abs(summary_annots[0].rect.x0 - original_rect.x0) < 1
        assert abs(summary_annots[0].rect.y0 - original_rect.y0) < 1
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


def test_rotated_pdf_summary_is_movable_annotation() -> None:
    # Adobe shows baked boxes as stuck page ink, absent from the Comments pane.
    # Rotated sheets now get a real FreeText annotation with a rotated
    # appearance stream; drag behavior still needs real editor verification.
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
        assert page.rotation == 90  # page untouched
        summary_annots = [annot for annot in page.annots() or [] if (annot.info or {}).get("content", "").startswith("MKR Job Totals")]
        assert len(summary_annots) == 1
        assert summary_annots[0].type[1] == "FreeText"
        assert "UG-56 - 168'" in summary_annots[0].info["content"]
        assert (doc.xref_get_key(summary_annots[0].xref, "Rotate")[1] or "") == "90"
        assert b"MKR Job Totals" not in page.read_contents()
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


def _material_annotations(page: fitz.Page) -> list[str]:
    rows = []
    for annot in page.annots() or []:
        content = str((annot.info or {}).get("content") or "").replace("\r", "\n")
        if annot.type[1] == "FreeText" and content.startswith("Materials"):
            rows.append(content)
    return rows


def _appearance_stream(doc: fitz.Document, annot: fitz.Annot) -> bytes:
    ap_ref = doc.xref_get_key(annot.xref, "AP")[1] or ""
    match = re.search(r"(\d+) 0 R", ap_ref)
    if not match:
        return b""
    return doc.xref_stream(int(match.group(1))) or b""


def _page_text_with_annotations(page: fitz.Page) -> str:
    parts = [page.get_text("text")]
    for annot in page.annots() or []:
        parts.append(str((annot.info or {}).get("content") or ""))
    return "\n".join(parts)


def _green_pixels_with_annotations(page: fitz.Page) -> int:
    pix = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5), annots=True, alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return sum(1 for r, g, b in image.getdata() if g > 220 and 150 < r < 230 and 120 < b < 210)
