from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import asdict, dataclass
from io import BytesIO
from typing import Any

import fitz
import httpx
from PIL import Image

from app.config import Settings
from app.models import SummaryResult
from app.pdf_parser import build_pdf_context, diagnose_extraction, derive_code_totals, extract_text_blocks
from app.rate_cards import CodeKey, code_key, load_code_catalog, total_line_key

logger = logging.getLogger(__name__)


class OpenRouterError(RuntimeError):
    pass


class ManualReviewRequired(OpenRouterError):
    def __init__(
        self,
        warnings: list[str],
        supported_totals: list[str] | None = None,
        unresolved_callouts: list[str] | None = None,
        verifier_model: str = "",
        verifier_used: bool = False,
        diagnostics: object | None = None,
    ) -> None:
        self.warnings = warnings
        self.supported_totals = supported_totals or []
        self.unresolved_callouts = unresolved_callouts or []
        self.verifier_model = verifier_model
        self.verifier_used = verifier_used
        self.diagnostics = _diagnostics_payload(diagnostics)
        super().__init__("Manual review required. The parsed PDF evidence did not fully support automatic totals.")


@dataclass
class ModelAttempt:
    model: str
    ok: bool
    summary: SummaryResult | None = None
    error: str | None = None


@dataclass
class ModelReview:
    summary: SummaryResult
    remaining_unresolved_callouts: list[str]
    resolved_callouts: list[dict[str, str]]


@dataclass
class CalloutResolutionReview:
    remaining_callouts: list[str]
    unsupported_resolution_count: int


def _diagnostics_payload(diagnostics: object | None) -> dict[str, Any]:
    if diagnostics is None:
        return {}
    if isinstance(diagnostics, dict):
        return diagnostics
    if hasattr(diagnostics, "__dataclass_fields__"):
        return asdict(diagnostics)
    return {}


SYSTEM_PROMPT = """You are helping Telcyte review parsed as-built construction drawings.
Use the parsed PDF text layer, positioned blocks, and deterministic aggregate candidates to review the MKR Job Totals box.
Return only JSON. Do not invent unreadable or uncertain details.
Use concise construction quantity lines like "UG-56 - 168'".
If a value is unclear, omit it or put a short warning in warnings."""


