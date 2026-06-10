"""Run-history PDF storage, search, and download (Nick, 2026-06-09 sync).

These tests use synthetic PDFs and a temp SQLite store - no local samples.
"""
import sqlite3

import fitz
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.models import SummaryResult
from app.run_history import RunHistoryStore


def _pdf(label: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), f"{label} UG-06 - 2")
    data = doc.tobytes()
    doc.close()
    return data


def _store(tmp_path) -> RunHistoryStore:
    return RunHistoryStore(
        database_url=None,
        sqlite_path=str(tmp_path / "runs.sqlite3"),
        savings_minutes_per_completed_pdf=8.0,
        savings_hourly_rate=75.0,
    )


@pytest.fixture()
def client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setattr(main, "run_history_store", _store(tmp_path))

    async def fake_summarize(content, settings, source_name=None):
        return SummaryResult(model="parser+fake", confidence=0.9, job_totals=["UG-06 - 2"])

    monkeypatch.setattr(main, "summarize_with_model", fake_summarize)
    monkeypatch.setattr(main, "annotate_pdf", lambda content, summary, source_name=None: b"%PDF-1.4 fake-output")
    return TestClient(main.app)


def test_input_and_output_pdfs_are_stored_and_downloadable(client) -> None:
    original = _pdf("storage")
    assert client.post("/api/summarize", files={"file": ("job-1.pdf", original, "application/pdf")}).status_code == 200

    run = client.get("/api/run-history").json()["runs"][0]
    assert run["has_input"] and run["has_output"]

    got_input = client.get(f"/api/run-history/{run['id']}/pdf?kind=input")
    assert got_input.status_code == 200
    assert got_input.content == original
    assert "job-1.pdf" in got_input.headers["content-disposition"]

    got_output = client.get(f"/api/run-history/{run['id']}/pdf?kind=output")
    assert got_output.content == b"%PDF-1.4 fake-output"
    assert "job-1-telcyte-summary.pdf" in got_output.headers["content-disposition"]


def test_download_missing_or_bad_kind_is_handled(client) -> None:
    assert client.get("/api/run-history/nope/pdf?kind=output").status_code == 404
    assert client.get("/api/run-history/nope/pdf?kind=sideways").status_code == 400


def test_search_filters_runs(client) -> None:
    client.post("/api/summarize", files={"file": ("BI-829050.pdf", _pdf("a"), "application/pdf")})
    client.post("/api/summarize", files={"file": ("RL-248790.pdf", _pdf("b"), "application/pdf")})

    hits = client.get("/api/run-history?q=829050").json()["runs"]
    assert [r["source_filename"] for r in hits] == ["BI-829050.pdf"]
    assert client.get("/api/run-history?q=zzz-no-match").json()["runs"] == []
    assert len(client.get("/api/run-history").json()["runs"]) == 2


def test_sqlite_schema_migration_adds_blob_columns(tmp_path) -> None:
    # Simulate a pre-upgrade database created without the blob columns.
    db = tmp_path / "old.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "create table asbuilt_run_history ("
            " id text primary key, created_at text not null,"
            " source_filename text not null, output_filename text not null default '',"
            " status text not null, duration_ms integer not null default 0,"
            " pages_processed integer, model text not null default '',"
            " confidence real, detected_totals_count integer not null default 0,"
            " extra_billing_codes_count integer not null default 0,"
            " selected_extras_json text not null default '[]',"
            " warnings_count integer not null default 0,"
            " error_type text not null default '', error_message text not null default '',"
            " estimated_minutes_saved real not null default 0,"
            " estimated_dollars_saved real not null default 0)"
        )
    store = RunHistoryStore(
        database_url=None,
        sqlite_path=str(db),
        savings_minutes_per_completed_pdf=8.0,
        savings_hourly_rate=75.0,
    )
    data = store.dashboard()  # triggers _ensure_schema migration
    assert data["summary"]["total_runs"] == 0
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(asbuilt_run_history)")}
    assert {"input_pdf", "output_pdf"} <= columns
