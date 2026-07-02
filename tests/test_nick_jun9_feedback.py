"""Regression tests for Nick Evans' 2026-06-09 feedback (email BI-304069 +
weekly sync). Every new edge case Nick reports should be appended here so the
parser/annotator behavior stays pinned as the code evolves.

None of these tests require local sample PDFs.
"""
from __future__ import annotations

from pathlib import Path

import fitz
import pytest
from PIL import Image

from app.box_titles import (
    is_previously_billed_totals_box,
    is_tool_new_totals_box,
    starts_with_new_totals_title,
    starts_with_totals_title,
)
from app.models import CableFootageLine, SummaryResult
from app.partial_asbuilt import derive_new_totals, extract_previously_billed_job_totals
from app.pdf_annotator import BORDER_WIDTH, annotate_pdf
from app.pdf_parser import (
    derive_code_totals,
    derive_code_totals_by_page,
    diagnose_extraction,
    extract_text_blocks,
    TextBlock,
)
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
    assert total_line_key("UG-85 - 5 (Due to PH depths)") == (("UG", "85"), "5", "")


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
    content = _pdf_with_lines([
        "PWR - UG-06 - 2",
        "UG-06 - 3",
        "UG-44 - 4",
        "UG-85 - 1",
        "General as-built note with enough readable text for parser confidence.",
        "Crew verified visible billing quantities and restoration notes.",
    ])
    blocks = extract_text_blocks(content)
    excluded: list[str] = []
    totals = derive_code_totals(blocks, excluded_lines=excluded)
    assert totals == ["UG-06 - 3", "UG-44 - 4", "UG-85 - 1"]
    assert any("PWR" in line for line in excluded)
    diagnostics = diagnose_extraction(blocks, totals, excluded_context_lines=excluded)
    assert not diagnostics.warnings
    assert any("non-billing context" in note for note in diagnostics.informational_notes)


def test_bore_context_still_excluded_and_surfaced() -> None:
    content = _pdf_with_lines(['UG-06 - 1 Bore@36"', "UG-06 - 2"])
    excluded: list[str] = []
    totals = derive_code_totals(extract_text_blocks(content), excluded_lines=excluded)
    assert totals == ["UG-06 - 2"]
    assert excluded


def test_standalone_unresolved_callout_is_done_with_notes_candidate() -> None:
    content = _pdf_with_lines([
        "UG-06 - 2",
        "UG-44 - 4",
        "UG-85 - 1",
        "Tie Point",
        "As-built notes include enough readable page text for a confident parser run.",
        "Crew verified quantities and restoration notes in the drawing text.",
    ])
    blocks = extract_text_blocks(content)
    totals = derive_code_totals(blocks)

    diagnostics = diagnose_extraction(blocks, totals)

    assert diagnostics.review_required is False
    assert not diagnostics.warnings
    assert any("Standalone construction callouts" in note and "Tie Point" in note for note in diagnostics.informational_notes)


def test_unresolved_callout_near_quantity_stays_review() -> None:
    content = _pdf_with_lines([
        "UG-06 - 2",
        "UG-44 - 4",
        "UG-85 - 1",
        "EOL - 48Ct - 66'",
        "As-built notes include enough readable page text for parser review.",
        "Crew verified quantities and restoration notes in the drawing text.",
    ])
    blocks = extract_text_blocks(content)
    totals = derive_code_totals(blocks)

    diagnostics = diagnose_extraction(blocks, totals)

    assert diagnostics.review_required is True
    assert any("EOL - 48Ct - 66'" in warning for warning in diagnostics.warnings)


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


def test_extract_json_ignores_trailing_model_notes() -> None:
    from app.openrouter_client import _extract_json

    payload = """```json
{
  "title": "MKR Job Totals",
  "job_totals": ["Comp-15 - 1228"],
  "materials": [],
  "warnings": [],
  "confidence": 0.78
}
```

**Reasoning notes:**

| Code | Evidence basis |
|---|---|
| Comp-15 | Included |
"""

    assert _extract_json(payload) == {
        "title": "MKR Job Totals",
        "job_totals": ["Comp-15 - 1228"],
        "materials": [],
        "warnings": [],
        "confidence": 0.78,
    }


def test_extract_json_accepts_plain_fenced_and_prefaced_json() -> None:
    from app.openrouter_client import _extract_json

    expected = {"title": "MKR Job Totals", "job_totals": ["UG-06 - 3"]}

    assert _extract_json('{"title": "MKR Job Totals", "job_totals": ["UG-06 - 3"]}') == expected
    assert _extract_json('```json\n{"title": "MKR Job Totals", "job_totals": ["UG-06 - 3"]}\n```') == expected
    assert _extract_json('Reasoning first.\n{"title": "MKR Job Totals", "job_totals": ["UG-06 - 3"]}') == expected


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


