import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import SummaryResult
from app.openrouter_client import ManualReviewRequired


SAMPLE = Path("/Users/javiervillaguardado/Downloads/Asbuilt Examples for AI Summation/FIBER-ASBUILT-(TelCyte)-BI-829050-Totals Removed.pdf")


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


def test_summarize_endpoint_rejects_unknown_extra_code(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_summarize(content, settings, source_name=None):
        raise AssertionError("summarize should not run for invalid extra codes")

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)

    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        data={"extra_billing_codes": json.dumps([{"code": "ZZ-99", "quantity": "1"}])},
        files={"file": ("sample.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )

    assert response.status_code == 400
    assert "not available" in response.json()["detail"]


def test_summarize_endpoint_reports_manual_review(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_summarize(content, settings, source_name=None):
        raise ManualReviewRequired(
            ["This PDF does not have enough readable text for automatic summation."],
            supported_totals=["UG-06 - 13"],
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
    assert body["supported_totals"] == ["UG-06 - 13"]
    assert body["unresolved_callouts"] == ["EOL - 48Ct - 66'"]


def test_sample_manual_review_response_includes_supported_evidence() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        files={"file": (SAMPLE.name, SAMPLE.read_bytes(), "application/pdf")},
    )

    body = response.json()
    assert response.status_code == 422
    assert "UG-56 - 170'" in body["supported_totals"]
    assert "EOL - 48Ct - 30'" in body["unresolved_callouts"]
