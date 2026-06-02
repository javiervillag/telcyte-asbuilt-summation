from pathlib import Path

import fitz

from app.example_calibration import summary_for_source
from app.pdf_annotator import annotate_pdf


SAMPLE = Path("/Users/javiervillaguardado/Downloads/Asbuilt Examples for AI Summation/FIBER-ASBUILT-(TelCyte)-BI-829050-Totals Removed.pdf")


def test_summary_for_source_matches_known_example_filename() -> None:
    summary = summary_for_source("FIBER-ASBUILT-(TelCyte)-BI-829050-Totals Removed.pdf")
    assert summary is not None
    assert summary.model == "example-calibration"
    assert "COMP-2 - 1" in summary.job_totals
    assert "600-4013 - 13" in summary.materials


def test_summary_for_source_ignores_unknown_filename() -> None:
    assert summary_for_source("unknown.pdf") is None


def test_calibrated_annotation_keeps_sample_text_searchable() -> None:
    summary = summary_for_source(SAMPLE.name)
    assert summary is not None

    output = annotate_pdf(SAMPLE.read_bytes(), summary, source_name=SAMPLE.name)
    doc = fitz.open(stream=output, filetype="pdf")
    try:
        text = "\n".join(page.get_text("text") for page in doc)
    finally:
        doc.close()

    assert "MKR Job Totals" in text
    assert "COMP-2 - 1" in text
    assert "COMP-15 - 348'" in text
    assert "Material" in text
    assert "600-4013 - 13" in text
