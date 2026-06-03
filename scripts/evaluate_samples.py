from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz
import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app
from app.rate_cards import ZERO_PAD_EQUIVALENT_PREFIXES
from app.rate_cards import total_line_key


def pdf_text(path: Path) -> str:
    doc = fitz.open(path)
    try:
        return "\n".join(page.get_text("text") for page in doc)
    finally:
        doc.close()


def pdf_text_from_bytes(pdf_bytes: bytes) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return "\n".join(page.get_text("text") for page in doc)
    finally:
        doc.close()


def added_text(before_text: str, after_text: str) -> str:
    matcher = difflib.SequenceMatcher(None, before_text, after_text)
    chunks: list[str] = []
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            chunk = after_text[j1:j2].strip()
            if chunk:
                chunks.append(chunk)
    return "\n".join(chunks)


def expected_added_text(before: Path, after: Path) -> str:
    return added_text(pdf_text(before), pdf_text(after))


def total_keys_from_text(text: str) -> set[tuple[tuple[str, str], str, str]]:
    keys = set()
    for line in text.splitlines():
        key = total_line_key(line)
        if key:
            keys.add(key)
    return keys


def compare_total_text(actual_text: str, expected_text: str) -> dict[str, Any]:
    actual_keys = total_keys_from_text(actual_text)
    expected_keys = total_keys_from_text(expected_text)
    return {
        "actual_total_count": len(actual_keys),
        "expected_total_count": len(expected_keys),
        "missing_total_count": len(expected_keys - actual_keys),
        "extra_total_count": len(actual_keys - expected_keys),
        "missing_totals": sorted(_format_total_key(key) for key in expected_keys - actual_keys),
        "extra_totals": sorted(_format_total_key(key) for key in actual_keys - expected_keys),
    }


def classify_missing_total_evidence(
    input_text: str,
    missing_totals: list[str],
    unresolved_callouts: list[str] | None = None,
) -> list[dict[str, Any]]:
    return [
        _missing_total_evidence(input_text, total, unresolved_callouts or [])
        for total in missing_totals
    ]


def summarize_missing_total_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in evidence:
        evidence_class = str(item.get("evidence_class") or "unknown")
        if evidence_class not in grouped:
            order.append(evidence_class)
            grouped[evidence_class] = {
                "evidence_class": evidence_class,
                "count": 0,
                "totals": [],
            }
        grouped[evidence_class]["count"] += 1
        grouped[evidence_class]["totals"].append(str(item.get("total") or ""))
    return [grouped[evidence_class] for evidence_class in order]


def _missing_total_evidence(input_text: str, total: str, unresolved_callouts: list[str]) -> dict[str, Any]:
    key = total_line_key(total)
    if not key:
        return {
            "total": total,
            "evidence_class": "unparseable_total",
            "exact_total_present": False,
            "code_present": False,
            "quantity_present": False,
            "unresolved_callout_context": bool(unresolved_callouts),
            "related_unresolved_callouts": unresolved_callouts,
            "matching_lines": [],
        }

    (prefix, number), quantity, unit = key
    code_patterns = [_code_regex(prefix, variant) for variant in _code_number_variants(prefix, number)]
    quantity_pattern = _quantity_regex(quantity, unit)
    exact_patterns = [
        re.compile(rf"{code.pattern}\s*-\s*{quantity_pattern.pattern}", re.I)
        for code in code_patterns
    ]
    lines = [line.strip() for line in input_text.splitlines() if line.strip()]
    matching_lines = [
        line
        for line in lines
        if any(pattern.search(line) for pattern in exact_patterns)
        or any(pattern.search(line) for pattern in code_patterns)
        or quantity_pattern.search(line)
    ][:8]
    exact_total_present = any(pattern.search(input_text) for pattern in exact_patterns)
    code_present = any(pattern.search(input_text) for pattern in code_patterns)
    quantity_present = bool(quantity_pattern.search(input_text))
    unresolved_callout_context = bool(unresolved_callouts)
    return {
        "total": total,
        "evidence_class": _missing_total_evidence_class(
            exact_total_present,
            code_present,
            quantity_present,
            unresolved_callout_context,
        ),
        "exact_total_present": exact_total_present,
        "code_present": code_present,
        "quantity_present": quantity_present,
        "unresolved_callout_context": unresolved_callout_context,
        "related_unresolved_callouts": unresolved_callouts if unresolved_callout_context else [],
        "matching_lines": matching_lines,
    }


