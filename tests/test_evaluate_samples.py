from __future__ import annotations

import importlib.util
from pathlib import Path

import fitz


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_samples.py"
spec = importlib.util.spec_from_file_location("evaluate_samples", SCRIPT_PATH)
evaluate_samples = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(evaluate_samples)


def _text_pdf(path: Path, lines: list[str]) -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    for index, line in enumerate(lines):
        page.insert_text((72, 72 + index * 24), line)
    doc.save(path)
    doc.close()


def test_added_text_finds_team_inserted_summary_text(tmp_path: Path) -> None:
    before = tmp_path / "before.pdf"
    after = tmp_path / "after.pdf"
    _text_pdf(before, ["Existing drawing label", "UG-56 - 170'"])
    _text_pdf(after, ["Existing drawing label", "UG-56 - 170'", "MKR Job Totals", "UG-07 - 1"])

    diff_text = evaluate_samples.expected_added_text(before, after)

    assert "MKR Job Totals" in diff_text
    assert "UG-07 - 1" in diff_text


def test_compare_total_text_reports_missing_and_extra_normalized_totals() -> None:
    actual = "MKR Job Totals\nUG-7 - 1\nCOMP-9 - 2"
    expected = "MKR Job Totals\nUG-07 - 1\nCOMP-09 - 2\nUG-56 - 170'"

    comparison = evaluate_samples.compare_total_text(actual, expected)

    assert comparison["actual_total_count"] == 2
    assert comparison["expected_total_count"] == 3
    assert comparison["missing_totals"] == ["COMP-09 - 2", "UG-56 - 170'"]
    assert comparison["extra_totals"] == ["COMP-9 - 2"]


def test_find_pairs_matches_totals_removed_to_team_output(tmp_path: Path) -> None:
    before = tmp_path / "FIBER-ASBUILT-(TelCyte)-BI-000001-Totals Removed.pdf"
    after = tmp_path / "FIBER-ASBUILT-(TelCyte)-BI-000001.pdf"
    before.write_bytes(b"%PDF-1.4 placeholder")
    after.write_bytes(b"%PDF-1.4 placeholder")

    assert evaluate_samples.find_pairs(tmp_path) == [(before, after)]
