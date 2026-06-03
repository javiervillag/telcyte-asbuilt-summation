from pathlib import Path
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import SummaryResult
from app.openrouter_client import ManualReviewRequired


SAMPLE = Path("/Users/javiervillaguardado/Downloads/Asbuilt Examples for AI Summation/FIBER-ASBUILT-(TelCyte)-BI-829050-Totals Removed.pdf")


class _FakeOpenRouterResponse:
    status_code = 200
    text = "ok"

    def json(self) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "title": "MKR Job Totals",
                                "job_totals": [],
                                "materials": [],
                                "warnings": ["Model verifier kept construction callouts in manual review."],
                                "confidence": 0.5,
                                "remaining_unresolved_callouts": ["EOL - 48Ct - 30'"],
                                "resolved_callouts": [],
                            }
                        ),
                    }
                }
            ]
        }


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers, json):
        return _FakeOpenRouterResponse()


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
        raise ManualReviewRequired(
            ["This PDF does not have enough readable text for automatic summation."],
            supported_totals=["UG-06 - 13"],
            unresolved_callouts=["EOL - 48Ct - 66'"],
            diagnostics={
                "block_count": 3,
                "text_chars": 80,
                "quantity_line_count": 1,
                "code_total_count": 1,
                "unresolved_callout_count": 1,
                "review_required": True,
            },
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
    assert body["diagnostics"] == {
        "block_count": 3,
        "text_chars": 80,
        "quantity_line_count": 1,
        "code_total_count": 1,
        "unresolved_callout_count": 1,
        "review_required": True,
    }


def test_sample_manual_review_response_includes_supported_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.openrouter_client.httpx.AsyncClient", _FakeAsyncClient)
    client = TestClient(app)
    response = client.post(
        "/api/summarize",
        files={"file": (SAMPLE.name, SAMPLE.read_bytes(), "application/pdf")},
    )

    body = response.json()
    assert response.status_code == 422
    assert "UG-56 - 170'" in body["supported_totals"]
    assert "EOL - 48Ct - 30'" in body["unresolved_callouts"]
    assert body["diagnostics"]["review_required"] is True
    assert body["diagnostics"]["code_total_count"] == len(body["supported_totals"])
    assert body["diagnostics"]["unresolved_callout_count"] >= 1
