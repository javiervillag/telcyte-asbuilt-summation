import logging
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.extra_billing_codes import (
    extra_totals_from_selections,
    grouped_extra_billing_codes,
    parse_extra_billing_code_selections,
)
from app.models import SummaryResult
from app.openrouter_client import OpenRouterError, summarize_with_model
from app.pdf_annotator import annotate_pdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Telcyte As-Built Summation", version="1.0.0")
settings = get_settings()
static_dir = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "openrouter_configured": bool(settings.openrouter_api_key),
        "model": settings.openrouter_model,
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


@app.post("/api/summarize")
async def summarize_pdf(file: UploadFile = File(...), extra_billing_codes: str = Form(default="[]")) -> Response:
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Upload a PDF file.")
    try:
        selected_extras = parse_extra_billing_code_selections(extra_billing_codes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded PDF is empty.")
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="The uploaded PDF is too large.")

    logger.info("upload_received filename=%s bytes=%s", file.filename, len(content))
    try:
        summary = await summarize_with_model(content, settings)
        summary = _with_user_selected_extras(summary, selected_extras)
        output = annotate_pdf(content, summary)
    except OpenRouterError as exc:
        logger.warning("summary_failed filename=%s error=%s", file.filename, exc)
        return JSONResponse(status_code=502, content={"detail": str(exc)})
    except Exception as exc:  # noqa: BLE001 - converted to safe user error
        logger.exception("processing_failed filename=%s", file.filename)
        return JSONResponse(status_code=500, content={"detail": "The PDF could not be processed."})

    base = Path(file.filename).stem
    output_name = f"{base}-telcyte-summary.pdf"
    logger.info(
        "pdf_complete filename=%s output_bytes=%s model=%s confidence=%.2f",
        file.filename,
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
        },
    )


def _with_user_selected_extras(summary: SummaryResult, selections: list) -> SummaryResult:
    if not selections:
        return summary
    extra_totals, extra_notes = extra_totals_from_selections(selections)
    return summary.model_copy(
        update={
            "extra_totals": [*summary.extra_totals, *extra_totals],
            "extra_notes": [*summary.extra_notes, *extra_notes],
        }
    )
