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

from app.additional_materials import AdditionalMaterialResult, derive_additional_materials
from app.cable_footage import CableFootageResult, derive_cable_footage
from app.config import Settings
from app.models import SummaryResult
from app.pdf_parser import (
    build_pdf_context,
    derive_code_total_map,
    derive_code_totals,
    derive_code_totals_by_page,
    diagnose_extraction,
    extract_text_blocks,
)
from app.rate_cards import CodeKey, code_key, load_code_catalog

logger = logging.getLogger(__name__)


class OpenRouterError(RuntimeError):
    pass


class ManualReviewRequired(OpenRouterError):
    def __init__(
        self,
        warnings: list[str],
        supported_totals: list[str] | None = None,
        unresolved_callouts: list[str] | None = None,
        informational_notes: list[str] | None = None,
        cable_footage: list | None = None,
        materials: list[str] | None = None,
    ) -> None:
        self.warnings = warnings
        self.supported_totals = supported_totals or []
        self.unresolved_callouts = unresolved_callouts or []
        self.informational_notes = informational_notes or []
        self.cable_footage = cable_footage or []
        self.materials = materials or []
        super().__init__("Manual review required. The parsed PDF evidence did not fully support automatic totals.")


@dataclass
class ModelAttempt:
    model: str
    ok: bool
    summary: SummaryResult | None = None
    error: str | None = None


SYSTEM_PROMPT = """You are Telcyte's evidence-first as-built billing review assistant.

<role>
Review extracted PDF text, positioned annotation blocks, deterministic parser totals, and suspicious quantity lines for MKR Job Totals.
</role>

<rules>
- Return only valid JSON matching the requested schema.
- Treat the extracted PDF context as evidence. Do not invent totals, quantities, materials, or implied business-rule codes.
- Prefer parser totals when the positioned text supports them.
- Add or correct a visible billing-code total only when the code and quantity are both present in extracted evidence.
- Surface uncertainty as warnings instead of silently guessing.
- Unknown or new billing-code prefixes may be valid if the label is visible, for example "DP-11 - 156'" or "SME-01 - 1".
- Ignore any previously stamped summary box that appears in the evidence - "MKR Job Totals", "MKR Page Totals", "MKR New Totals", or a "Materials" box: each is output from an earlier run or the customer's own annotation, not a field callout. The deterministic parser already excludes these by title; do not re-count their lines.
- Unit markers (' and sqft) are ignored: quantities for the same code always total together and totals are written without units.
- Do not auto-add implied extras such as PC-02 unless visible evidence contains that code and quantity.
</rules>

<format>
Use concise construction lines like "UG-56 - 168'".
</format>"""


USER_PROMPT = """Analyze the parsed as-built PDF context.

<task>
Produce the MKR Job Totals review using only supported evidence. Compare deterministic totals against positioned text blocks and likely quantity lines. Look for missed visible billing-code labels, including combined labels in one box and labels with nearby descriptive words such as Asphalt or Concrete.
</task>

<json_schema>
{
  "title": "MKR Job Totals",
  "job_totals": ["CODE - quantity", "..."],
  "materials": ["material - quantity", "..."],
  "warnings": ["short warning if something needs human review"],
  "confidence": 0.0
}
</json_schema>

<decision_rules>
- Include high-confidence visible billing-code totals.
- If parser and evidence disagree, include the evidence-supported total and add a warning.
- If a possible code is visible but the quantity is unclear, omit it from job_totals and add a warning.
- Materials are phase-two unless explicitly enabled in context.
- Keep warnings short and actionable.
</decision_rules>

<parsed_context>
{context}
</parsed_context>"""


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
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.S | re.I)
    candidates = [fenced.group(1).strip()] if fenced else []
    candidates.append(cleaned)
    decoder = json.JSONDecoder()
    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        candidate = re.sub(r"^```(?:json)?", "", candidate).strip()
        candidate = re.sub(r"```$", "", candidate).strip()
        for match in re.finditer(r"\{", candidate):
            try:
                value, _end = decoder.raw_decode(candidate[match.start() :])
            except json.JSONDecodeError as exc:
                last_error = exc
                continue
            if isinstance(value, dict):
                return value
    if "{" not in cleaned:
        raise OpenRouterError("Model did not return JSON.")
    raise OpenRouterError("Model returned invalid or truncated JSON.") from last_error


def _normalize_summary(data: dict, model: str) -> SummaryResult:
    return SummaryResult(
        title=str(data.get("title") or "MKR Job Totals"),
        job_totals=[str(v) for v in data.get("job_totals") or [] if str(v).strip()],
        materials=[str(v) for v in data.get("materials") or [] if str(v).strip()],
        warnings=[str(v) for v in data.get("warnings") or [] if str(v).strip()],
        confidence=float(data.get("confidence") or 0),
        model=model,
    )


