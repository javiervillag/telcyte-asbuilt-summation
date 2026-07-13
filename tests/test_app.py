from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.evidence import finalize_material_evidence
from app.main import _finalize_evidence_for_output, _result_summary_header, _result_summary_payload, app
from app.models import (
    CableFootageItem,
    CableFootageLine,
    MaterialEvidence,
    SummaryEvidence,
    SummaryIssue,
    SummaryResult,
)
from app.openrouter_client import ManualReviewRequired
from app.pdf_annotator import PlacementReviewRequired


SAMPLE = Path("/Users/javiervillaguardado/Downloads/Asbuilt Examples for AI Summation/FIBER-ASBUILT-(TelCyte)-BI-829050-Totals Removed.pdf")


def _cable_line(*, eligible: bool = True, review_flags: list[str] | None = None) -> CableFootageLine:
    return CableFootageLine(
        callout="48ct",
        display_type="48Ct",
        part_number="605-3277",
        family="fiber",
        path_segments=[
            CableFootageItem(label="Comp-15", page=1, feet=1200, source="Comp-15 - 1200'"),
            CableFootageItem(label="Comp-15", page=1, feet=28, source="Comp-15 - 28'"),
        ],
        storage_items=[
            CableFootageItem(label="EOL", page=1, feet=122, source="EOL - 48Ct - 122'"),
            CableFootageItem(label="Storage", page=1, feet=100, source="Storage - 48Ct - 100'"),
            CableFootageItem(label="Tie Point", page=1, feet=68, source="Tie Point - 48Ct - 68'"),
        ],
        path_subtotal=1228,
        storage_subtotal=290,
        total_ft=1700,
        material_line="605-3277 (48Ct) - 1700'",
        eligible_for_stamp=eligible,
        source_pages=[1],
        confidence=0.92 if eligible else 0.55,
        review_flags=review_flags or [],
    )


def test_health_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_rejects_non_pdf_upload() -> None:
    client = TestClient(app)
    response = client.post("/api/summarize", files={"file": ("note.txt", b"hello", "text/plain")})
    assert response.status_code == 400


def test_rejects_invalid_pdf_upload() -> None:
    client = TestClient(app)
    response = client.post("/api/summarize", files={"file": ("bad.pdf", b"not a pdf", "application/pdf")})
    assert response.status_code == 400
    assert "valid PDF" in response.json()["detail"]