def test_pdf_context_represents_every_page_within_budget() -> None:
    # The LLM context must sample blocks from ALL pages (code-bearing blocks
    # first) instead of blindly truncating the tail, which dropped the later
    # pages of permit drawings (NR-702749, 2026-06-10).
    from app.pdf_parser import build_pdf_context

    doc = fitz.open()
    for page_num in range(6):
        page = doc.new_page(width=1224, height=792)
        page.insert_text((600, 400), f"UG-0{page_num + 1} - {page_num + 1}")
        for i in range(40):  # boilerplate filler
            page.insert_text((60, 60 + i * 17), f"General permit note {page_num}-{i} with no billing data")
    content = doc.tobytes()
    doc.close()

    ctx = build_pdf_context(content, max_chars=8000)
    assert len(ctx) <= 8000 + 200
    # Every page's code line made it in despite the tight budget.
    for page_num in range(6):
        assert f"UG-0{page_num + 1} - {page_num + 1}" in ctx


# --- 6-month robustness pass (2026-06-10) ---

def test_comma_grouped_quantities_total_correctly() -> None:
    content = _pdf_with_lines(["UG-03 - 1,904'", "UG-03 - 96", "Comp-9 - 2,756'"])
    totals = derive_code_totals(extract_text_blocks(content))
    assert "UG-03 - 2000" in totals
    assert "Comp-9 - 2756" in totals


def test_total_line_key_handles_comma_quantities() -> None:
    assert total_line_key("UG-03 - 1,904'") == total_line_key("UG-03 - 1904")


def test_unicode_dashes_and_multiplication_sign_normalize() -> None:
    # Page text with base-14 fonts cannot encode an en-dash, but FreeText
    # callout annotations can - which is exactly where field crews type them.
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.add_freetext_annot(fitz.Rect(72, 72, 260, 130), "UG\u201306 \u2013 2", fontsize=10)
    page.insert_text((72, 200), "13 x UG-44")
    content = doc.tobytes()
    doc.close()

    totals = derive_code_totals(extract_text_blocks(content))
    assert "UG-06 - 2" in totals
    assert "UG-44 - 13" in totals


def test_clean_text_normalizes_typographic_characters() -> None:
    from app.pdf_parser import _clean_text

    assert _clean_text("UG\u201306 \u2014 2 \u00d7 UG-44 \u2212 1") == "UG-06 - 2 x UG-44 - 1"


def test_rate_card_misses_are_flagged_not_silent() -> None:
    content = _pdf_with_lines(["UG-06 - 2", "FX-11 - 3"])
    notes: list[str] = []
    totals = derive_code_totals(
        extract_text_blocks(content),
        code_catalog={("UG", "6"): "UG-06"},
        notes=notes,
    )
    assert totals == ["UG-06 - 2"]
    assert any("NOT in the loaded rate card" in n and "FX-11" in n for n in notes)


def test_novel_code_prefixes_are_flagged() -> None:
    content = _pdf_with_lines(["ZZQ-5 - 3", "UG-06 - 1"])
    notes: list[str] = []
    totals = derive_code_totals(extract_text_blocks(content), notes=notes)
    assert "ZZQ-05 - 3" in totals
    assert any("Unrecognized code prefixes" in n and "ZZQ" in n for n in notes)


def test_many_pages_beyond_parse_cap_warns_and_requires_review() -> None:
    doc = fitz.open()
    for _ in range(14):
        page = doc.new_page(width=1224, height=792)
        page.insert_text((600, 400), "UG-06 - 1")
        page.insert_text((100, 100), "Readable note line for the text layer check.")
    content = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(content)
    totals = derive_code_totals(blocks)
    diagnostics = diagnose_extraction(blocks, totals, total_pages=14)
    assert any("only the first 12" in w for w in diagnostics.warnings)
    assert diagnostics.review_required is True


def test_long_total_lists_shrink_font_instead_of_dropping_lines() -> None:
    summary = SummaryResult(
        model="t", confidence=1.0,
        job_totals=[f"UG-{i:02d} - {i}" for i in range(1, 61)],
    )
    output = annotate_pdf(_pdf_with_lines(["UG-06 - 1"]), summary)
    doc = fitz.open(stream=output, filetype="pdf")
    try:
        page = doc[0]
        a = [x for x in page.annots() or [] if "MKR" in str((x.info or {}).get("content", ""))][0]
        content_lines = a.info["content"].splitlines()
    finally:
        doc.close()
    assert len(content_lines) == 61  # title + all 60 codes, nothing dropped


# --- NR-702749 Segment 12 round 2 (Nick, 2026-06-10): spaced callouts ---

