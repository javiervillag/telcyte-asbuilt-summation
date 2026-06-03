import json
import logging
from pathlib import Path

import fitz
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.openrouter_client import ManualReviewRequired, OpenRouterError, summarize_with_model
from app.pdf_annotator import PlacementReviewRequired, annotate_pdf, describe_pdf_fonts

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
        "pdf_fonts": describe_pdf_fonts(),
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (static_dir / "index.html").read_text()


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.post("/api/summarize")
async def summarize_pdf(file: UploadFile = File(...)) -> Response:
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Upload a PDF file.")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded PDF is empty.")
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="The uploaded PDF is too large.")
    _validate_pdf_upload(content)

    logger.info("upload_received filename=%s bytes=%s", file.filename, len(content))
    try:
        summary = await summarize_with_model(content, settings, source_name=file.filename)
        output = annotate_pdf(content, summary, source_name=file.filename)
    except ManualReviewRequired as exc:
        logger.warning("manual_review_required filename=%s warnings=%s", file.filename, exc.warnings)
        return JSONResponse(
            status_code=422,
            content={
                "detail": "This PDF needs manual review because the parsed evidence did not fully support automatic totals.",
                "warnings": exc.warnings,
                "supported_totals": exc.supported_totals,
                "unresolved_callouts": exc.unresolved_callouts,
                "unresolved_callout_summary": exc.diagnostics.get("unresolved_callout_summary") or [],
                "verifier_model": exc.verifier_model,
                "verifier_used": exc.verifier_used,
                "diagnostics": exc.diagnostics,
            },
        )
    except PlacementReviewRequired as exc:
        logger.warning("placement_review_required filename=%s error=%s", file.filename, exc)
        return JSONResponse(
            status_code=422,
            content={
                "detail": "This PDF needs manual review because there is no clear open area for the summary box.",
                "warnings": ["The app could not place the summary box without risking existing annotations."],
            },
        )
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
            "X-Telcyte-Warnings": json.dumps(summary.warnings[:6]),
        },
    )


def _validate_pdf_upload(content: bytes) -> None:
    try:
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - converted to safe user error
        raise HTTPException(status_code=400, detail="The uploaded file is not a valid PDF.") from exc
    try:
        if doc.is_encrypted or doc.needs_pass:
            raise HTTPException(status_code=422, detail="Password-protected PDFs need manual review.")
        if doc.page_count < 1:
            raise HTTPException(status_code=400, detail="The uploaded PDF has no pages.")
    finally:
        doc.close()
