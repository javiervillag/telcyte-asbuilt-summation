from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from io import BytesIO

import fitz
import httpx
from PIL import Image

from app.config import Settings
from app.models import SummaryResult
from app.pdf_parser import build_pdf_context

logger = logging.getLogger(__name__)


class OpenRouterError(RuntimeError):
    pass


@dataclass
class ModelAttempt:
    model: str
    ok: bool
    summary: SummaryResult | None = None
    error: str | None = None


SYSTEM_PROMPT = """You are helping Telcyte review parsed as-built construction drawings.
Use the parsed PDF text layer, positioned blocks, and deterministic aggregate candidates to produce the MKR Job Totals and Materials box.
Return only JSON. Do not invent unreadable or uncertain details.
Use concise construction quantity lines like "UG-56 - 168'" or "450-0307 PV - 2".
If a value is unclear, omit it or put a short warning in warnings."""


USER_PROMPT = """Analyze this parsed as-built PDF context and produce the green-box contents.
Expected JSON shape:
{
  "title": "MKR Job Totals",
  "job_totals": ["CODE - quantity", "..."],
  "materials": ["material - quantity", "..."],
  "warnings": ["short warning if needed"],
  "confidence": 0.0
}
Prefer the deterministic code totals when they are supported by the positioned text blocks.
Use the material candidates for the Materials section, but keep only true material lines.
Focus on totals and materials visible or inferable from drawing labels, callouts, notes, and quantity markings.
Never add a detail unless the parsed PDF context supports it.

Parsed context:
{context}"""


def render_pdf_images(pdf_bytes: bytes, max_pages: int = 2) -> list[str]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images: list[str] = []
    try:
        for page in doc[:max_pages]:
            zoom = min(2.0, max(0.9, 2200 / max(page.rect.width, page.rect.height)))
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            buffer = BytesIO()
            img.save(buffer, format="PNG", optimize=True)
            images.append(base64.b64encode(buffer.getvalue()).decode("ascii"))
    finally:
        doc.close()
    return images


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    if not cleaned.startswith("{"):
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            raise OpenRouterError("Model did not return JSON.")
        cleaned = match.group(0)
    return json.loads(cleaned)


def _normalize_summary(data: dict, model: str) -> SummaryResult:
    return SummaryResult(
        title=str(data.get("title") or "MKR Job Totals"),
        job_totals=[str(v) for v in data.get("job_totals") or [] if str(v).strip()],
        materials=[str(v) for v in data.get("materials") or [] if str(v).strip()],
        warnings=[str(v) for v in data.get("warnings") or [] if str(v).strip()],
        confidence=float(data.get("confidence") or 0),
        model=model,
    )


async def summarize_with_model(pdf_bytes: bytes, settings: Settings, model: str | None = None) -> SummaryResult:
    if not settings.openrouter_api_key:
        raise OpenRouterError("OPENROUTER_API_KEY is not configured.")

    selected_model = model or settings.openrouter_model
    parsed_context = build_pdf_context(pdf_bytes)

    content: list[dict] = [{"type": "text", "text": USER_PROMPT.replace("{context}", parsed_context)}]
    image_count = 0
    if settings.include_page_images:
        images = render_pdf_images(pdf_bytes)
        image_count = len(images)
        for image in images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image}"},
                }
            )

    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
        "X-Title": settings.openrouter_app_title,
    }
    if settings.openrouter_http_referer:
        headers["HTTP-Referer"] = settings.openrouter_http_referer

    payload = {
        "model": selected_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0.1,
        "max_tokens": 2200,
        "response_format": {"type": "json_object"},
    }

    logger.info("requesting_summary model=%s image_pages=%s parsed_chars=%s", selected_model, image_count, len(parsed_context))
    async with httpx.AsyncClient(timeout=settings.openrouter_timeout_seconds) as client:
        response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
    if response.status_code >= 400:
        logger.warning("openrouter_error status=%s body=%s", response.status_code, response.text[:500])
        raise OpenRouterError(f"OpenRouter returned {response.status_code}.")

    data = response.json()
    text = data["choices"][0]["message"]["content"]
    summary = _normalize_summary(_extract_json(text), selected_model)
    if not summary.job_totals and not summary.materials:
        summary.warnings.append("Unable to identify supported totals from the drawing.")
    logger.info(
        "summary_complete model=%s totals=%s materials=%s confidence=%.2f",
        selected_model,
        len(summary.job_totals),
        len(summary.materials),
        summary.confidence,
    )
    return summary


async def try_models(pdf_bytes: bytes, settings: Settings, models: list[str]) -> list[ModelAttempt]:
    attempts: list[ModelAttempt] = []
    for model in models:
        try:
            summary = await summarize_with_model(pdf_bytes, settings, model=model)
            attempts.append(ModelAttempt(model=model, ok=True, summary=summary))
        except Exception as exc:  # noqa: BLE001 - recorded for model comparison notes
            logger.exception("model_attempt_failed model=%s", model)
            attempts.append(ModelAttempt(model=model, ok=False, error=str(exc)))
    return attempts