def test_spaced_code_callouts_are_counted() -> None:
    # Field crews hand-type codes with stray spaces around the dash.
    content = _pdf_with_lines([
        "UG- 6 - 1",       # the exact missed callout
        "UG- 6 - 3",
        "UG - 6 - 2",
        "UG -84 - 1",
        "Comp - 9 - 480'",
    ])
    totals = derive_code_totals(extract_text_blocks(content))
    assert "UG-06 - 6" in totals
    assert "UG-84 - 1" in totals
    assert "Comp-9 - 480" in totals


def test_spacing_tolerance_does_not_apply_to_unknown_prefixes() -> None:
    # Generic (unknown-prefix) matching stays strict; otherwise prose like
    # "Tie Point - 144" or "EOL - 48" would be totaled as codes.
    content = _pdf_with_lines([
        "Tie Point - 144 - 98",
        "EOL - 48 - 30",
        "UG-06 - 2",
    ])
    totals = derive_code_totals(extract_text_blocks(content))
    assert totals == ["UG-06 - 2"]


def test_spaced_codes_normalize_in_display() -> None:
    content = _pdf_with_lines(["UG- 6 - 1", "UG-06 - 1"])
    totals = derive_code_totals(extract_text_blocks(content))
    assert totals == ["UG-06 - 2"]  # one merged row, clean display


# --- BI-872022 re-run (2026-06-11): existing totals boxes must be ignored ---

def test_existing_mkr_totals_box_is_not_counted() -> None:
    doc = fitz.open()
    page = doc.new_page(width=1224, height=792)
    # Field callouts
    page.insert_text((600, 300), "UG-06 - 4")
    page.insert_text((600, 400), "UG-36 - 138'")
    # A previously stamped totals box (e.g. from an earlier run)
    page.add_freetext_annot(
        fitz.Rect(20, 20, 280, 200),
        "MKR Job Totals\nUG-06 - 4\nUG-36 - 138\nTL-20 - 2\nPC-02 - 1",
        fontsize=12,
    )
    content = doc.tobytes()
    doc.close()

    notes: list[str] = []
    totals = derive_code_totals(extract_text_blocks(content), notes=notes)
    assert "UG-06 - 4" in totals          # not 8
    assert "UG-36 - 138" in totals        # not 276
    assert not any(t.startswith("TL-20") or t.startswith("PC-02") for t in totals)
    assert any("re-run detected" in n for n in notes)


def test_split_title_existing_mkr_totals_box_is_not_counted() -> None:
    doc = fitz.open()
    page = doc.new_page(width=1224, height=792)
    page.insert_text((600, 300), "UG-06 - 4")
    page.add_freetext_annot(
        fitz.Rect(20, 20, 280, 200),
        "MKR Job\nTotals\nUG-06 - 4\nTL-20 - 2",
        fontsize=12,
    )
    content = doc.tobytes()
    doc.close()

    notes: list[str] = []
    totals = derive_code_totals(extract_text_blocks(content), notes=notes)
    assert totals == ["UG-06 - 4"]
    assert any("re-run detected" in n for n in notes)


def test_flattened_mkr_totals_box_lines_not_double_counted() -> None:
    # Nick, June-23 sync: an editor FLATTENED a previously stamped box so its title
    # and EACH code line became separate, individually positioned page-text blocks
    # (not one FreeText annotation). The title-only block was excluded but the
    # orphaned code lines below leaked back in as field callouts, doubling several
    # codes (29.76 vs 14.88). The whole box region must be excluded, not just the
    # title line.
    doc = fitz.open()
    page = doc.new_page(width=1224, height=792)
    # Real field callouts, in their own column on the drawing.
    page.insert_text((600, 300), "UG-44 - 156")
    page.insert_text((600, 340), "UG-06 - 4")
    # The flattened box: title + each line as SEPARATE page-text blocks in one
    # column (>= ~16pt apart so PyMuPDF does not regroup them into one block).
    y = 60
    for line in ["MKR Job Totals", "UG-44 - 156", "UG-06 - 4", "TL-20 - 2"]:
        page.insert_text((60, y), line)
        y += 18
    content = doc.tobytes()
    doc.close()

    notes: list[str] = []
    totals = derive_code_totals(extract_text_blocks(content), notes=notes)
    assert "UG-44 - 156" in totals      # not 312
    assert "UG-06 - 4" in totals         # not 8
    assert not any(t.startswith("TL-20") for t in totals)  # box-only line excluded
    assert any("re-run detected" in n for n in notes)


def test_flattened_box_does_not_eat_field_callouts_in_other_columns() -> None:
    # Guard against over-eager region growth: a real field callout that shares a
    # code with the flattened box but sits in a DIFFERENT column must still count.
    doc = fitz.open()
    page = doc.new_page(width=1224, height=792)
    page.insert_text((600, 300), "UG-06 - 4")   # genuine field callout (other column)
    y = 60
    for line in ["MKR Job Totals", "UG-06 - 4"]:  # flattened box in the left column
        page.insert_text((60, y), line)
        y += 18
    content = doc.tobytes()
    doc.close()

    totals = derive_code_totals(extract_text_blocks(content))
    assert "UG-06 - 4" in totals  # the real callout survives; the box copy is dropped


