from __future__ import annotations

import fitz
import pytest

from app.box_titles import (
    TotalsTitleKind,
    is_legacy_totals_alias,
    is_previously_billed_totals_box,
    starts_with_job_totals_title,
    starts_with_materials_title,
    starts_with_page_totals_title,
    starts_with_totals_title,
    totals_title_kind,
)
from app.models import SummaryResult
from app.pdf_annotator import annotate_pdf
from app.pdf_parser import TextBlock, diagnose_extraction, derive_code_totals, extract_text_blocks


@pytest.mark.parametrize(
    ("title", "kind", "legacy"),
    [
        ("MKR Job Total", TotalsTitleKind.JOB, True),
        ("MKR Job Totals", TotalsTitleKind.JOB, False),
        ("MKR Job\nTotals", TotalsTitleKind.JOB, False),
        ("Pg 1 Total", TotalsTitleKind.PAGE, True),
        ("Pg 2 Totals", TotalsTitleKind.PAGE, True),
        ("Page 3 Total", TotalsTitleKind.PAGE, True),
        ("MKR Page Totals", TotalsTitleKind.PAGE, False),
    ],
)
def test_verified_totals_titles_are_classified(title: str, kind: TotalsTitleKind, legacy: bool) -> None:
    assert totals_title_kind(title) == kind
    assert starts_with_totals_title(title)
    assert is_legacy_totals_alias(title) is legacy


@pytest.mark.parametrize(
    "title",
    [
        "Construction Total",
        "Total Bore Length",
        "Job Cost Total",
        "Subtotal",
        "MKR Job Totalizer",
        "Dirt - UG-6 - 1\nTotal restoration area",
    ],
)
def test_unverified_titles_are_not_automatic_totals_aliases(title: str) -> None:
    assert totals_title_kind(title) is None
    assert not starts_with_totals_title(title)
    assert not is_legacy_totals_alias(title)


def test_protected_output_box_titles_keep_their_existing_contract() -> None:
    assert is_previously_billed_totals_box("MKR Job Totals - Previously Billed\nUG-06 - 4")
    assert is_previously_billed_totals_box("MKR Page Totals - Previously Billed\nUG-06 - 4")
    assert starts_with_totals_title("MKR New Totals\nAdd resto\nUG-06 - 4")
    assert not starts_with_job_totals_title("MKR New Totals\nAdd resto\nUG-06 - 4")
    assert not starts_with_page_totals_title("MKR New Totals\nAdd resto\nUG-06 - 4")
    assert starts_with_materials_title("Materials\n605-3277 (48Ct) - 1000'")


def _nr_702749_blocks() -> list[TextBlock]:
    return [
        TextBlock(1, (20, 20, 260, 150), "MKR Job Total\nUG-37 - 1852\nUG-6 - 85", "annotation"),
        TextBlock(2, (20, 20, 260, 120), "Pg 1 Total\nUG-6 - 10", "annotation"),
        TextBlock(3, (20, 20, 260, 140), "Pg 2 Total\nUG-37 - 1192\nUG-6 - 40", "annotation"),
        TextBlock(4, (20, 20, 260, 140), "Pg 3 Total\nUG-37 - 660\nUG-6 - 35", "annotation"),
        TextBlock(1, (500, 220, 700, 260), "UG-85 - 14", "page"),
        TextBlock(2, (500, 220, 700, 260), "UG-6 - 18", "page"),
        TextBlock(2, (500, 280, 700, 320), "Comp-9 - 1852\nComp-6 - 1852", "page"),
        TextBlock(4, (500, 220, 700, 260), "UG-38 - 3704", "page"),
    ]


def test_nr_702749_legacy_tallies_are_excluded_and_force_review_on_conflict() -> None:
    notes: list[str] = []
    warnings: list[str] = []
    issues = []
    totals = derive_code_totals(
        _nr_702749_blocks(),
        notes=notes,
        warnings=warnings,
        issues=issues,
    )

    assert totals == [
        "UG-85 - 14",
        "UG-06 - 18",
        "Comp-9 - 1852",
        "Comp-6 - 1852",
        "UG-38 - 3704",
    ]
    assert not any(row.startswith("UG-37") for row in totals)
    assert any(issue.code == "legacy_totals_ignored" and issue.severity == "notice" for issue in issues)
    assert any(issue.code == "summary_conflict" and issue.severity == "blocker" for issue in issues)

    diagnostics = diagnose_extraction(
        _nr_702749_blocks(),
        totals,
        parser_notes=notes,
        parser_warnings=warnings,
        parser_issues=issues,
    )
    assert diagnostics.review_required
    assert any(issue.code == "summary_conflict" for issue in diagnostics.issues)