USER_PROMPT = """Analyze this parsed as-built PDF context and produce the green-box contents.
Expected JSON shape:
{
  "title": "MKR Job Totals",
  "job_totals": ["CODE - quantity", "..."],
  "materials": ["material - quantity", "..."],
  "warnings": ["short warning if needed"],
  "confidence": 0.0,
  "remaining_unresolved_callouts": ["copy any unresolved callout that still needs human interpretation"],
  "resolved_callouts": [
    {"callout": "exact unresolved callout text", "resolution": "why no manual interpretation is needed", "evidence": "specific parsed text evidence"}
  ]
}
Prefer the deterministic code totals when they are supported by the positioned text blocks.
Focus on billing-code totals visible or inferable from drawing labels, callouts, notes, and quantity markings.
Materials are phase-two unless the request explicitly enables them.
Never add a detail unless the parsed PDF context supports it.
For unresolved construction callouts, keep them in remaining_unresolved_callouts unless the parsed context explicitly proves they require no additional MKR total or are fully covered by deterministic supported totals.
Do not clear EOL, Tie Point, Storage, Pull through, or Pull-through callouts just because they look familiar; clear them only with explicit evidence.

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


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_review(data: dict, model: str) -> ModelReview:
    resolved_callouts: list[dict[str, str]] = []
    for item in data.get("resolved_callouts") or []:
        if not isinstance(item, dict):
            continue
        callout = str(item.get("callout") or "").strip()
        resolution = str(item.get("resolution") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        if callout:
            resolved_callouts.append(
                {
                    "callout": callout,
                    "resolution": resolution,
                    "evidence": evidence,
                }
            )

    summary = SummaryResult(
        title=str(data.get("title") or "MKR Job Totals"),
        job_totals=[str(v) for v in data.get("job_totals") or [] if str(v).strip()],
        materials=[str(v) for v in data.get("materials") or [] if str(v).strip()],
        warnings=[str(v) for v in data.get("warnings") or [] if str(v).strip()],
        confidence=float(data.get("confidence") or 0),
        model=model,
    )
    return ModelReview(
        summary=summary,
        remaining_unresolved_callouts=_string_list(data.get("remaining_unresolved_callouts")),
        resolved_callouts=resolved_callouts,
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
    omitted_model_totals = 0
    parser_total_keys = {key for line in parser_totals if (key := total_line_key(line))}
    parser_total_codes = {key[0] for key in parser_total_keys}
    model_disagreed_total_count = sum(
        1
        for line in model_summary.job_totals
        if (
            (model_key := total_line_key(line))
            and model_key[0] in parser_total_codes
            and model_key not in parser_total_keys
        )
    )
    if settings.allow_llm_inferred_totals:
        seen = {_line_code_key(line) for line in totals}
        for line in model_summary.job_totals:
            key = _line_code_key(line)
            if key and key not in seen:
                totals.append(line)
                seen.add(key)
    else:
        parser_keys = {_line_code_key(line) for line in totals}
        omitted_model_totals = sum(
            1
            for line in model_summary.job_totals
            if (_line_code_key(line) not in parser_keys)
        )

    warnings = list(model_summary.warnings)
    if omitted_model_totals:
        warnings.append(
            "Possible extra totals were not added because the parsed PDF text did not support them."
        )
    if model_disagreed_total_count:
        warnings.append(
            "Verifier returned a different quantity for a parser-supported code; the parser total was kept."
        )

    return SummaryResult(
        title="MKR Job Totals",
        job_totals=totals if totals or not settings.allow_llm_inferred_totals else model_summary.job_totals,
        materials=model_summary.materials if settings.include_materials else [],
        warnings=warnings,
        confidence=model_summary.confidence,
        model=f"parser+{model_summary.model}" if totals else model_summary.model,
    )


def _requires_manual_review_before_model(diagnostics) -> bool:
    return (
        diagnostics.block_count < 5
        or diagnostics.text_chars < 120
        or diagnostics.quantity_line_count < 2
        or diagnostics.code_total_count == 0
        or diagnostics.ambiguous_code_line_count > 0
    )


def _norm_callout(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _norm_evidence_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _resolved_callout_has_grounded_evidence(
    callout: str,
    evidence: str,
    parsed_context: str,
    parser_totals: list[str],
) -> bool:
    evidence_key = _norm_evidence_text(evidence)
    if len(evidence_key) < 6:
        return False
    callout_key = _norm_evidence_text(callout)
    if not evidence_key.replace(callout_key, "").strip():
        return False
    parser_total_keys = [_norm_evidence_text(total) for total in parser_totals]
    if any(evidence_key == total_key for total_key in parser_total_keys):
        return False
    context_key = _norm_evidence_text(parsed_context)
    if evidence_key in context_key:
        return True
    return False


def _remaining_callouts_after_model_review(
    original_callouts: list[str],
    model_review: ModelReview,
    parsed_context: str,
    parser_totals: list[str],
) -> CalloutResolutionReview:
    original_by_norm = {_norm_callout(callout): callout for callout in original_callouts if callout.strip()}
    remaining = dict(original_by_norm)
    unsupported_resolution_count = 0

    for item in model_review.resolved_callouts:
        norm = _norm_callout(item.get("callout") or "")
        if norm not in original_by_norm:
            continue
        resolution = (item.get("resolution") or "").strip()
        evidence = (item.get("evidence") or "").strip()
        if not resolution or not evidence:
            continue
        if not _resolved_callout_has_grounded_evidence(
            original_by_norm[norm],
            evidence,
            parsed_context,
            parser_totals,
        ):
            unsupported_resolution_count += 1
            continue
        remaining.pop(norm, None)

    for callout in model_review.remaining_unresolved_callouts:
        norm = _norm_callout(callout)
        if norm in original_by_norm:
            remaining[norm] = original_by_norm[norm]

    return CalloutResolutionReview(
        remaining_callouts=list(remaining.values()),
        unsupported_resolution_count=unsupported_resolution_count,
    )


def _review_prompt_context(
    parsed_context: str,
    parser_totals: list[str],
    diagnostics,
) -> str:
    context_parts = [
        "Deterministic supported totals:",
        *(parser_totals or ["None"]),
        "",
        "Unresolved construction callouts needing verifier review:",
        *(diagnostics.unresolved_callouts or ["None"]),
        "",
        parsed_context,
    ]
    return "\n".join(context_parts)


def _manual_review_warnings(
    diagnostics,
    model_summary: SummaryResult | None = None,
) -> list[str]:
    warnings = list(model_summary.warnings if model_summary else [])
    if model_summary is not None:
        verifier_warning = "OpenRouter verifier reviewed unresolved callouts but could not clear them from parsed evidence."
        if verifier_warning not in warnings:
            warnings.append(verifier_warning)
    warnings.extend(warning for warning in diagnostics.warnings if warning not in warnings)
    return warnings


def _raise_manual_review_for_unavailable_verifier(
    diagnostics,
    parser_totals: list[str],
    reason: str,
    verifier_model: str,
) -> None:
    warnings = list(diagnostics.warnings)
    warnings.append(f"OpenRouter verifier was unavailable ({reason}); manual review is required.")
    raise ManualReviewRequired(
        warnings,
        supported_totals=parser_totals,
        unresolved_callouts=diagnostics.unresolved_callouts,
        verifier_model=verifier_model,
        verifier_used=True,
        diagnostics=diagnostics,
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
    parser_totals = derive_code_totals(blocks, code_catalog=code_catalog)
    diagnostics = diagnose_extraction(blocks, parser_totals)
    if diagnostics.review_required and _requires_manual_review_before_model(diagnostics):
        raise ManualReviewRequired(
            diagnostics.warnings,
            supported_totals=parser_totals,
            unresolved_callouts=diagnostics.unresolved_callouts,
            verifier_model=selected_model,
            verifier_used=False,
            diagnostics=diagnostics,
        )

    if not settings.openrouter_api_key:
        if diagnostics.review_required:
            warnings = list(diagnostics.warnings)
            warnings.append("OpenRouter verifier is not configured, so unresolved callouts need manual review.")
            raise ManualReviewRequired(
                warnings,
                supported_totals=parser_totals,
                unresolved_callouts=diagnostics.unresolved_callouts,
                verifier_model=selected_model,
                verifier_used=False,
                diagnostics=diagnostics,
            )
        raise OpenRouterError("OPENROUTER_API_KEY is not configured.")

    parsed_context = build_pdf_context(pdf_bytes, code_catalog=code_catalog)
    verifier_context = _review_prompt_context(parsed_context, parser_totals, diagnostics)

    content: list[dict] = [{"type": "text", "text": USER_PROMPT.replace("{context}", verifier_context)}]
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
    try:
        async with httpx.AsyncClient(timeout=settings.openrouter_timeout_seconds) as client:
            response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
    except httpx.HTTPError as exc:
        if diagnostics.review_required:
            _raise_manual_review_for_unavailable_verifier(
                diagnostics,
                parser_totals,
                exc.__class__.__name__,
                selected_model,
            )
        raise
    if response.status_code >= 400:
        logger.warning("openrouter_error status=%s body=%s", response.status_code, response.text[:500])
        if diagnostics.review_required:
            _raise_manual_review_for_unavailable_verifier(
                diagnostics,
                parser_totals,
                f"OpenRouter returned {response.status_code}",
                selected_model,
            )
        raise OpenRouterError(f"OpenRouter returned {response.status_code}.")

    data = response.json()
    text = data["choices"][0]["message"]["content"]
    model_review = _normalize_review(_extract_json(text), selected_model)
    summary = _merge_parser_and_model(parser_totals, model_review.summary, settings)

    if diagnostics.unresolved_callouts:
        callout_review = _remaining_callouts_after_model_review(
            diagnostics.unresolved_callouts,
            model_review,
            parsed_context,
            parser_totals,
        )
        if callout_review.unsupported_resolution_count:
            summary.warnings.append(
                "Verifier tried to clear unresolved callouts with evidence not found in parsed PDF evidence."
            )
        if callout_review.remaining_callouts:
            raise ManualReviewRequired(
                _manual_review_warnings(diagnostics, summary),
                supported_totals=summary.job_totals,
                unresolved_callouts=callout_review.remaining_callouts,
                verifier_model=selected_model,
                verifier_used=True,
                diagnostics=diagnostics,
            )

    for warning in diagnostics.warnings:
        if "Readable construction callouts require rate-card/composite interpretation" in warning:
            continue
        if warning == "Manual review is required; the app did not add unsupported totals.":
            continue
        if warning not in summary.warnings:
            summary.warnings.append(warning)
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