def _missing_total_evidence_class(
    exact_total_present: bool,
    code_present: bool,
    quantity_present: bool,
    unresolved_callout_context: bool,
) -> str:
    if exact_total_present:
        return "direct_total_text"
    if code_present:
        return "billing_code_text_without_matching_total"
    if quantity_present:
        return "quantity_text_without_billing_code"
    if unresolved_callout_context:
        return "unresolved_construction_callout_context"
    return "no_direct_input_evidence"


def _code_number_variants(prefix: str, number: str) -> list[str]:
    variants = [number]
    if prefix in ZERO_PAD_EQUIVALENT_PREFIXES and number.isdigit():
        variants.append(str(int(number)))
        variants.append(f"{int(number):02d}")
    deduped: list[str] = []
    for variant in variants:
        if variant not in deduped:
            deduped.append(variant)
    return deduped


def _code_regex(prefix: str, number: str) -> re.Pattern[str]:
    if prefix in ZERO_PAD_EQUIVALENT_PREFIXES and number.isdigit():
        number_pattern = rf"0*{re.escape(str(int(number)))}"
    else:
        number_pattern = re.escape(number)
    return re.compile(rf"\b{re.escape(prefix)}-?{number_pattern}\b", re.I)


def _quantity_regex(quantity: str, unit: str) -> re.Pattern[str]:
    if not _quantity_is_distinct(quantity, unit):
        return re.compile(r"a^")
    suffix = r"\s*" + re.escape(unit) if unit else r"(?!\s*(?:\d|'|sqft))"
    return re.compile(rf"\b{re.escape(quantity)}{suffix}", re.I)


def _quantity_is_distinct(quantity: str, unit: str) -> bool:
    if unit:
        return True
    try:
        return float(quantity) >= 10
    except ValueError:
        return False


def find_pairs(folder: Path) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for before in sorted(folder.glob("*-Totals Removed.pdf")):
        expected_name = before.name.replace("-Totals Removed", "")
        candidates = [
            folder / expected_name,
            folder / expected_name.replace(".pdf", " (1).pdf"),
        ]
        for candidate in candidates:
            if candidate.exists():
                pairs.append((before, candidate))
                break
    return pairs


def health_status(client: Any) -> dict[str, Any]:
    try:
        response = client.get("/health")
        try:
            body = response.json()
        except Exception:
            body = {"text": response.text[:1000]}
        return {
            "ok": response.status_code < 400,
            "status_code": response.status_code,
            "body": body,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc)[:500],
        }