def test_existing_mkr_page_totals_box_is_not_counted() -> None:
    # Multi-page as-builts carry per-page "MKR Page Totals" boxes in addition to
    # the page-1 "MKR Job Totals" box. A re-run must not re-count those either
    # (Nick, June-23 sync: NR-996825 page-totals boxes drove the Comp-9 double).
    doc = fitz.open()
    page = doc.new_page(width=1224, height=792)
    page.insert_text((600, 300), "Comp-9 - 430")  # genuine field callout
    page.add_freetext_annot(
        fitz.Rect(20, 20, 280, 160),
        "MKR Page Totals\nComp-9 - 430\nUG-85 - 3",
        fontsize=12,
    )
    content = doc.tobytes()
    doc.close()

    notes: list[str] = []
    totals = derive_code_totals(extract_text_blocks(content), notes=notes)
    assert "Comp-9 - 430" in totals  # not 860
    assert not any(t.startswith("UG-85") for t in totals)  # box-only line excluded
    assert any("re-run detected" in n for n in notes)


def test_per_page_totals_partition_and_sum_to_job() -> None:
    # Page Totals (R1): each page totals only its own billing codes, reusing the
    # same aggregation as the job total so the per-page totals sum to the job
    # total. Validated against Nick's real NR-996825 (page totals 430 + 1058 =
    # job 1488). Page numbering is 1-based; pages with no codes are omitted.
    doc = fitz.open()
    p1 = doc.new_page(width=1224, height=792)
    p1.insert_text((600, 300), "UG-44 - 100")
    p1.insert_text((600, 340), "Comp-9 - 5")
    p2 = doc.new_page(width=1224, height=792)
    p2.insert_text((600, 300), "UG-44 - 56")
    p2.insert_text((600, 340), "Comp-6 - 2")
    doc.new_page(width=1224, height=792)  # page 3: no codes -> omitted
    content = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(content)
    job = derive_code_totals(blocks)
    pages = derive_code_totals_by_page(blocks)

    assert set(pages) == {1, 2}  # 1-based; the empty page 3 is omitted
    assert "UG-44 - 100" in pages[1] and "Comp-9 - 5" in pages[1]
    assert "UG-44 - 56" in pages[2] and "Comp-6 - 2" in pages[2]
    assert "UG-44 - 156" in job  # job sums across pages (100 + 56)
    # Page totals are billing codes only - never a materials/extras heading.
    assert all(
        not r.lower().startswith(("material", "user-"))
        for rows in pages.values()
        for r in rows
    )


def test_page_totals_boxes_stamped_on_later_pages_only_and_idempotent() -> None:
    # F4: each page after the first gets its own "MKR Page Totals" box; page 1
    # keeps Job Totals only; re-stamping the output replaces (never stacks) them.
    import re as _re

    doc = fitz.open()
    p1 = doc.new_page(width=1224, height=792)
    p1.insert_text((600, 300), "UG-44 - 100")
    p2 = doc.new_page(width=1224, height=792)
    p2.insert_text((600, 300), "UG-44 - 56")
    content = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(content)
    summary = SummaryResult(
        title="MKR Job Totals",
        job_totals=derive_code_totals(blocks),
        page_totals=derive_code_totals_by_page(blocks),
        model="t",
    )

    def box_titles(pdf: bytes) -> dict[int, list[str]]:
        d = fitz.open(stream=pdf, filetype="pdf")
        out: dict[int, list[str]] = {}
        for i in range(d.page_count):
            kinds = []
            for a in d[i].annots() or []:
                if a.type[1] != "FreeText":
                    continue
                norm = _re.sub(r"\s+", " ", (a.info.get("content") or "")).strip().lower()
                if norm.startswith("mkr job totals"):
                    kinds.append("job")
                elif norm.startswith("mkr page totals"):
                    kinds.append("page")
            out[i] = kinds
        d.close()
        return out

    once = box_titles(annotate_pdf(content, summary))
    assert once[0] == ["job"]   # page 1: Job Totals only
    assert once[1] == ["page"]  # page 2: its own Page Totals box

    twice = box_titles(annotate_pdf(annotate_pdf(content, summary), summary))
    assert twice[0] == ["job"]  # idempotent re-stamp: no duplicate boxes
    assert twice[1] == ["page"]