def _line_code_key(line: str) -> CodeKey | None:
    code_part = line.split("-", 2)
    if len(code_part) >= 2:
        return code_key("-".join(code_part[:2]))
    return code_key(line)


def _merge_parser_and_model(
    parser_totals: list[str],
    model_summary: SummaryResult,
    settings: Settings,
) -> SummaryResult:
    totals = list(parser_totals)
    omitted_model_lines: list[str] = []
    if settings.allow_llm_inferred_totals:
        seen = {_line_code_key(line) for line in totals}
        for line in model_summary.job_totals:
            key = _line_code_key(line)
            if key and key not in seen:
                totals.append(line)
                seen.add(key)
    else:
        parser_keys = {_line_code_key(line) for line in totals}
        omitted_model_lines = [
            line
            for line in model_summary.job_totals
            if (_line_code_key(line) not in parser_keys)
        ]

    warnings = list(model_summary.warnings)
    if omitted_model_lines:
        preview = "; ".join(omitted_model_lines[:6])
        if len(omitted_model_lines) > 6:
            preview += f"; plus {len(omitted_model_lines) - 6} more"
        warnings.append(
            f"Model review suggested possible extra totals not auto-added without parser support: {preview}."
        )

    return SummaryResult(
        title="MKR Job Totals",
        job_totals=totals if totals or not settings.allow_llm_inferred_totals else model_summary.job_totals,
        materials=model_summary.materials if settings.include_materials else [],
        warnings=warnings,
        confidence=model_summary.confidence,
        model=f"parser+{model_summary.model}" if totals else model_summary.model,
    )


async def summarize_with_model(
    pdf_bytes: bytes,
    settings: Settings,
    model: str | None = None,
    source_name: str | None = None,
) -> SummaryResult:
    selected_model = model or settings.openrouter_model
    code_catalog = load_code_catalog(settings.rate_card_codes, settings.rate_card_paths)
    blocks = extract_text_blocks(pdf_bytes)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total_pages = doc.page_count
    finally:
        doc.close()
    excluded_context_lines: list[str] = []
    parser_notes: list[str] = []
    parser_warnings: list[str] = []
    parser_totals = derive_code_totals(
        blocks,
        code_catalog=code_catalog,
        excluded_lines=excluded_context_lines,
        notes=parser_notes,
        warnings=parser_warnings,
    )
    # Per-page billing totals for the "MKR Page Totals" boxes (multi-page sheets).
    # Deterministic parser output - never routed through the LLM merge.
    page_totals = derive_code_totals_by_page(blocks, code_catalog=code_catalog)
    material_code_totals = derive_code_total_map(blocks, code_catalog=code_catalog, apply_catalog=False)
    cable_result = (
        derive_cable_footage(
            blocks,
            auto_stamp=settings.auto_stamp_cable_footage,
            path_code=settings.cable_path_code,
            coax_rounding_increment=settings.coax_rounding_increment,
        )
        if settings.include_cable_footage
        else CableFootageResult()
    )
    additional_materials = (
        derive_additional_materials(blocks, code_totals_by_key=material_code_totals)
        if settings.include_cable_footage and settings.auto_stamp_cable_footage
        else AdditionalMaterialResult()
    )
    resolved_callout_lines = set(cable_result.handled_callout_lines) | set(additional_materials.handled_callout_lines)
    diagnostics = diagnose_extraction(
        blocks,
        parser_totals,
        excluded_context_lines=excluded_context_lines,
        parser_notes=parser_notes,
        parser_warnings=parser_warnings,
        resolved_callout_lines=resolved_callout_lines,
        total_pages=total_pages,
    )
    if diagnostics.review_required and (
        not settings.enable_model_review_on_warnings or not settings.openrouter_api_key
    ):
        raise ManualReviewRequired(
            diagnostics.warnings,
            supported_totals=parser_totals,
            unresolved_callouts=diagnostics.unresolved_callouts,
            informational_notes=diagnostics.informational_notes,
            cable_footage=cable_result.lines,
            materials=additional_materials.material_rows,
        )

    if not settings.openrouter_api_key:
        raise OpenRouterError("OPENROUTER_API_KEY is not configured.")

    parsed_context = build_pdf_context(pdf_bytes, code_catalog=code_catalog)

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
        # Keep enough room for large multi-page drawing context, but do not
        # ask the reviewer for extra verbosity/reasoning. Some providers honor
        # JSON mode loosely and append narrative notes after the object.
        "max_tokens": 6000,
        "response_format": {"type": "json_object"},
    }

    logger.info("requesting_summary model=%s image_pages=%s parsed_chars=%s", selected_model, image_count, len(parsed_context))
    try:
        async with httpx.AsyncClient(timeout=settings.openrouter_timeout_seconds) as client:
            response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
        if response.status_code >= 400:
            logger.warning("openrouter_error status=%s body=%s", response.status_code, response.text[:500])
            raise OpenRouterError(f"OpenRouter returned {response.status_code}.")

        data = response.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenRouterError(f"OpenRouter response had no completion: {str(data)[:200]}") from exc
        model_summary = _normalize_summary(_extract_json(text), selected_model)
    except Exception as exc:
        if diagnostics.review_required:
            logger.warning("model_review_failed_using_parser_review model=%s error=%s", selected_model, exc)
            raise ManualReviewRequired(
                diagnostics.warnings,
                supported_totals=parser_totals,
                unresolved_callouts=diagnostics.unresolved_callouts,
                informational_notes=diagnostics.informational_notes,
                cable_footage=cable_result.lines,
                materials=additional_materials.material_rows,
            ) from exc
        if parser_totals:
            # The deterministic parser is the source of truth (its totals
            # always win and, in production, model-only totals are dropped
            # anyway). A reviewer failure must never sink a run that has
            # parser-backed totals - fall back to parser-only output with a
            # visible warning (NR-702749 3x processing_error, 2026-06-10).
            logger.warning(
                "model_review_failed_using_parser_totals model=%s error=%s", selected_model, exc
            )
            model_summary = SummaryResult(
                title="MKR Job Totals",
                job_totals=[],
                warnings=[
                    "Model review was unavailable for this run; totals are parser-only."
                ],
                confidence=0.5,
                model=selected_model,
            )
        else:
            raise
    summary = _merge_parser_and_model(parser_totals, model_summary, settings)
    summary = _with_cable_footage(summary, cable_result, settings)
    summary = _with_additional_materials(summary, additional_materials)
    for warning in diagnostics.warnings:
        if warning not in summary.warnings:
            summary.warnings.append(warning)
    for note in diagnostics.informational_notes:
        if note not in summary.informational_notes:
            summary.informational_notes.append(note)
    if not summary.job_totals and not summary.materials:
        summary.warnings.append("Unable to identify supported totals from the drawing.")
    summary = summary.model_copy(update={"page_totals": page_totals})
    logger.info(
        "summary_complete model=%s totals=%s materials=%s confidence=%.2f",
        selected_model,
        len(summary.job_totals),
        len(summary.materials),
        summary.confidence,
    )
    return summary