def evaluate_pair(client: Any, before: Path, team_output: Path, out_dir: Path) -> dict[str, Any]:
    before_text = pdf_text(before)
    team_text = pdf_text(team_output)
    team_added = added_text(before_text, team_text)
    team_totals = compare_total_text(team_added, team_added)

    sample_dir = out_dir / before.stem
    sample_dir.mkdir(parents=True, exist_ok=True)
    (sample_dir / "01_team_added_text.txt").write_text(team_added, encoding="utf-8")

    response = client.post(
        "/api/summarize",
        files={"file": (before.name, before.read_bytes(), "application/pdf")},
    )
    result: dict[str, Any] = {
        "input": str(before),
        "team_output": str(team_output),
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "team_added_total_count": team_totals["expected_total_count"],
    }

    if response.status_code == 200:
        generated_pdf = response.content
        output_path = sample_dir / "02_app_output.pdf"
        output_path.write_bytes(generated_pdf)
        generated_text = pdf_text_from_bytes(generated_pdf)
        generated_added = added_text(before_text, generated_text)
        (sample_dir / "03_app_added_text.txt").write_text(generated_added, encoding="utf-8")
        result.update(
            {
                "result": "annotated_pdf",
                "output_pdf": str(output_path),
                "model": response.headers.get("x-telcyte-model", ""),
                "confidence": response.headers.get("x-telcyte-confidence", ""),
                "warnings": response.headers.get("x-telcyte-warnings", ""),
                "app_vs_team_totals": compare_total_text(generated_added, team_added),
            }
        )
        return result

    try:
        body = response.json()
    except Exception:
        body = {"detail": response.text[:1000]}
    response_path = sample_dir / "02_app_response.json"
    response_path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    diagnostics = body.get("diagnostics") or {}
    supported_totals = "\n".join(str(line) for line in body.get("supported_totals") or [])
    supported_comparison = compare_total_text(supported_totals, team_added)
    missing_total_input_evidence = classify_missing_total_evidence(
        before_text,
        supported_comparison["missing_totals"],
        [str(callout) for callout in body.get("unresolved_callouts") or []],
    )
    result.update(
        {
            "result": "manual_review" if response.status_code == 422 else "error",
            "response_json": str(response_path),
            "detail": str(body.get("detail") or "")[:300],
            "warning_count": len(body.get("warnings") or []),
            "warnings": [str(warning) for warning in body.get("warnings") or []],
            "supported_total_count": len(body.get("supported_totals") or []),
            "supported_totals": [str(total) for total in body.get("supported_totals") or []],
            "unresolved_callout_count": len(body.get("unresolved_callouts") or []),
            "unresolved_callouts": [str(callout) for callout in body.get("unresolved_callouts") or []],
            "unresolved_callout_details": diagnostics.get("unresolved_callout_details") or [],
            "unresolved_callout_summary": (
                body.get("unresolved_callout_summary")
                or diagnostics.get("unresolved_callout_summary")
                or []
            ),
            "verifier_model": body.get("verifier_model") or "",
            "verifier_used": bool(body.get("verifier_used")),
            "diagnostics": diagnostics,
            "supported_vs_team_totals": supported_comparison,
            "missing_total_input_evidence": missing_total_input_evidence,
            "missing_total_evidence_summary": summarize_missing_total_evidence(
                missing_total_input_evidence
            ),
        }
    )
    return result


def _format_total_key(key: tuple[tuple[str, str], str, str]) -> str:
    (prefix, number), quantity, unit = key
    number_text = number if prefix == "COMP" else f"{int(number):02d}" if number.isdigit() else number
    return f"{prefix}-{number_text} - {quantity}{unit}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run sample PDFs through the app endpoint and compare extracted output text to Telcyte team PDFs."
    )
    parser.add_argument(
        "--samples",
        default="/Users/javiervillaguardado/Downloads/Asbuilt Examples for AI Summation",
    )
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--base-url",
        default="",
        help="Optional deployed app base URL. When omitted, the local FastAPI app is used through TestClient.",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    sample_dir = Path(args.samples)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else sample_dir / "Results" / f"endpoint_validation_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    pairs = find_pairs(sample_dir)
    if not pairs:
        raise SystemExit(f"No sample pairs found in {sample_dir}")

    base_url = args.base_url.rstrip("/")
    if base_url:
        client_context = httpx.Client(base_url=base_url, timeout=args.timeout)
        workflow = f"HTTP POST {base_url}/api/summarize, then deterministic PDF text extraction comparison"
    else:
        client_context = TestClient(app)
        workflow = "FastAPI TestClient POST /api/summarize, then deterministic PDF text extraction comparison"

    with client_context as client:
        report = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "workflow": workflow,
            "endpoint_health": health_status(client),
            "samples": [evaluate_pair(client, before, team_output, out_dir) for before, team_output in pairs],
        }
    report_path = out_dir / "endpoint-validation.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {report_path}")


if __name__ == "__main__":
    main()
