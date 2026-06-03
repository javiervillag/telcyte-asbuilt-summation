from __future__ import annotations

import importlib.util
from pathlib import Path

import fitz


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_samples.py"
spec = importlib.util.spec_from_file_location("evaluate_samples", SCRIPT_PATH)
evaluate_samples = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(evaluate_samples)


class _FakeHealthResponse:
    status_code = 200

    def json(self) -> dict:
        return {"ok": True, "model": "anthropic/claude-sonnet-4"}


class _FakeHealthClient:
    def get(self, path: str):
        assert path == "/health"
        return _FakeHealthResponse()


class _FakeManualReviewResponse:
    status_code = 422
    headers = {"content-type": "application/json"}
    text = ""

    def json(self) -> dict:
        return {
            "detail": "Manual review required.",
            "warnings": [
                "OpenRouter verifier reviewed unresolved callouts but could not clear them from parsed evidence.",
                "Manual review is required; the app did not add unsupported totals.",
            ],
            "supported_totals": ["UG-56 - 170'"],
            "unresolved_callouts": ["EOL - 48Ct - 30'"],
            "verifier_model": "anthropic/claude-sonnet-4",
            "verifier_used": True,
            "diagnostics": {"review_required": True, "code_total_count": 1},
        }


class _FakeManualReviewClient:
    def post(self, path: str, files: dict):
        assert path == "/api/summarize"
        return _FakeManualReviewResponse()


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


def test_missing_total_evidence_classifies_input_support() -> None:
    input_text = "Construction note\nFB-4 storage note\nUG-56 - 170'\n13 fiber callout"
    missing = ["FB-04 - 6", "COMP-13 - 13", "UG-56 - 170'"]

    evidence = evaluate_samples.classify_missing_total_evidence(input_text, missing)

    by_total = {item["total"]: item for item in evidence}
    assert by_total["FB-04 - 6"]["code_present"] is True
    assert by_total["FB-04 - 6"]["evidence_class"] == "billing_code_text_without_matching_total"
    assert by_total["FB-04 - 6"]["exact_total_present"] is False
    assert by_total["COMP-13 - 13"]["code_present"] is False
    assert by_total["COMP-13 - 13"]["quantity_present"] is True
    assert by_total["COMP-13 - 13"]["evidence_class"] == "quantity_text_without_billing_code"
    assert by_total["UG-56 - 170'"]["exact_total_present"] is True
    assert by_total["UG-56 - 170'"]["evidence_class"] == "direct_total_text"


def test_missing_total_evidence_ignores_tiny_unitless_quantities() -> None:
    evidence = evaluate_samples.classify_missing_total_evidence(
        "UG-7 - 1\nCD-1 - 1\nrandom 6",
        ["PC-01 - 1", "FB-04 - 6"],
    )

    assert all(item["quantity_present"] is False for item in evidence)


def test_missing_total_evidence_marks_unresolved_callout_context() -> None:
    evidence = evaluate_samples.classify_missing_total_evidence(
        "EOL - 48Ct - 30'\nStorage - 48Ct - 50'",
        ["FB-04 - 6"],
        ["EOL - 48Ct - 30'", "Storage - 48Ct - 50'"],
    )

    assert evidence == [
        {
            "total": "FB-04 - 6",
            "evidence_class": "unresolved_construction_callout_context",
            "exact_total_present": False,
            "code_present": False,
            "quantity_present": False,
            "unresolved_callout_context": True,
            "related_unresolved_callouts": ["EOL - 48Ct - 30'", "Storage - 48Ct - 50'"],
            "matching_lines": [],
        }
    ]


def test_health_status_records_endpoint_health() -> None:
    status = evaluate_samples.health_status(_FakeHealthClient())

    assert status == {
        "ok": True,
        "status_code": 200,
        "body": {"ok": True, "model": "anthropic/claude-sonnet-4"},
    }


def test_evaluate_pair_records_manual_review_warning_text(tmp_path: Path) -> None:
    before = tmp_path / "sample-Totals Removed.pdf"
    after = tmp_path / "sample.pdf"
    _text_pdf(before, ["Existing note", "UG-56 - 170'"])
    _text_pdf(after, ["Existing note", "UG-56 - 170'", "MKR Job Totals", "UG-56 - 170'"])

    result = evaluate_samples.evaluate_pair(_FakeManualReviewClient(), before, after, tmp_path / "out")

    assert result["result"] == "manual_review"
    assert result["warning_count"] == 2
    assert result["warnings"] == [
        "OpenRouter verifier reviewed unresolved callouts but could not clear them from parsed evidence.",
        "Manual review is required; the app did not add unsupported totals.",
    ]
    assert result["supported_totals"] == ["UG-56 - 170'"]
    assert result["unresolved_callouts"] == ["EOL - 48Ct - 30'"]


def test_find_pairs_matches_totals_removed_to_team_output(tmp_path: Path) -> None:
    before = tmp_path / "FIBER-ASBUILT-(TelCyte)-BI-000001-Totals Removed.pdf"
    after = tmp_path / "FIBER-ASBUILT-(TelCyte)-BI-000001.pdf"
    before.write_bytes(b"%PDF-1.4 placeholder")
    after.write_bytes(b"%PDF-1.4 placeholder")

    assert evaluate_samples.find_pairs(tmp_path) == [(before, after)]