def test_replacement_rect_resizes_for_content_on_rotated_pages() -> None:
    # Isolated unit test: a re-stamp must size the box to fit the new content even
    # on rotated sheets, not reuse a stale (possibly narrow) rectangle. On a
    # rotated page the title's width maps to the rect HEIGHT, which must clear the
    # title so it never wraps. (NR-996825, rot=270.)
    from app.pdf_annotator import _replacement_rect_for_content

    doc = fitz.open()
    page = doc.new_page(width=792, height=612)
    page.set_rotation(270)
    # narrow stale anchor, in-bounds for the rotated page (page.rect is 612x792)
    stale_narrow = fitz.Rect(400, 10, 422, 130)  # 22pt wide - far too narrow for the title
    lines = ["MKR Page Totals", "Comp-9 - 734", "Comp-6 - 734"]
    rect = _replacement_rect_for_content(page, stale_narrow, lines, preferred_font_size=12)
    doc.close()

    title_w = fitz.get_text_length("MKR Page Totals", fontname="helv", fontsize=12)
    assert rect.height >= title_w  # rotation swap -> title fits on one line
    assert rect.width > stale_narrow.width  # not the stale narrow rectangle
    assert (rect.x0, rect.y0) == (stale_narrow.x0, stale_narrow.y0)  # position preserved


def test_restamp_rotated_existing_box_title_not_wrapped() -> None:
    # End-to-end guard for NR-996825 (rot=270): re-stamping a rotated page that
    # already has a NARROW totals box must produce a one-line title in the
    # annotation /Contents (identical to a fresh stamp), not "MKR Page\nTotals".
    # Covers both the page-1 Job box and a per-page Page Totals box replacement.
    doc = fitz.open()
    p1 = doc.new_page(width=792, height=612)
    p1.set_rotation(270)
    p1.insert_text((120, 300), "UG-44 - 5")
    p1.add_freetext_annot(fitz.Rect(740, 10, 762, 90), "MKR Job Totals\nUG-44 - 5", fontsize=10)
    p2 = doc.new_page(width=792, height=612)
    p2.set_rotation(270)
    p2.insert_text((120, 300), "UG-44 - 3")
    p2.add_freetext_annot(fitz.Rect(740, 10, 762, 90), "MKR Page Totals\nUG-44 - 3", fontsize=10)
    content = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(content)
    summary = SummaryResult(
        title="MKR Job Totals",
        job_totals=derive_code_totals(blocks),
        page_totals=derive_code_totals_by_page(blocks),
        model="t",
    )
    out = annotate_pdf(content, summary)

    d = fitz.open(stream=out, filetype="pdf")
    first_lines = []
    for i in range(d.page_count):
        for a in d[i].annots() or []:
            if a.type[1] != "FreeText":
                continue
            first = (a.info.get("content") or "").replace("\r", "\n").split("\n")[0].strip()
            if first.lower().startswith(("mkr job", "mkr page")):
                first_lines.append(first)
    d.close()

    assert "MKR Job Totals" in first_lines   # one-line job title (re-stamped)
    assert "MKR Page Totals" in first_lines  # one-line page title (re-stamped)
    # the wrapped forms must NOT appear as any box's first line
    assert "MKR Page" not in first_lines and "MKR Job" not in first_lines


def test_box_has_norotate_flag_on_unrotated_pages() -> None:
    # Nick's editor auto-rotates the box on drag/copy-paste for some permit
    # drawings (2026-06-11); NoRotate pins the orientation.
    output = annotate_pdf(_pdf_with_lines(["UG-06 - 1"]), _summary())
    doc = fitz.open(stream=output, filetype="pdf")
    try:
        page = doc[0]
        a = [x for x in page.annots() or [] if "MKR" in str((x.info or {}).get("content", ""))][0]
        assert a.flags & fitz.PDF_ANNOT_IS_NO_ROTATE
    finally:
        doc.close()


def test_rotated_pages_get_movable_annotation() -> None:
    # NR-1138768 (2026-06-15): Adobe shows baked boxes as stuck page ink,
    # absent from the Comments pane. Rotated sheets must still get a real
    # movable FreeText annotation; editor drag behavior is verified manually.
    doc = fitz.open()
    page = doc.new_page(width=1728, height=2592)
    page.insert_text((600, 1200), "UG-06 - 2")
    page.set_rotation(90)
    rotated = doc.tobytes()
    doc.close()

    out = annotate_pdf(rotated, _summary())
    doc = fitz.open(stream=out, filetype="pdf")
    try:
        page = doc[0]
        assert page.rotation == 90
        summary_annots = [
            a for a in page.annots() or []
            if str((a.info or {}).get("content", "")).startswith("MKR Job Totals")
        ]
        assert len(summary_annots) == 1
        summary_annot = summary_annots[0]
        assert summary_annot.type[1] == "FreeText"
        assert (doc.xref_get_key(summary_annot.xref, "Rotate")[1] or "") == "90"
        assert b"MKR Job Totals" not in page.read_contents()
    finally:
        doc.close()

    out = annotate_pdf(_pdf_with_lines(["UG-06 - 2"]), _summary())
    doc = fitz.open(stream=out, filetype="pdf")
    try:
        page = doc[0]
        assert len([a for a in page.annots() or [] if "MKR" in str((a.info or {}).get("content", ""))]) == 1
        assert b"MKR Job Totals" not in page.read_contents()
    finally:
        doc.close()


