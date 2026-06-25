import json
import logging
import time
from pathlib import Path
from typing import Optional

import fitz
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.extra_billing_codes import (
    ExtraBillingCodeSelection,
    extra_totals_from_selections,
    grouped_extra_billing_codes,
    parse_extra_billing_code_selections,
)
from app.models import SummaryResult
from app.openrouter_client import ManualReviewRequired, OpenRouterError, summarize_with_model
from app.pdf_annotator import PlacementReviewRequired, annotate_pdf, describe_pdf_fonts
from app.run_history import RunHistoryStore, RunLogRecord

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Telcyte As-Built Summation", version="1.0.0")
settings = get_settings()
static_dir = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")
run_history_store = RunHistoryStore(
    database_url=settings.run_log_url,
    sqlite_path=settings.run_log_sqlite_path,
    savings_minutes_per_completed_pdf=settings.savings_minutes_per_completed_pdf,
    savings_hourly_rate=settings.savings_hourly_rate,
)


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "openrouter_configured": bool(settings.openrouter_api_key),
        "model": settings.openrouter_model,
        "run_history": {
            "configured": True,
            "backend": "postgres" if settings.run_log_url else "sqlite",
        },
        "pdf_fonts": describe_pdf_fonts(),
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (static_dir / "index.html").read_text()


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/api/extra-billing-codes")
async def extra_billing_codes() -> dict:
    return {"categories": grouped_extra_billing_codes()}


@app.get("/api/run-history")
async def run_history(limit: int = 20, q: str = "") -> dict:
    return run_history_store.dashboard(limit=limit, query=q)


@app.get("/api/run-history/{run_id}/pdf")
async def run_history_pdf(run_id: str, kind: str = "output") -> Response:
    if kind not in {"input", "output"}:
        raise HTTPException(status_code=400, detail="kind must be 'input' or 'output'.")
    stored = run_history_store.get_pdf(run_id, kind)
    if not stored:
        raise HTTPException(status_code=404, detail="No stored PDF for this run.")
    name, data = stored
    safe_name = name.replace('"', "")
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@app.get("/api/run-history.csv")
async def run_history_csv(limit: int = 500) -> Response:
    csv_text = run_history_store.csv_export(limit=limit)
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="telcyte-run-history.csv"'},
    )