def test_matching_legacy_job_tally_is_notice_only() -> None:
    blocks = [
        TextBlock(1, (20, 20, 220, 120), "MKR Job Total\nUG-6 - 4", "annotation"),
        TextBlock(1, (350, 300, 450, 330), "UG-6 - 4", "page"),
    ]
    notes: list[str] = []
    warnings: list[str] = []
    issues = []

    assert derive_code_totals(blocks, notes=notes, warnings=warnings, issues=issues) == ["UG-06 - 4"]
    assert warnings == []
    assert [issue.code for issue in issues] == ["legacy_totals_ignored"]
    assert issues[0].severity == "notice"


def test_singular_previously_billed_box_is_protected_from_conflict_diagnostic() -> None:
    blocks = [
        TextBlock(
            1,
            (20, 20, 220, 120),
            "MKR Job Total - Previously Billed\nUG-6 - 99",
            "annotation",
        ),
        TextBlock(1, (350, 300, 450, 330), "UG-6 - 4", "page"),
    ]
    issues = []

    assert derive_code_totals(blocks, issues=issues) == ["UG-06 - 4"]
    assert not any(issue.code in {"legacy_totals_ignored", "summary_conflict"} for issue in issues)


def test_unknown_total_like_box_is_preserved_for_review_without_hiding_field_callout() -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((350, 300), "UG-06 - 4")
    page.add_freetext_annot(
        fitz.Rect(20, 20, 240, 120),
        "Construction Total\nUG-06 - 4",
        fontsize=10,
    )
    content = doc.tobytes()
    doc.close()

    issues = []
    totals = derive_code_totals(extract_text_blocks(content), issues=issues)

    assert totals == ["UG-06 - 4"]
    assert any(issue.code == "unclassified_totals_summary" for issue in issues)


def _legacy_boxes_pdf() -> bytes:
    doc = fitz.open()
    p1 = doc.new_page(width=612, height=792)
    p1.add_freetext_annot(fitz.Rect(20, 20, 220, 150), "MKR Job Total\nUG-37 - 1852", fontsize=10)
    p1.add_freetext_annot(
        fitz.Rect(240, 20, 440, 150),
        "MKR Job Totals - Previously Billed\nUG-06 - 2",
        fontsize=10,
    )
    p1.add_freetext_annot(fitz.Rect(20, 600, 220, 740), "Materials\nLg Ped - 2", fontsize=10)
    p1.add_freetext_annot(fitz.Rect(240, 600, 440, 740), "MKR New Totals\nAdd resto\nUG-06 - 1", fontsize=10)
    p2 = doc.new_page(width=612, height=792)
    p2.add_freetext_annot(fitz.Rect(20, 20, 220, 150), "Pg 1 Total\nUG-06 - 10", fontsize=10)
    p3 = doc.new_page(width=612, height=792)
    p3.add_freetext_annot(fitz.Rect(20, 20, 220, 150), "Page 2 Totals\nUG-06 - 8", fontsize=10)
    content = doc.tobytes()
    doc.close()
    return content


def _annotation_contents(pdf_bytes: bytes) -> list[list[str]]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [
            [str((annot.info or {}).get("content") or "") for annot in page.annots() or []]
            for page in doc
        ]
    finally:
        doc.close()


def test_legacy_boxes_are_replaced_in_place_while_protected_boxes_survive_and_rerun_is_idempotent() -> None:
    summary = SummaryResult(
        model="parser-test",
        confidence=1.0,
        job_totals=["UG-06 - 18"],
        page_totals={2: ["UG-06 - 10"], 3: ["UG-06 - 8"]},
    )

    first = annotate_pdf(_legacy_boxes_pdf(), summary)
    second = annotate_pdf(first, summary)
    contents = _annotation_contents(second)

    assert sum(text.startswith("MKR Job Totals\nUG-06 - 18") for text in contents[0]) == 1
    assert sum(text.startswith("MKR Page Totals") for page in contents[1:] for text in page) == 2
    assert not any(text.startswith(("MKR Job Total\n", "Pg 1 Total", "Page 2 Totals")) for page in contents for text in page)
    assert any("Previously Billed" in text for text in contents[0])
    assert any(text.startswith("MKR New Totals\nAdd resto") for text in contents[0])
    assert any(text.startswith("Materials\nLg Ped - 2") for text in contents[0])