def test_nr_1138768_replaces_existing_rotated_totals_box() -> None:
    sample = Path(
        "/Users/javiervillaguardado/Downloads/New as built summation issue_15 Jun/"
        "Input/COAX-ASBUILT-(TelCyte)-NR-1138768 (1).pdf"
    )
    if not sample.exists():
        pytest.skip("NR-1138768 local sample PDF not available")

    content = sample.read_bytes()
    totals = derive_code_totals(extract_text_blocks(content))
    assert "UG-85 - 10" in totals

    summary = SummaryResult(model="parser-test", confidence=1.0, job_totals=totals)
    output = annotate_pdf(content, summary)
    assert any("previous box showed UG-85 - 9" in note for note in summary.informational_notes)
    assert summary.warnings == []

    doc = fitz.open(stream=output, filetype="pdf")
    try:
        page = doc[0]
        summary_annots = [
            a for a in page.annots() or []
            if str((a.info or {}).get("content", "")).startswith("MKR Job Totals")
        ]
        assert len(summary_annots) == 1
        annot = summary_annots[0]
        assert annot.type[1] == "FreeText"
        assert annot.rect.x0 <= 1
        assert (doc.xref_get_key(annot.xref, "Rotate")[1] or "") == "90"
        assert "UG-85 - 10" in str((annot.info or {}).get("content", ""))
        assert _top_totals_box_count(page) == 1
    finally:
        doc.close()


def _top_totals_box_count(page: fitz.Page) -> int:
    pix = page.get_pixmap(matrix=fitz.Matrix(0.35, 0.35), annots=True, alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    top_limit = int(image.height * 0.35)
    pixels = image.load()
    visited: set[tuple[int, int]] = set()
    components = 0
    for y in range(top_limit):
        for x in range(image.width):
            if (x, y) in visited or not _is_totals_green(pixels[x, y]):
                continue
            stack = [(x, y)]
            visited.add((x, y))
            count = 0
            while stack:
                px, py = stack.pop()
                count += 1
                for nx, ny in ((px + 1, py), (px - 1, py), (px, py + 1), (px, py - 1)):
                    if nx < 0 or ny < 0 or nx >= image.width or ny >= top_limit or (nx, ny) in visited:
                        continue
                    if _is_totals_green(pixels[nx, ny]):
                        visited.add((nx, ny))
                        stack.append((nx, ny))
            if count > 150:
                components += 1
    return components


def _is_totals_green(pixel: tuple[int, int, int]) -> bool:
    r, g, b = pixel
    return g > 220 and 170 <= r <= 230 and 130 <= b <= 210


# --- Email: BI-872022 Materials box (144ct part #, cable-type label, 10% buffer) ---


def _materials_box_lines(pdf_bytes: bytes) -> list[str]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            for annot in page.annots() or []:
                if annot.type[1] != "FreeText":
                    continue
                content = (annot.info.get("content") or "").strip()
                if content.lower().startswith("materials"):
                    return [ln.strip() for ln in content.replace("\r", "\n").split("\n") if ln.strip()]
        return []
    finally:
        doc.close()


def _pdf_with_materials_box(rows: list[str]) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=1728, height=2592)
    page.insert_text((100, 120), "Readable as-built notes for the text layer check.")
    page.insert_text((100, 160), "Crew completed restoration per plan and verified quantities.")
    rect = fitz.Rect(60, 200, 640, 200 + 26 * (len(rows) + 2))
    page.add_freetext_annot(rect, "Materials\n" + "\n".join(rows), fontsize=10)
    content = doc.tobytes()
    doc.close()
    return content


def test_bi872022_materials_box_remaps_legacy_part_labels_and_buffers() -> None:
    # Nick, BI-872022: the field-printed Materials box carries the OLD 144ct part number
    # (605-3324), no cable-type label, and the exact footage. The stamped box must remap
    # to the current 605-1502, add the (144Ct) label, and apply the 10% buffer rounded up
    # to the next 100'. A non-empty cable_footage list mirrors production (it triggers the
    # materials-box update even with INCLUDE_MATERIALS off).
    pdf = _pdf_with_materials_box(["470-9997 - 460'", "605-3277 - 4270'", "605-3324 - 1810'"])
    summary = SummaryResult(
        title="MKR Job Totals",
        job_totals=["UG-06 - 1"],
        cable_footage=[CableFootageLine(callout="144ct", display_type="144Ct", family="fiber")],
        model="parser-only",
    )

    lines = _materials_box_lines(annotate_pdf(pdf, summary))

    assert "605-1502 (144Ct) - 2000'" in lines  # 605-3324 -> 605-1502, labeled, 1810 -> 2000
    assert "605-3277 (48Ct) - 4700'" in lines  # 48ct labeled + 4270 -> 4700
    assert "470-9997 - 460'" in lines  # non-cable hardware untouched
    assert all("605-3324" not in line for line in lines)  # legacy part number is gone


