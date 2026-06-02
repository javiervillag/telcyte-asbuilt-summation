from pathlib import Path

import fitz

from app.models import SummaryResult
from app.pdf_annotator import annotate_pdf


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
