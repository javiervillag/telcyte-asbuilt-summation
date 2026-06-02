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


def test_summarize_endpoint_reports_manual_review(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_summarize(content, settings, source_name=None):
        raise ManualReviewRequired(["This PDF does not have enough readable text for automatic summation."])

    monkeypatch.setattr("app.main.summarize_with_model", fake_summarize)
    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        files={"file": ("blank.pdf", SAMPLE.read_bytes(), "application/pdf")},
    )
    assert response.status_code == 422
    assert "manual review" in response.json()["detail"].lower()
    assert response.json()["warnings"]