@app.post("/api/summarize")
async def summarize_pdf(file: UploadFile = File(...), extra_billing_codes: str = Form(default="[]")) -> Response:
    started_at = time.perf_counter()
    source_filename = file.filename or "unknown"
    selected_extras: list[ExtraBillingCodeSelection] = []
    page_count: Optional[int] = None
    if not source_filename.lower().endswith(".pdf"):
        _log_run_attempt(
            source_filename=source_filename,
            status="failed",
            started_at=started_at,
            selected_extras=selected_extras,
            error_type="invalid_upload",
            error_message="Upload a PDF file.",
        )
        raise HTTPException(status_code=400, detail="Upload a PDF file.")
    try:
        selected_extras = parse_extra_billing_code_selections(extra_billing_codes)
    except ValueError as exc:
        _log_run_attempt(
            source_filename=source_filename,
            status="failed",
            started_at=started_at,
            selected_extras=selected_extras,
            error_type="extra_code_error",
            error_message=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    content = await file.read()
    if not content:
        _log_run_attempt(
            source_filename=source_filename,
            status="failed",
            started_at=started_at,
            selected_extras=selected_extras,
            error_type="empty_upload",
            error_message="The uploaded PDF is empty.",
        )
        raise HTTPException(status_code=400, detail="The uploaded PDF is empty.")
    if len(content) > settings.max_upload_bytes:
        _log_run_attempt(
            source_filename=source_filename,
            status="failed",
            started_at=started_at,
            selected_extras=selected_extras,
            error_type="upload_too_large",
            error_message="The uploaded PDF is too large.",
        )
        raise HTTPException(status_code=413, detail="The uploaded PDF is too large.")
    try:
        page_count = _validate_pdf_upload(content)
    except HTTPException as exc:
        _log_run_attempt(
            source_filename=source_filename,
            status="failed",
            started_at=started_at,
            selected_extras=selected_extras,
            error_type="invalid_pdf",
            error_message=str(exc.detail),
            input_pdf=content,
        )
        raise

    logger.info("upload_received filename=%s bytes=%s", source_filename, len(content))
    try:
        summary = await summarize_with_model(content, settings, source_name=source_filename)
        summary = _with_user_selected_extras(summary, selected_extras)
        summary = _finalize_summary_for_output(summary)
        output = annotate_pdf(content, summary, source_name=source_filename)
    except ManualReviewRequired as exc:
        logger.warning("manual_review_required filename=%s warnings=%s", source_filename, exc.warnings)
        # NOTE: per-page "MKR Page Totals" boxes are intentionally NOT stamped on the
        # manual-review path. These runs are flagged as uncertain, so we stamp only
        # the page-1 Job Totals (from parser-supported totals) and leave per-page
        # boxes to a confirmed re-run rather than auto-placing totals a human still
        # needs to verify.
        if selected_extras:
            summary = _finalize_summary_for_output(_with_user_selected_extras(
                SummaryResult(
                    title="MKR Job Totals",
                    job_totals=exc.supported_totals,
                    cable_footage=exc.cable_footage,
                    warnings=exc.warnings,
                    informational_notes=exc.informational_notes,
                    confidence=0.0,
                    model=f"parser+{settings.openrouter_model}",
                ),
                selected_extras,
            ))
            try:
                output = annotate_pdf(content, summary, source_name=source_filename)
            except PlacementReviewRequired as placement_exc:
                logger.warning("placement_review_required filename=%s error=%s", source_filename, placement_exc)
                _log_run_attempt(
                    source_filename=source_filename,
                    status="manual_review",
                    started_at=started_at,
                    selected_extras=selected_extras,
                    pages_processed=page_count,
                    input_pdf=content,
                    warnings_count=1,
                    error_type="placement_review",
                    error_message="No clear open area for the summary box.",
                )
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": "This PDF needs manual review because there is no clear open area for the summary box.",
                        "warnings": ["The app could not place the summary box without risking existing annotations."],
                    },
                )
        else:
            if not exc.supported_totals:
                review_summary = _finalize_summary_for_output(SummaryResult(
                    title="MKR Job Totals",
                    job_totals=[],
                    cable_footage=exc.cable_footage,
                    warnings=exc.warnings,
                    informational_notes=exc.informational_notes,
                    confidence=0.0,
                    model=f"parser+{settings.openrouter_model}",
                ))
                _log_run_attempt(
                    source_filename=source_filename,
                    status="manual_review",
                    started_at=started_at,
                    selected_extras=selected_extras,
                    pages_processed=page_count,
                    input_pdf=content,
                    summary=review_summary,
                    warnings_count=len(exc.warnings),
                    error_type="manual_review",
                    error_message="Parsed evidence did not fully support automatic totals.",
                )
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": "This PDF needs manual review because the parsed evidence did not fully support automatic totals.",
                        "warnings": exc.warnings,
                        "supported_totals": exc.supported_totals,
                        "unresolved_callouts": exc.unresolved_callouts,
                        "result_summary": _result_summary_payload(review_summary, None),
                    },
                )
            summary = _finalize_summary_for_output(SummaryResult(
                title="MKR Job Totals",
                job_totals=exc.supported_totals,
                cable_footage=exc.cable_footage,
                warnings=exc.warnings,
                informational_notes=exc.informational_notes,
                confidence=0.0,
                model=f"parser+{settings.openrouter_model}",
            ))
            try:
                output = annotate_pdf(content, summary, source_name=source_filename)
            except PlacementReviewRequired as placement_exc:
                logger.warning("placement_review_required filename=%s error=%s", source_filename, placement_exc)
                _log_run_attempt(
                    source_filename=source_filename,
                    status="manual_review",
                    started_at=started_at,
                    selected_extras=selected_extras,
                    pages_processed=page_count,
                    input_pdf=content,
                    summary=summary,
                    warnings_count=len(summary.warnings),
                    error_type="placement_review",
                    error_message="No clear open area for the summary box.",
                )
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": "This PDF needs manual review because there is no clear open area for the summary box.",
                        "warnings": ["The app could not place the summary box without risking existing annotations."],
                    },
                )
    except PlacementReviewRequired as exc:
        logger.warning("placement_review_required filename=%s error=%s", source_filename, exc)
        _log_run_attempt(
            source_filename=source_filename,
            status="manual_review",
            started_at=started_at,
            selected_extras=selected_extras,
            pages_processed=page_count,
            input_pdf=content,
            warnings_count=1,
            error_type="placement_review",
            error_message="No clear open area for the summary box.",
        )
        return JSONResponse(
            status_code=422,
            content={
                "detail": "This PDF needs manual review because there is no clear open area for the summary box.",
                "warnings": ["The app could not place the summary box without risking existing annotations."],
            },
        )
    except OpenRouterError as exc:
        logger.warning("summary_failed filename=%s error=%s", source_filename, exc)
        _log_run_attempt(
            source_filename=source_filename,
            status="failed",
            started_at=started_at,
            selected_extras=selected_extras,
            pages_processed=page_count,
            input_pdf=content,
            error_type="model_error",
            error_message=str(exc),
        )
        return JSONResponse(status_code=502, content={"detail": str(exc)})
    except Exception as exc:  # noqa: BLE001 - converted to safe user error
        logger.exception("processing_failed filename=%s", source_filename)
        _log_run_attempt(
            source_filename=source_filename,
            status="failed",
            started_at=started_at,
            selected_extras=selected_extras,
            pages_processed=page_count,
            input_pdf=content,
            error_type="processing_error",
            error_message="The PDF could not be processed.",
        )
        return JSONResponse(status_code=500, content={"detail": "The PDF could not be processed."})

    base = Path(source_filename).stem
    output_name = f"{base}-telcyte-summary.pdf"
    status = _status_for_summary(summary)
    _log_run_attempt(
        source_filename=source_filename,
        status=status,
        started_at=started_at,
        selected_extras=selected_extras,
        pages_processed=page_count,
        input_pdf=content,
        output_pdf=output,
        output_filename=output_name,
        summary=summary,
        warnings_count=len(summary.warnings),
    )
    logger.info(
        "pdf_complete filename=%s output_bytes=%s model=%s confidence=%.2f",
        source_filename,
        len(output),
        summary.model,
        summary.confidence,
    )
    return Response(
        content=output,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{output_name}"',
            "X-Telcyte-Model": summary.model,
            "X-Telcyte-Confidence": f"{summary.confidence:.2f}",
            "X-Telcyte-Status": status,
            "X-Telcyte-Warnings": json.dumps(summary.warnings[:6]),
            "X-Telcyte-Result-Summary": _result_summary_header(summary, output_name),
        },
    )


