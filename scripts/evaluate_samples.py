from __future__ import annotations

import argparse
import difflib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app
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


def evaluate_pair(client: TestClient, before: Path, team_output: Path, out_dir: Path) -> dict[str, Any]:
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
    supported_totals = "\n".join(str(line) for line in body.get("supported_totals") or [])
    result.update(
        {
            "result": "manual_review" if response.status_code == 422 else "error",
            "response_json": str(response_path),
            "detail": str(body.get("detail") or "")[:300],
            "warning_count": len(body.get("warnings") or []),
            "supported_total_count": len(body.get("supported_totals") or []),
            "unresolved_callout_count": len(body.get("unresolved_callouts") or []),
            "supported_vs_team_totals": compare_total_text(supported_totals, team_added),
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
    args = parser.parse_args()

    sample_dir = Path(args.samples)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else sample_dir / "Results" / f"endpoint_validation_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    pairs = find_pairs(sample_dir)
    if not pairs:
        raise SystemExit(f"No sample pairs found in {sample_dir}")

    client = TestClient(app)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "workflow": "FastAPI TestClient POST /api/summarize, then deterministic PDF text extraction comparison",
        "samples": [evaluate_pair(client, before, team_output, out_dir) for before, team_output in pairs],
    }
    report_path = out_dir / "endpoint-validation.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {report_path}")


if __name__ == "__main__":
    main()
