"""Regression tests for Nick Evans' 2026-06-09 feedback (email BI-304069 +
weekly sync). Every new edge case Nick reports should be appended here so the
parser/annotator behavior stays pinned as the code evolves.

None of these tests require local sample PDFs.
"""
from __future__ import annotations

import fitz
import pytest

from app.models import SummaryResult
from app.pdf_annotator import BORDER_WIDTH, annotate_pdf
from app.pdf_parser import derive_code_totals, diagnose_extraction, extract_text_blocks
from app.rate_cards import code_key, total_line_key


def _pdf_with_lines(lines: list[str], width: float = 1728, height: float = 2592) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    for i, line in enumerate(lines):
        page.insert_text((width * 0.55, height * 0.55 + i * 24), line)
    content = doc.tobytes()
    doc.close()
    return content


# --- Email: unit markers must not split totals (BI-304069: UG-80, UG-03) ---

def test_unit_variants_total_together_and_render_unitless() -> None:
    content = _pdf_with_lines(["UG-80 - 258'", "UG-80 - 91.75sqft", "UG-03 - 2172'", "UG-03 - 91.75"])
    totals = derive_code_totals(extract_text_blocks(content))
    assert "UG-80 - 349.75" in totals
    assert "UG-03 - 2263.75" in totals
    assert not any("'" in t or "sqft" in t for t in totals)


def test_total_line_key_ignores_units() -> None:
    assert total_line_key("UG-80 - 258'") == total_line_key("UG-80 - 258")
    assert total_line_key("UG-80 - 258sqft") == total_line_key("UG-80 - 258")


# --- Sync @50:20-56:55: DIRT-UG6-2 must be counted like CONCRETE-UG85-2 ---

def test_dirt_and_concrete_descriptor_callouts_both_count() -> None:
    content = _pdf_with_lines(["DIRT-UG6-2", "CONCRETE-UG85-2"])
    totals = derive_code_totals(extract_text_blocks(content))
    assert "UG-06 - 2" in totals
    assert "UG-85 - 2" in totals


def test_other_surface_descriptors_count() -> None:
    content = _pdf_with_lines(["Asphalt - UG-06 - 2", "Concrete - UG-06 - 1", "Dirt - UG-06 - 1"])
    totals = derive_code_totals(extract_text_blocks(content))
    assert totals == ["UG-06 - 4"]


# --- Utility-crossing markers stay excluded, but never silently ---

def test_utility_markers_are_not_codes() -> None:
    assert code_key("PWR-36") is None
    assert code_key("COX-12") is None
    assert code_key("ELI-7") is None
    assert code_key("UG-6") == ("UG", "6")


def test_utility_context_exclusions_are_surfaced_not_silent() -> None:
    content = _pdf_with_lines(["PWR - UG-06 - 2", "UG-06 - 3"])
    blocks = extract_text_blocks(content)
    excluded: list[str] = []
    totals = derive_code_totals(blocks, excluded_lines=excluded)
    assert totals == ["UG-06 - 3"]
    assert any("PWR" in line for line in excluded)
    diagnostics = diagnose_extraction(blocks, totals, excluded_context_lines=excluded)
    assert any("non-billing context" in w for w in diagnostics.warnings)


def test_bore_context_still_excluded_and_surfaced() -> None:
    content = _pdf_with_lines(['UG-06 - 1 Bore@36"', "UG-06 - 2"])
    excluded: list[str] = []
    totals = derive_code_totals(extract_text_blocks(content), excluded_lines=excluded)
    assert totals == ["UG-06 - 2"]
    assert excluded


# --- Email: totals box format (single annotation, border 2, red, scaled font) ---

def _summary() -> SummaryResult:
    return SummaryResult(
        model="parser-test",
        confidence=1.0,
        job_totals=["UG-80 - 349.75", "UG-44 - 425"],
        warnings=["Something needing review"],
    )