def _with_user_selected_extras(
    summary: SummaryResult,
    selections: list[ExtraBillingCodeSelection],
) -> SummaryResult:
    if not selections:
        return summary
    extra_totals, extra_notes = extra_totals_from_selections(selections)
    return summary.model_copy(
        update={
            "extra_totals": [*summary.extra_totals, *extra_totals],
            "extra_notes": [*summary.extra_notes, *extra_notes],
        }
    )


def _finalize_summary_for_output(summary: SummaryResult) -> SummaryResult:
    return summary.with_eligible_cable_materials()


def _validate_pdf_upload(content: bytes) -> int:
    try:
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - converted to safe user error
        raise HTTPException(status_code=400, detail="The uploaded file is not a valid PDF.") from exc
    try:
        if doc.is_encrypted or doc.needs_pass:
            raise HTTPException(status_code=422, detail="Password-protected PDFs need manual review.")
        if doc.page_count < 1:
            raise HTTPException(status_code=400, detail="The uploaded PDF has no pages.")
        return doc.page_count
    finally:
        doc.close()


def _log_run_attempt(
    *,
    source_filename: str,
    status: str,
    started_at: float,
    selected_extras: list[ExtraBillingCodeSelection],
    pages_processed: Optional[int] = None,
    output_filename: str = "",
    summary: Optional[SummaryResult] = None,
    warnings_count: int = 0,
    error_type: str = "",
    error_message: str = "",
    input_pdf: Optional[bytes] = None,
    output_pdf: Optional[bytes] = None,
) -> None:
    minutes_saved, dollars_saved = run_history_store.estimate_savings(status, bool(output_filename))
    record = RunLogRecord(
        source_filename=source_filename,
        output_filename=output_filename,
        status=status,
        duration_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
        pages_processed=pages_processed,
        model=summary.model if summary else "",
        confidence=summary.confidence if summary else None,
        detected_totals_count=len(summary.job_totals) if summary else 0,
        extra_billing_codes_count=len(summary.extra_totals) if summary else 0,
        selected_extras=[
            {"code": item.code, "quantity": item.quantity, "note": item.note}
            for item in selected_extras
        ],
        warnings_count=warnings_count,
        error_type=error_type,
        error_message=error_message[:240],
        estimated_minutes_saved=minutes_saved,
        estimated_dollars_saved=dollars_saved,
        input_pdf=input_pdf,
        output_pdf=output_pdf,
        result_lines=summary.display_lines() if summary else [],
        cable_footage=[
            line.model_dump()
            for line in summary.cable_footage
        ] if summary else [],
    )
    try:
        run_history_store.log_run(record)
    except Exception as exc:  # noqa: BLE001 - logging must never block PDF generation
        logger.warning("run_history_unexpected_failure error=%s", exc)