def test_bi872022_materials_box_rerun_is_idempotent() -> None:
    pdf = _pdf_with_materials_box(["605-3277 - 4270'", "605-3324 - 1810'"])
    summary = SummaryResult(
        title="MKR Job Totals",
        job_totals=["UG-06 - 1"],
        cable_footage=[CableFootageLine(callout="144ct", display_type="144Ct", family="fiber")],
        model="parser-only",
    )

    first = annotate_pdf(pdf, summary)
    first_lines = _materials_box_lines(first)
    # Re-stamping the tool's own output must not re-buffer (2000 -> stays 2000).
    second_lines = _materials_box_lines(annotate_pdf(first, summary))

    assert first_lines == second_lines
    assert "605-1502 (144Ct) - 2000'" in second_lines


# --- Email: NR-996825 PRJ10 partial as-built (green "Previously Billed" + yellow "MKR New Totals") ---


def test_mkr_new_totals_title_predicate() -> None:
    assert starts_with_new_totals_title("MKR New Totals\nAdd resto\nComp-9 - 144'")
    assert not starts_with_new_totals_title("MKR Job Totals\nComp-9 - 144'")
    # The combined predicate (the parser's exclusion-from-counting rule) covers all three.
    assert starts_with_totals_title("MKR New Totals\nAdd resto")
    assert starts_with_totals_title("MKR Job Totals")
    assert starts_with_totals_title("MKR Page Totals")


def test_mkr_new_totals_box_is_not_counted_as_field_callouts() -> None:
    # Nick, PRJ10: a yellow "MKR New Totals - Add resto" summary box (the added-scope totals)
    # must never be summed on top of the field callouts. Overall = field callouts only, since
    # the field callouts already include every original + added segment.
    doc = fitz.open()
    page = doc.new_page(width=1224, height=792)
    page.insert_text((300, 300), "Comp-9 - 100'")
    page.insert_text((300, 330), "Comp-9 - 60'")  # field callouts overall = 160
    page.add_freetext_annot(
        fitz.Rect(60, 60, 360, 200),
        "MKR New Totals\nAdd resto\nComp-9 - 60'\nComp-6 - 60'",  # summary -> must be ignored
        fontsize=10,
    )
    content = doc.tobytes()
    doc.close()

    notes: list[str] = []
    totals = derive_code_totals(extract_text_blocks(content), notes=notes)

    assert "Comp-9 - 160" in totals  # 100 + 60; the box's own 60 is excluded
    assert not any(t.startswith("Comp-9 - 220") for t in totals)  # would be the double-count
    assert any("New Totals" in n for n in notes)


def test_mkr_new_totals_box_is_preserved_not_replaced() -> None:
    # The yellow box is the customer's annotation: excluded from counting but left on the page
    # (only "MKR Job/Page Totals" boxes are replaced by the tool).
    doc = fitz.open()
    page = doc.new_page(width=1224, height=792)
    page.insert_text((300, 300), "Comp-9 - 100'")
    page.add_freetext_annot(
        fitz.Rect(60, 60, 360, 200), "MKR New Totals\nAdd resto\nComp-9 - 60'", fontsize=10
    )
    content = doc.tobytes()
    doc.close()

    out = annotate_pdf(content, SummaryResult(title="MKR Job Totals", job_totals=["Comp-9 - 100"], model="x"))
    d = fitz.open(stream=out, filetype="pdf")
    titles = [
        (a.info.get("content") or "").strip()
        for a in (d[0].annots() or [])
        if a.type[1] == "FreeText"
    ]
    d.close()

    assert any(t.lower().startswith("mkr new totals") for t in titles)  # preserved
    assert any(t.lower().startswith("mkr job totals") for t in titles)  # tool box still stamped


def test_previously_billed_job_delta_ignores_page_level_reference_boxes() -> None:
    doc = fitz.open()
    page1 = doc.new_page(width=1224, height=792)
    page1.insert_text((300, 300), "Comp-9 - 1160'")
    page1.add_freetext_annot(
        fitz.Rect(60, 60, 360, 210),
        "MKR Job Totals\nPreviously Billed\nComp-9 - 832'",
        fontsize=10,
    )
    page2 = doc.new_page(width=1224, height=792)
    page2.add_freetext_annot(
        fitz.Rect(60, 60, 360, 230),
        "MKR Page Totals\nPreviously Billed\nComp-9 - 832'\nUG-85 - 5 (Due to PH depths)",
        fontsize=10,
    )
    content = doc.tobytes()
    doc.close()

    blocks = extract_text_blocks(content)
    previous = extract_previously_billed_job_totals(blocks)
    rows, warnings = derive_new_totals(["Comp-9 - 1160"], previous)

    assert previous == {("COMP", "9"): 832.0}
    assert rows == ["Comp-9 - 328"]
    assert warnings == []


