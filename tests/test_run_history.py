import json
from pathlib import Path

import fitz
from fastapi.testclient import TestClient

import app.main as main
from app.models import CableFootageLine, SummaryResult
from app.openrouter_client import ManualReviewRequired
from app.run_history import RunHistoryStore


def _temp_store(tmp_path) -> RunHistoryStore:
    return RunHistoryStore(
        database_url=None,
        sqlite_path=str(tmp_path / "runs.sqlite3"),
        savings_minutes_per_completed_pdf=8.0,
        savings_hourly_rate=75.0,
    )


def _sample_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "UG-56 - 170'")
    content = doc.tobytes()
    doc.close()
    return content


def test_successful_pdf_run_is_logged(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main, "run_history_store", _temp_store(tmp_path))

    async def fake_summarize(content, settings, source_name=None):
        return SummaryResult(
            model="parser+fake-model",
            confidence=0.91,
            job_totals=["UG-56 - 170'"],
            materials=[],
        )

    monkeypatch.setattr(main, "summarize_with_model", fake_summarize)
    monkeypatch.setattr(main, "annotate_pdf", lambda content, summary, source_name=None: b"%PDF-1.4 fake")

    client = TestClient(main.app)
    response = client.post(
        "/api/summarize",
        files={"file": ("success.pdf", _sample_pdf(), "application/pdf")},
    )

    assert response.status_code == 200
    data = client.get("/api/run-history").json()
    assert data["summary"]["completed_runs"] == 1
    assert data["summary"]["done_runs"] == 1
    assert data["summary"]["done_with_notes_runs"] == 0
    assert data["summary"]["failed_runs"] == 0
    # Minutes are shown (Nick confirmed ~8 min/as-built on 2026-06-08);
    # dollar figures stay hidden until the hourly rate is confirmed.
    assert data["summary"]["estimated_minutes_saved"] == 8.0
    assert "estimated_dollars_saved" not in data["summary"]
    run = data["runs"][0]
    assert run["status"] == "success"
    assert run["status_label"] == "Done"


def test_cable_footage_round_trips_to_run_history(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main, "run_history_store", _temp_store(tmp_path))

    async def fake_summarize(content, settings, source_name=None):
        return SummaryResult(
            model="parser+fake-model",
            confidence=0.91,
            job_totals=["UG-56 - 170'"],
            cable_footage=[
                CableFootageLine(
                    callout="48ct",
                    display_type="48Ct",
                    part_number="605-3277",
                    family="fiber",
                    path_subtotal=1772,
                    storage_subtotal=730,
                    total_ft=2800,
                    material_line="605-3277 (48Ct) - 2800'",
                )
            ],
        )

    monkeypatch.setattr(main, "summarize_with_model", fake_summarize)
    monkeypatch.setattr(main, "annotate_pdf", lambda content, summary, source_name=None: b"%PDF-1.4 fake")

    client = TestClient(main.app)
    response = client.post(
        "/api/summarize",
        files={"file": ("fiber.pdf", _sample_pdf(), "application/pdf")},
    )

    assert response.status_code == 200
    payload = json.loads(response.headers["x-telcyte-result-summary"])
    assert payload["cable_footage"][0]["material_line"] == "605-3277 (48Ct) - 2800'"
    run = client.get("/api/run-history").json()["runs"][0]
    assert run["cable_footage"][0]["material_line"] == "605-3277 (48Ct) - 2800'"
    assert run["source_filename"] == "fiber.pdf"
    assert run["output_filename"] == "fiber-telcyte-summary.pdf"
    assert run["detected_totals_count"] == 1
    assert run["pages_processed"] >= 1
    assert run["estimated_minutes_saved"] == 8.0
    assert "estimated_dollars_saved" not in run
    assert run["has_input"] is True
    assert run["has_output"] is True


def test_failed_pdf_run_is_logged(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main, "run_history_store", _temp_store(tmp_path))

    client = TestClient(main.app)
    response = client.post(
        "/api/summarize",
        files={"file": ("bad.pdf", b"not a real pdf", "application/pdf")},
    )

    assert response.status_code == 400
    data = client.get("/api/run-history").json()
    assert data["summary"]["completed_runs"] == 0
    assert data["summary"]["done_runs"] == 0
    assert data["summary"]["done_with_notes_runs"] == 0
    assert data["summary"]["failed_runs"] == 1
    run = data["runs"][0]
    assert run["status"] == "failed"
    assert run["error_type"] == "invalid_pdf"
    assert "valid PDF" in run["error_message"]


def test_manual_review_pdf_run_is_logged(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main, "run_history_store", _temp_store(tmp_path))

    async def fake_summarize(content, settings, source_name=None):
        raise ManualReviewRequired(
            ["Unresolved callouts require review."],
            supported_totals=["UG-06 - 13"],
            unresolved_callouts=["EOL - 48Ct - 66'"],
        )

    monkeypatch.setattr(main, "summarize_with_model", fake_summarize)
    monkeypatch.setattr(main, "annotate_pdf", lambda content, summary, source_name=None: b"%PDF-1.4 fake")

    client = TestClient(main.app)
    response = client.post(
        "/api/summarize",
        files={"file": ("review.pdf", _sample_pdf(), "application/pdf")},
    )

    assert response.status_code == 200
    data = client.get("/api/run-history").json()
    assert data["summary"]["completed_runs"] == 1
    assert data["summary"]["done_runs"] == 0
    assert data["summary"]["done_with_notes_runs"] == 0
    assert data["summary"]["review_needed_runs"] == 1
    run = data["runs"][0]
    assert run["status"] == "manual_review"
    assert run["warnings_count"] == 1
    assert run["detected_totals_count"] == 1