def _result_summary_header(summary: SummaryResult, output_name: str) -> str:
    return json.dumps(_result_summary_payload(summary, output_name), ensure_ascii=True, separators=(",", ":"))


def _result_summary_payload(summary: SummaryResult, output_name: Optional[str]) -> dict:
    payload = {
        "output_name": output_name or "",
        "detected_totals": summary.job_totals[:20],
        "extra_billing_codes": summary.extra_totals[:20],
        "materials": summary.materials[:10],
        "cable_footage": [
            _compact_cable_footage(line)
            for line in summary.cable_footage[:10]
        ],
        "notes": summary.informational_notes[:10],
        "result_lines": _result_detail_lines(summary),
    }
    return payload


def _compact_cable_footage(line) -> dict:
    path_pages = sorted({item.page for item in line.path_segments if item.page})
    storage_pages = sorted({item.page for item in line.storage_items if item.page})
    return {
        "callout": line.callout,
        "display_type": line.display_type,
        "part_number": line.part_number,
        "family": line.family,
        "path_subtotal": line.path_subtotal,
        "storage_subtotal": line.storage_subtotal,
        "buffer": line.buffer,
        "rounding": line.rounding,
        "total_ft": line.total_ft,
        "material_line": line.material_line,
        "eligible_for_stamp": line.eligible_for_stamp,
        "source_pages": line.source_pages,
        "path_segment_count": len(line.path_segments),
        "storage_item_count": len(line.storage_items),
        "path_pages": path_pages,
        "storage_pages": storage_pages,
        "confidence": line.confidence,
        "review_flags": line.review_flags[:10],
        "notes": line.notes[:10],
    }


def _status_for_summary(summary: SummaryResult) -> str:
    if settings.strict_review_badges and (summary.warnings or summary.informational_notes):
        return "manual_review"
    if summary.warnings:
        return "manual_review"
    if summary.informational_notes:
        return "done_with_notes"
    return "success"


def _result_detail_lines(summary: SummaryResult) -> list[str]:
    return summary.display_lines()[:80]