def test_previously_billed_job_delta_ignores_duplicate_page_text_copy() -> None:
    content = "MKR Job Totals\nPreviously Billed\nComp-9 - 832'"
    blocks = [
        TextBlock(page=1, bbox=(0, 0, 200, 100), text=content, source="annotation"),
        TextBlock(page=1, bbox=(0, 0, 200, 100), text=content, source="page"),
    ]

    previous = extract_previously_billed_job_totals(blocks)
    rows, warnings = derive_new_totals(["Comp-9 - 1160"], previous)

    assert previous == {("COMP", "9"): 832.0}
    assert rows == ["Comp-9 - 328"]
    assert warnings == []


def test_negative_previously_billed_delta_warns_without_new_total() -> None:
    rows, warnings = derive_new_totals(["Comp-9 - 1160"], {("COMP", "9"): 1664.0})

    assert rows == []
    assert any("higher than the recomputed total" in warning for warning in warnings)


def test_previously_billed_delta_includes_brand_new_codes() -> None:
    rows, warnings = derive_new_totals(
        [
            "UG-06 - 5",
            "UG-80 - 924",
            "UG-03 - 2049",
            "UG-44 - 899",
            "Comp-9 - 1160",
            "Comp-6 - 1160",
            "UG-38 - 4640",
            "UG-60 - 780",
            "UG-85 - 12",
        ],
        {
            ("COMP", "9"): 832.0,
            ("COMP", "6"): 832.0,
            ("UG", "38"): 3328.0,
            ("UG", "85"): 12.0,
            ("UG", "80"): 269.0,
            ("UG", "3"): 1139.0,
            ("UG", "44"): 443.0,
            ("UG", "60"): 414.0,
        },
    )

    assert rows == [
        "UG-06 - 5",
        "UG-80 - 655",
        "UG-03 - 910",
        "UG-44 - 456",
        "Comp-9 - 328",
        "Comp-6 - 328",
        "UG-38 - 1312",
        "UG-60 - 366",
    ]
    assert warnings == []


def test_previously_billed_boxes_are_preserved_and_tool_new_totals_replace_on_rerun() -> None:
    doc = fitz.open()
    page1 = doc.new_page(width=1224, height=792)
    page1.add_freetext_annot(
        fitz.Rect(60, 60, 360, 210),
        "MKR Job Totals\nPreviously Billed\nComp-9 - 832'",
        fontsize=10,
    )
    page2 = doc.new_page(width=1224, height=792)
    page2.add_freetext_annot(
        fitz.Rect(60, 60, 360, 230),
        "MKR Page Totals\nPreviously Billed\nComp-9 - 832'",
        fontsize=10,
    )
    content = doc.tobytes()
    doc.close()
    summary = SummaryResult(
        title="MKR Job Totals",
        job_totals=["Comp-9 - 1160"],
        new_totals=["Comp-9 - 328"],
        page_totals={2: ["Comp-9 - 832"]},
        model="x",
    )

    out = annotate_pdf(content, summary)
    again = annotate_pdf(out, summary)
    d = fitz.open(stream=again, filetype="pdf")
    try:
        contents = [
            (a.info.get("content") or "").replace("\r", "\n").strip()
            for page in d
            for a in (page.annots() or [])
            if a.type[1] == "FreeText"
        ]
    finally:
        d.close()

    assert sum(is_previously_billed_totals_box(content) for content in contents) == 2
    assert sum(is_tool_new_totals_box(content) for content in contents) == 1
    assert any(content == "MKR New Totals\nAdditions\nComp-9 - 328" for content in contents)
    assert not any(content == "MKR Job Totals\nComp-9 - 1160" for content in contents)
    assert not any(content == "MKR Page Totals\nComp-9 - 832" for content in contents)


def test_nick_authored_new_totals_box_is_preserved_when_tool_stamps_its_own() -> None:
    doc = fitz.open()
    page = doc.new_page(width=1224, height=792)
    page.add_freetext_annot(
        fitz.Rect(60, 60, 360, 200),
        "MKR New Totals\nAdd resto\nComp-9 - 60'",
        fontsize=10,
    )
    content = doc.tobytes()
    doc.close()

    out = annotate_pdf(content, SummaryResult(model="x", new_totals=["Comp-9 - 328"]))
    d = fitz.open(stream=out, filetype="pdf")
    try:
        contents = [
            (a.info.get("content") or "").replace("\r", "\n").strip()
            for a in (d[0].annots() or [])
            if a.type[1] == "FreeText"
        ]
    finally:
        d.close()

    assert any(content.startswith("MKR New Totals\nAdd resto") for content in contents)
    assert sum(is_tool_new_totals_box(content) for content in contents) == 1