def test_summarize_endpoint_returns_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_summarize(content, settings, source_name=None):
        return SummaryResult(
            model="parser+fake-model",
            confidence=0.91,
            job_totals=["UG-56 - 170'"],
            materials=[],
        )

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        files={"file": ("sample.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["x-telcyte-model"] == "parser+fake-model"
    assert response.headers["x-telcyte-warnings"] == "[]"
    result_summary = json.loads(response.headers["x-telcyte-result-summary"])
    assert result_summary["output_name"] == "sample-telcyte-summary.pdf"
    assert result_summary["detected_totals"] == ["UG-56 - 170'"]
    assert result_summary["extra_billing_codes"] == []
    assert result_summary["result_lines"] == ["MKR Job Totals", "UG-56 - 170'"]


@pytest.mark.parametrize("processing_path", ["success", "manual_review", "manual_review_with_extras"])
def test_placement_visibility_failure_is_specific_and_persisted_on_every_processing_path(
    monkeypatch: pytest.MonkeyPatch,
    processing_path: str,
) -> None:
    message = "Materials on page 1 fell outside the visible page bounds (0.0, -10.0, 100.0, 30.0)."
    logged: dict = {}

    async def fake_summarize(content, settings, source_name=None):
        if processing_path.startswith("manual_review"):
            raise ManualReviewRequired(
                ["Parser review remains."],
                supported_totals=["UG-28 - 1"],
                materials=["450-0323 (UG-28) - 1"],
            )
        return SummaryResult(
            model="parser+fake-model",
            job_totals=["UG-28 - 1"],
            materials=["450-0323 (UG-28) - 1"],
        )

    def fake_annotate(content, summary, source_name=None):
        summary.issues.append(
            SummaryIssue(
                severity="blocker",
                code="output_box_off_page",
                message=message,
                subject="Materials",
            )
        )
        raise PlacementReviewRequired(message)

    def fake_log_run_attempt(**kwargs):
        logged.update(kwargs)

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    monkeypatch.setattr("app.main.annotate_pdf", fake_annotate)
    monkeypatch.setattr("app.main._log_run_attempt", fake_log_run_attempt)

    data = None
    if processing_path == "manual_review_with_extras":
        data = {"extra_billing_codes": json.dumps([{"code": "PC-02", "quantity": "1"}])}
    response = TestClient(app).post(
        "/api/summarize",
        data=data,
        files={"file": ("rotated.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 422
    assert response.json()["warnings"] == [message]
    assert logged["status"] == "manual_review"
    assert logged["error_type"] == "placement_review"
    assert logged["error_message"] == message
    assert any(issue.code == "output_box_off_page" for issue in logged["summary"].issues)


def test_extra_billing_code_catalog_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/api/extra-billing-codes")
    assert response.status_code == 200
    categories = response.json()["categories"]
    codes = [item for category in categories for item in category["codes"]]
    pc02 = next(item for item in codes if item["code"] == "PC-02")
    assert pc02["category"] == "Preconstruction"
    assert "White" in pc02["name"]
    assert pc02["unit"] == "each"


def test_summarize_endpoint_does_not_add_extras_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, SummaryResult] = {}

    async def fake_summarize(content, settings, source_name=None):
        return SummaryResult(
            model="parser+fake-model",
            confidence=0.91,
            job_totals=["UG-56 - 170'"],
            materials=[],
        )

    def fake_annotate(content, summary, source_name=None):
        captured["summary"] = summary
        return b"%PDF-1.4 fake"

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    monkeypatch.setattr("app.main.annotate_pdf", fake_annotate)

    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        files={"file": ("sample.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 200
    assert captured["summary"].job_totals == ["UG-56 - 170'"]
    assert captured["summary"].extra_totals == []
    assert captured["summary"].extra_notes == []


def test_summarize_endpoint_adds_selected_extras_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, SummaryResult] = {}

    async def fake_summarize(content, settings, source_name=None):
        return SummaryResult(
            model="parser+fake-model",
            confidence=0.91,
            job_totals=["UG-56 - 170'"],
            materials=[],
        )

    def fake_annotate(content, summary, source_name=None):
        captured["summary"] = summary
        return b"%PDF-1.4 fake"

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    monkeypatch.setattr("app.main.annotate_pdf", fake_annotate)

    extras = [{"code": "PC-02", "quantity": "1", "note": "White lining confirmed by field note."}]
    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        data={"extra_billing_codes": json.dumps(extras)},
        files={"file": ("sample.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 200
    assert captured["summary"].job_totals == ["UG-56 - 170'"]
    assert captured["summary"].extra_totals == ["PC-02 - 1"]
    assert captured["summary"].extra_notes == ["PC-02: White lining confirmed by field note."]
    result_summary = json.loads(response.headers["x-telcyte-result-summary"])
    assert result_summary["detected_totals"] == ["UG-56 - 170'"]
    assert result_summary["extra_billing_codes"] == ["PC-02 - 1"]
    assert result_summary["result_lines"] == [
        "MKR Job Totals",
        "UG-56 - 170'",
        "User-selected extra totals",
        "PC-02 - 1",
        "Extra notes",
        "PC-02: White lining confirmed by field note.",
    ]
    assert captured["summary"].display_lines() == [
        "MKR Job Totals",
        "UG-56 - 170'",
        "User-selected extra totals",
        "PC-02 - 1",
        "Extra notes",
        "PC-02: White lining confirmed by field note.",
    ]


def test_selected_extras_allow_supported_totals_pdf_after_manual_review(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, SummaryResult] = {}

    async def fake_summarize(content, settings, source_name=None):
        raise ManualReviewRequired(
            ["Unresolved callouts remain."],
            supported_totals=["UG-56 - 170'"],
            unresolved_callouts=["EOL - 48Ct - 30'"],
        )

    def fake_annotate(content, summary, source_name=None):
        captured["summary"] = summary
        return b"%PDF-1.4 fake"

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    monkeypatch.setattr("app.main.annotate_pdf", fake_annotate)

    extras = [{"code": "FB-04", "quantity": "6", "note": "Confirmed 48-count splice group."}]
    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        data={"extra_billing_codes": json.dumps(extras)},
        files={"file": ("sample.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 200
    assert captured["summary"].job_totals == ["UG-56 - 170'"]
    assert captured["summary"].extra_totals == ["FB-04 - 6"]
    assert captured["summary"].extra_notes == ["FB-04: Confirmed 48-count splice group."]
    assert captured["summary"].warnings == ["Unresolved callouts remain."]
    result_summary = json.loads(response.headers["x-telcyte-result-summary"])
    assert result_summary["detected_totals"] == ["UG-56 - 170'"]
    assert captured["summary"].warnings == ["Unresolved callouts remain."]
    assert result_summary["extra_billing_codes"] == ["FB-04 - 6"]
    assert result_summary["result_lines"] == [
        "MKR Job Totals",
        "UG-56 - 170'",
        "User-selected extra totals",
        "FB-04 - 6",
        "Extra notes",
        "FB-04: Confirmed 48-count splice group.",
    ]


def test_manual_review_with_extras_preserves_new_totals_and_review_materials(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, SummaryResult] = {}

    async def fake_summarize(content, settings, source_name=None):
        raise ManualReviewRequired(
            ["Cable material needs review for 48Ct."],
            supported_totals=["Comp-9 - 1160"],
            unresolved_callouts=[],
            cable_footage=[
                CableFootageLine(
                    callout="48ct",
                    display_type="48Ct",
                    part_number="605-3277",
                    family="fiber",
                    storage_subtotal=236,
                    review_material_line="605-3277 (48Ct) - VERIFY",
                    review_flags=["No supported pulled-path footage was found for 48Ct."],
                )
            ],
            new_totals=["Comp-9 - 328"],
        )

    def fake_annotate(content, summary, source_name=None):
        captured["summary"] = summary
        return b"%PDF-1.4 fake"

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    monkeypatch.setattr("app.main.annotate_pdf", fake_annotate)

    extras = [{"code": "FB-04", "quantity": "6", "note": "Confirmed 48-count splice group."}]
    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        data={"extra_billing_codes": json.dumps(extras)},
        files={"file": ("sample.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 200
    assert captured["summary"].new_totals == ["Comp-9 - 328"]
    assert captured["summary"].materials == ["605-3277 (48Ct) - VERIFY"]
    result_summary = json.loads(response.headers["x-telcyte-result-summary"])
    assert result_summary["new_totals"] == ["Comp-9 - 328"]
    assert result_summary["materials"] == ["605-3277 (48Ct) - VERIFY"]


def test_summarize_endpoint_accepts_manual_extra_code(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, SummaryResult] = {}

    async def fake_summarize(content, settings, source_name=None):
        return SummaryResult(
            model="parser+fake-model",
            confidence=0.91,
            job_totals=["UG-56 - 170'"],
            materials=[],
        )

    def fake_annotate(content, summary, source_name=None):
        captured["summary"] = summary
        return b"%PDF-1.4 fake"

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    monkeypatch.setattr("app.main.annotate_pdf", fake_annotate)

    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        data={"extra_billing_codes": json.dumps([{"code": "XX", "quantity": "1"}])},
        files={"file": ("sample.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 200
    assert captured["summary"].extra_totals == ["XX - 1"]


def test_summarize_endpoint_rejects_malformed_manual_extra_code(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_summarize(content, settings, source_name=None):
        raise AssertionError("summarize should not run for invalid extra codes")

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)

    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        data={"extra_billing_codes": json.dumps([{"code": "BAD CODE!", "quantity": "1"}])},
        files={"file": ("sample.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 400
    assert "not a valid" in response.json()["detail"]


def test_summarize_endpoint_reports_manual_review(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_summarize(content, settings, source_name=None):
        raise ManualReviewRequired(
            ["This PDF does not have enough readable text for automatic summation."],
            supported_totals=[],
            unresolved_callouts=["EOL - 48Ct - 66'"],
        )

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        files={"file": ("blank.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )
    assert response.status_code == 422
    body = response.json()
    assert "manual review" in body["detail"].lower()
    assert body["warnings"]
    assert body["supported_totals"] == []
    assert body["unresolved_callouts"] == ["EOL - 48Ct - 66'"]
    assert body["result_summary"]["output_name"] == ""
    assert body["result_summary"]["detected_totals"] == []
    assert body["result_summary"]["extra_billing_codes"] == []
    assert body["result_summary"]["run_id"]
    assert body["result_summary"]["preview_pages"] == []
    assert body["result_summary"]["result_lines"] == ["MKR Job Totals"]
    assert body["warnings"] == ["This PDF does not have enough readable text for automatic summation."]


def test_manual_review_with_supported_totals_returns_review_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, SummaryResult] = {}

    async def fake_summarize(content, settings, source_name=None):
        raise ManualReviewRequired(
            ["Unresolved callouts require review."],
            supported_totals=["UG-06 - 13"],
            unresolved_callouts=["EOL - 48Ct - 66'"],
        )

    def fake_annotate(content, summary, source_name=None):
        captured["summary"] = summary
        return b"%PDF-1.4 fake"

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    monkeypatch.setattr("app.main.annotate_pdf", fake_annotate)
    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        files={"file": ("review.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert captured["summary"].job_totals == ["UG-06 - 13"]
    assert captured["summary"].warnings == ["Unresolved callouts require review."]
    result_summary = json.loads(response.headers["x-telcyte-result-summary"])
    assert result_summary["output_name"] == "review-telcyte-summary.pdf"
    assert result_summary["detected_totals"] == ["UG-06 - 13"]
    assert result_summary["result_lines"] == [
        "MKR Job Totals",
        "UG-06 - 13",
    ]


def test_manual_review_with_supported_totals_preserves_derived_materials(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, SummaryResult] = {}

    async def fake_summarize(content, settings, source_name=None):
        raise ManualReviewRequired(
            ["Unresolved callouts require review."],
            supported_totals=["UG-06 - 13"],
            unresolved_callouts=["EOL - 48Ct - 66'"],
            materials=["470-0349 (CD-02/MDU-11) - 110'"],
        )

    def fake_annotate(content, summary, source_name=None):
        captured["summary"] = summary
        return b"%PDF-1.4 fake"

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    monkeypatch.setattr("app.main.annotate_pdf", fake_annotate)
    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        files={"file": ("review-materials.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 200
    assert captured["summary"].materials == ["470-0349 (CD-02/MDU-11) - 110'"]
    result_summary = json.loads(response.headers["x-telcyte-result-summary"])
    assert result_summary["materials"] == ["470-0349 (CD-02/MDU-11) - 110'"]
    assert result_summary["result_lines"] == [
        "MKR Job Totals",
        "UG-06 - 13",
        "Material",
        "470-0349 (CD-02/MDU-11) - 110'",
    ]


def test_manual_review_without_supported_totals_preserves_material_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_summarize(content, settings, source_name=None):
        raise ManualReviewRequired(
            ["Readable materials but no supported billing totals."],
            supported_totals=[],
            unresolved_callouts=[],
            materials=["450-0323 (UG-28) - 2"],
        )

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        files={"file": ("review-material-only.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["result_summary"]["materials"] == ["450-0323 (UG-28) - 2"]
    assert body["result_summary"]["result_lines"] == [
        "MKR Job Totals",
        "Material",
        "450-0323 (UG-28) - 2",
    ]


def test_manual_review_with_eligible_cable_still_adds_materials(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, SummaryResult] = {}

    async def fake_summarize(content, settings, source_name=None):
        raise ManualReviewRequired(
            ["Review remains for an unrelated reason."],
            supported_totals=["Comp-15 - 1228"],
            unresolved_callouts=[],
            cable_footage=[_cable_line()],
        )

    def fake_annotate(content, summary, source_name=None):
        captured["summary"] = summary
        return b"%PDF-1.4 fake"

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    monkeypatch.setattr("app.main.annotate_pdf", fake_annotate)
    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        files={"file": ("review-cable.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 200
    assert captured["summary"].materials == ["605-3277 (48Ct) - 1700'"]
    result_summary = json.loads(response.headers["x-telcyte-result-summary"])
    assert result_summary["materials"] == ["605-3277 (48Ct) - 1700'"]
    assert result_summary["result_lines"] == [
        "MKR Job Totals",
        "Comp-15 - 1228",
        "Material",
        "605-3277 (48Ct) - 1700'",
    ]


def test_ineligible_cable_line_is_not_promoted_to_materials(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, SummaryResult] = {}

    async def fake_summarize(content, settings, source_name=None):
        raise ManualReviewRequired(
            ["Cable material needs review for .625: Coax source path must be validated before automatic stamping."],
            supported_totals=["Comp-15 - 118"],
            unresolved_callouts=[],
            cable_footage=[
                _cable_line(
                    eligible=False,
                    review_flags=["Coax source path must be validated before automatic stamping."],
                )
            ],
        )

    def fake_annotate(content, summary, source_name=None):
        captured["summary"] = summary
        return b"%PDF-1.4 fake"

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    monkeypatch.setattr("app.main.annotate_pdf", fake_annotate)
    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        files={"file": ("review-cable.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 200
    assert captured["summary"].materials == []
    result_summary = json.loads(response.headers["x-telcyte-result-summary"])
    assert result_summary["materials"] == []
    assert result_summary["cable_footage"][0]["eligible_for_stamp"] is False
    assert result_summary["result_lines"] == ["MKR Job Totals", "Comp-15 - 118"]


def test_cable_header_payload_is_compact_for_many_segments() -> None:
    line = _cable_line()
    long_segments = [
        CableFootageItem(label="Comp-15", page=1, feet=10, source=f"Comp-15 - 10' verbose source line {i} " * 20)
        for i in range(250)
    ]
    summary = SummaryResult(
        model="parser-test",
        confidence=1.0,
        job_totals=["Comp-15 - 2500"],
        cable_footage=[line.model_copy(update={"path_segments": long_segments})],
    ).with_eligible_cable_materials()

    payload = _result_summary_payload(summary, "large-cable.pdf")
    compact = payload["cable_footage"][0]
    header = _result_summary_header(summary, "large-cable.pdf")

    assert compact["path_segment_count"] == 250
    assert "path_segments" not in compact
    assert "storage_items" not in compact
    assert "verbose source line" not in header
    assert len(header) < 4096


def test_result_header_stays_under_conservative_proxy_budget() -> None:
    cables = [
        _cable_line().model_copy(update={"callout": f"cable-{index}"})
        for index in range(6)
    ]
    summary = SummaryResult(
        model="parser+representative-model",
        job_totals=[f"UG-{index:02d} - {index * 10}" for index in range(20)],
        materials=[f"470-{index:04d} - {index}" for index in range(10)],
        new_totals=[f"UG-{index:02d} - {index}" for index in range(8)],
        cable_footage=cables,
        informational_notes=["Short processing note."] * 5,
        issues=[
            SummaryIssue(severity="action", code=f"check_{index}", message=f"Check item {index}.")
            for index in range(8)
        ],
    )

    header = _result_summary_header(summary, "large-result.pdf", run_id="a" * 32)

    # The measured production baseline was 3,344 bytes before run_id and
    # preview_pages. Keep ample room below common proxy header limits.
    assert len(header.encode("utf-8")) < 6144


def test_final_material_evidence_labels_preserved_rows_without_duplicating_computed_rows() -> None:
    summary = SummaryResult(
        model="parser-test",
        materials=["470-0135 (SMC-07) - 4"],
        final_material_rows=["470-0135 (SMC-07) - 4", "600-8403 (FP) - 1"],
        evidence=SummaryEvidence(
            materials=[
                MaterialEvidence(
                    part="470-0135",
                    display="SMC-07",
                    rule="count each",
                    source_quantity="4",
                    source_lines=["SMC-07 - 4"],
                    result="470-0135 (SMC-07) - 4",
                )
            ]
        ),
    )

    finalize_material_evidence(summary)

    assert [item.result for item in summary.evidence.materials] == [
        "470-0135 (SMC-07) - 4",
        "600-8403 (FP) - 1",
    ]
    assert summary.evidence.materials[1].rule == "preserved from existing Materials box"


def test_preview_pages_follow_actual_stamping_mode() -> None:
    page_totals = {3: ["UG-06 - 2"], 4: ["Comp-9 - 3"]}
    regular = SummaryResult(model="parser-test", job_totals=["UG-06 - 2"], page_totals=page_totals)
    delta = SummaryResult(
        model="parser-test",
        job_totals=["Comp-9 - 1160"],
        new_totals=["Comp-9 - 328"],
        page_totals=page_totals,
    )

    _finalize_evidence_for_output(regular, 4)
    _finalize_evidence_for_output(delta, 4)

    assert regular.evidence.preview_pages == [1, 3, 4]
    assert delta.evidence.preview_pages == [1]


def test_preserved_numeric_cable_row_is_explained_when_auto_result_is_verify() -> None:
    summary = SummaryResult(
        model="parser-test",
        materials=["605-3277 (48Ct) - VERIFY"],
        final_material_rows=["605-3277 (48Ct) - 1000'"],
        cable_footage=[
            CableFootageLine(
                callout="48ct",
                display_type="48Ct",
                part_number="605-3277",
                family="fiber",
                path_source="unassigned",
                storage_subtotal=250,
                review_material_line="605-3277 (48Ct) - VERIFY",
            )
        ],
    )

    finalize_material_evidence(summary)

    assert summary.evidence.materials[0].result == "605-3277 (48Ct) - 1000'"
    assert summary.evidence.materials[0].rule == "preserved from existing Materials box"


def test_result_header_omits_run_id_when_history_insert_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_summarize(content, settings, source_name=None):
        return SummaryResult(model="parser+fake", confidence=0.9, job_totals=["UG-56 - 170"])

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    monkeypatch.setattr("app.main.annotate_pdf", lambda content, summary, source_name=None: b"%PDF-1.4 fake")
    monkeypatch.setattr("app.main.run_history_store.log_run", lambda record: None)

    response = TestClient(app).post(
        "/api/summarize",
        files={"file": ("no-history.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 200
    payload = json.loads(response.headers["x-telcyte-result-summary"])
    assert "run_id" not in payload


def test_sample_manual_review_response_includes_supported_evidence() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        files={"file": (SAMPLE.name, SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 200
    result_summary = json.loads(response.headers["x-telcyte-result-summary"])
    assert "UG-56 - 170" in result_summary["detected_totals"]
    # Unresolved callouts are no longer stamped in the box (Review section
    # removed per Nick 2026-06-09); they surface via the warnings header.
    warnings = json.loads(response.headers["x-telcyte-warnings"])
    assert any("EOL - 48Ct - 30'" in w for w in warnings)
    assert not any("EOL - 48Ct - 30'" in line for line in result_summary["result_lines"])