def _with_cable_footage(
    summary: SummaryResult,
    cable_result: CableFootageResult,
    settings: Settings,
) -> SummaryResult:
    if not settings.include_cable_footage:
        return summary
    materials = list(summary.materials)
    for line in cable_result.lines:
        if line.eligible_for_stamp and line.material_line and line.material_line not in materials:
            materials.append(line.material_line)
    warnings = list(summary.warnings)
    for warning in cable_result.warnings:
        if warning not in warnings:
            warnings.append(warning)
    informational_notes = list(summary.informational_notes)
    for note in cable_result.informational_notes:
        if note not in informational_notes:
            informational_notes.append(note)
    return summary.model_copy(
        update={
            "materials": materials,
            "cable_footage": cable_result.lines,
            "warnings": warnings,
            "informational_notes": informational_notes,
        }
    ).with_eligible_cable_materials()


def _with_additional_materials(
    summary: SummaryResult,
    additional_materials: AdditionalMaterialResult,
) -> SummaryResult:
    materials = list(summary.materials)
    for row in additional_materials.material_rows:
        if row not in materials:
            materials.append(row)
    warnings = list(summary.warnings)
    for warning in additional_materials.warnings:
        if warning not in warnings:
            warnings.append(warning)
    informational_notes = list(summary.informational_notes)
    for note in additional_materials.informational_notes:
        if note not in informational_notes:
            informational_notes.append(note)
    if (
        materials == summary.materials
        and warnings == summary.warnings
        and informational_notes == summary.informational_notes
    ):
        return summary
    return summary.model_copy(
        update={
            "materials": materials,
            "warnings": warnings,
            "informational_notes": informational_notes,
        }
    )


async def try_models(
    pdf_bytes: bytes,
    settings: Settings,
    models: list[str],
    source_name: str | None = None,
) -> list[ModelAttempt]:
    attempts: list[ModelAttempt] = []
    for model in models:
        try:
            summary = await summarize_with_model(pdf_bytes, settings, model=model, source_name=source_name)
            attempts.append(ModelAttempt(model=model, ok=True, summary=summary))
        except Exception as exc:  # noqa: BLE001 - recorded for model comparison notes
            logger.exception("model_attempt_failed model=%s", model)
            attempts.append(ModelAttempt(model=model, ok=False, error=str(exc)))
    return attempts