def test_done_with_notes_pdf_run_is_logged_as_green_completed(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main, "run_history_store", _temp_store(tmp_path))
    monkeypatch.setattr(main.settings, "strict_review_badges", False)

    async def fake_summarize(content, settings, source_name=None):
        return SummaryResult(
            model="parser+fake-model",
            confidence=0.91,
            job_totals=["UG-56 - 170"],
            informational_notes=["No readable PDF text-box annotations were found; totals came from readable page text."],
        )

    monkeypatch.setattr(main, "summarize_with_model", fake_summarize)
    monkeypatch.setattr(main, "annotate_pdf", lambda content, summary, source_name=None: b"%PDF-1.4 fake")

    client = TestClient(main.app)
    response = client.post(
        "/api/summarize",
        files={"file": ("noted.pdf", _sample_pdf(), "application/pdf")},
    )

    assert response.status_code == 200
    assert response.headers["x-telcyte-status"] == "done_with_notes"
    assert response.headers["x-telcyte-warnings"] == "[]"
    result_summary = json.loads(response.headers["x-telcyte-result-summary"])
    assert result_summary["notes"] == [
        "No readable PDF text-box annotations were found; totals came from readable page text."
    ]

    data = client.get("/api/run-history").json()
    assert data["summary"]["completed_runs"] == 1
    assert data["summary"]["done_runs"] == 0
    assert data["summary"]["done_with_notes_runs"] == 1
    assert data["summary"]["review_needed_runs"] == 0
    assert data["summary"]["estimated_minutes_saved"] == 8.0
    run = data["runs"][0]
    assert run["status"] == "done_with_notes"
    assert run["status_label"] == "Done - Notes"
    assert run["warnings_count"] == 0


def test_strict_review_badges_turn_notes_back_to_review(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main, "run_history_store", _temp_store(tmp_path))
    monkeypatch.setattr(main.settings, "strict_review_badges", True)

    async def fake_summarize(content, settings, source_name=None):
        return SummaryResult(
            model="parser+fake-model",
            confidence=0.91,
            job_totals=["UG-56 - 170"],
            informational_notes=["Handled note."],
        )

    monkeypatch.setattr(main, "summarize_with_model", fake_summarize)
    monkeypatch.setattr(main, "annotate_pdf", lambda content, summary, source_name=None: b"%PDF-1.4 fake")

    client = TestClient(main.app)
    response = client.post(
        "/api/summarize",
        files={"file": ("strict.pdf", _sample_pdf(), "application/pdf")},
    )

    assert response.status_code == 200
    assert response.headers["x-telcyte-status"] == "manual_review"
    data = client.get("/api/run-history").json()
    assert data["summary"]["review_needed_runs"] == 1
    assert data["runs"][0]["status"] == "manual_review"


def test_logging_failure_does_not_block_pdf_generation(monkeypatch) -> None:
    class BrokenStore:
        def estimate_savings(self, status, has_output):
            return 20.0, 25.0

        def log_run(self, record):
            raise RuntimeError("database unavailable")

    async def fake_summarize(content, settings, source_name=None):
        return SummaryResult(
            model="parser+fake-model",
            confidence=0.91,
            job_totals=["UG-56 - 170'"],
            materials=[],
        )

    monkeypatch.setattr(main, "run_history_store", BrokenStore())
    monkeypatch.setattr(main, "summarize_with_model", fake_summarize)
    monkeypatch.setattr(main, "annotate_pdf", lambda content, summary, source_name=None: b"%PDF-1.4 fake")

    client = TestClient(main.app)
    response = client.post(
        "/api/summarize",
        files={"file": ("success.pdf", _sample_pdf(), "application/pdf")},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


def test_run_history_csv_export(monkeypatch, tmp_path) -> None:
    store = _temp_store(tmp_path)
    monkeypatch.setattr(main, "run_history_store", store)

    async def fake_summarize(content, settings, source_name=None):
        return SummaryResult(
            model="parser+fake-model",
            confidence=0.91,
            job_totals=["UG-56 - 170'"],
            materials=[],
        )

    monkeypatch.setattr(main, "summarize_with_model", fake_summarize)
    monkeypatch.setattr(main, "annotate_pdf", lambda content, summary, source_name=None: b"%PDF-1.4 fake")

    client = TestClient(main.app)
    extras = [{"code": "PC-02", "quantity": "1", "note": "Confirmed by Nick."}]
    client.post(
        "/api/summarize",
        data={"extra_billing_codes": json.dumps(extras)},
        files={"file": ("csv.pdf", _sample_pdf(), "application/pdf")},
    )
    response = client.get("/api/run-history.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "source_filename,status,output_filename" not in response.text
    assert "csv.pdf" in response.text
    assert "csv-telcyte-summary.pdf" in response.text
    assert "PC-02" not in response.text
    assert "estimated_minutes_saved" in response.text
    assert "status_label" in response.text
    assert "estimated_dollars_saved" not in response.text


def test_history_gui_knows_done_with_notes_state() -> None:
    app_js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
    styles = (Path(__file__).resolve().parents[1] / "static" / "styles.css").read_text()

    assert 'status === "done_with_notes"' in app_js
    assert 'kind === "note"' in app_js
    assert ".run-status.done_with_notes" in styles
    assert ".status.note .status-badge" in styles