def test_box_is_single_annotation_with_no_baked_duplicate() -> None:
    output = annotate_pdf(_pdf_with_lines(["UG-80 - 258'"]), _summary())
    doc = fitz.open(stream=output, filetype="pdf")
    try:
        page = doc[0]
        summary_annots = [
            a for a in page.annots() or []
            if str((a.info or {}).get("content", "")).startswith("MKR Job Totals")
        ]
        assert len(summary_annots) == 1
        # No baked page-content copy (drag-duplicate bug): the page content
        # stream must not contain the box text (annotation appearance may).
        assert b"MKR Job Totals" not in page.read_contents()
        annot = summary_annots[0]
        # Border = yes, size 2.
        assert annot.border.get("width") == BORDER_WIDTH
        # Red text in /DA so Adobe and Nitro render the same color.
        assert "1 0 0 rg" in (doc.xref_get_key(annot.xref, "DA")[1] or "")
    finally:
        doc.close()


def test_review_warnings_not_stamped_in_box() -> None:
    lines = _summary().display_lines()
    assert "Review" not in lines
    assert not any("review" in line.lower() for line in lines)


def test_font_scales_with_sheet_size() -> None:
    small = annotate_pdf(_pdf_with_lines(["UG-06 - 1"], width=612, height=792), _summary())
    large = annotate_pdf(_pdf_with_lines(["UG-06 - 1"], width=2592, height=1728), _summary())

    def _fontsize(data: bytes) -> float:
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            for a in doc[0].annots() or []:
                if str((a.info or {}).get("content", "")).startswith("MKR Job Totals"):
                    da = doc.xref_get_key(a.xref, "DA")[1] or ""
                    return float(da.split("Tf")[0].split()[-1])
        finally:
            doc.close()
        raise AssertionError("summary annotation not found")

    assert _fontsize(large) > _fontsize(small) >= 10.0


# --- NR-702749 PRJ52 Segment 12 (2026-06-10): multi-page permit drawings ---

def test_codes_on_later_pages_are_totaled() -> None:
    # Permit drawings put billing callouts on pages past the old 3-page cap.
    doc = fitz.open()
    for page_codes in (["UG-06 - 1"], [], ["UG-06 - 2"], ["UG-84 - 38"], ["UG-85 - 9"]):
        page = doc.new_page(width=1224, height=792)
        for i, line in enumerate(page_codes):
            page.insert_text((700, 400 + i * 24), line)
    content = doc.tobytes()
    doc.close()

    totals = derive_code_totals(extract_text_blocks(content))
    assert "UG-06 - 3" in totals      # pages 1 + 3
    assert "UG-84 - 38" in totals     # page 4
    assert "UG-85 - 9" in totals      # page 5


def test_extract_json_truncated_payload_is_model_error() -> None:
    from app.openrouter_client import OpenRouterError, _extract_json

    with pytest.raises(OpenRouterError):
        _extract_json('{"title": "MKR Job Totals", "job_totals": ["UG-06 - 3"')


def test_model_review_failure_falls_back_to_parser_totals(monkeypatch) -> None:
    # A reviewer crash must never sink a run that has parser-backed totals.
    import asyncio

    import app.openrouter_client as oc
    from app.config import Settings

    class _BoomClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise RuntimeError("connection reset")

    monkeypatch.setattr(oc.httpx, "AsyncClient", _BoomClient)
    settings = Settings(OPENROUTER_API_KEY="test-key")
    # Rich enough that diagnostics do NOT require review (the review path
    # has its own fallback); spread blocks so the text layer looks healthy.
    doc = fitz.open()
    page = doc.new_page(width=1224, height=792)
    for i, line in enumerate(["UG-06 - 2", "UG-06 - 1", "UG-44 - 10", "UG-56 - 170", "DP-11 - 20"]):
        page.insert_text((100 + (i % 2) * 500, 150 + i * 110), line)
    page.insert_text((100, 700), "General as-built notes with plenty of readable text for parsing.")
    page.insert_text((700, 700), "Crew completed restoration per plan and verified quantities.")
    pdf = doc.tobytes()
    doc.close()

    summary = asyncio.run(oc.summarize_with_model(pdf, settings))

    assert "UG-06 - 3" in summary.job_totals
    assert "UG-44 - 10" in summary.job_totals
    assert any("parser-only" in w for w in summary.warnings)
