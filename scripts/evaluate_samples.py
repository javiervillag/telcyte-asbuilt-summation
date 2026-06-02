from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import re
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.openrouter_client import try_models
from app.pdf_annotator import annotate_pdf
from app.rate_cards import total_line_key


def normalized_text(path: Path) -> str:
    doc = fitz.open(path)
    try:
        return "\n".join(page.get_text("text") for page in doc)
    finally:
        doc.close()


def expected_added_text(before: Path, after: Path) -> str:
    before_text = normalized_text(before)
    after_text = normalized_text(after)
    matcher = difflib.SequenceMatcher(None, before_text, after_text)
    chunks: list[str] = []
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            chunk = after_text[j1:j2].strip()
            if chunk:
                chunks.append(chunk)
    return "\n".join(chunks)


def tokens(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)?|'|sqft", text) if len(t) > 1}


def score_summary(summary_text: str, expected_text: str) -> float:
    found = tokens(summary_text)
    expected = tokens(expected_text)
    if not expected:
        return 0.0
    return len(found & expected) / len(expected)


def total_keys(lines: list[str]) -> set:
    return {key for line in lines if (key := total_line_key(line))}


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


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--samples",
        default="/Users/javiervillaguardado/Downloads/Asbuilt Examples for AI Summation",
    )
    parser.add_argument("--out", default="sample_reports")
    parser.add_argument("--models", default=None)
    args = parser.parse_args()

    sample_dir = Path(args.samples)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    models = [m.strip() for m in args.models.split(",")] if args.models else settings.candidate_models
    pairs = find_pairs(sample_dir)
    if not pairs:
        raise SystemExit(f"No sample pairs found in {sample_dir}")

    report = {"models": models, "samples": []}
    for before, after in pairs:
        print(f"Evaluating {before.name}")
        expected = expected_added_text(before, after)
        attempts = await try_models(before.read_bytes(), settings, models, source_name=before.name)
        rows = []
        best = None
        best_score = -1.0
        for attempt in attempts:
            if attempt.ok and attempt.summary:
                summary_text = "\n".join(attempt.summary.display_lines())
                score = score_summary(summary_text, expected)
                found_keys = total_keys(attempt.summary.job_totals)
                expected_keys = total_keys(expected.splitlines())
                rows.append(
                    {
                        "model": attempt.model,
                        "ok": True,
                        "score": round(score, 4),
                        "normalized_missing_total_count": len(expected_keys - found_keys),
                        "normalized_extra_total_count": len(found_keys - expected_keys),
                        "confidence": attempt.summary.confidence,
                        "totals": attempt.summary.job_totals,
                        "materials": attempt.summary.materials,
                        "warnings": attempt.summary.warnings,
                    }
                )
                if score > best_score:
                    best = attempt.summary
                    best_score = score
            else:
                rows.append({"model": attempt.model, "ok": False, "error": attempt.error})
        if best:
            output_pdf = annotate_pdf(before.read_bytes(), best, source_name=before.name)
            output_path = out_dir / before.name.replace(".pdf", "-generated.pdf")
            output_path.write_bytes(output_pdf)
        report["samples"].append(
            {
                "input": str(before),
                "expected": str(after),
                "expected_added_text": expected,
                "attempts": rows,
                "chosen_model": best.model if best else None,
                "best_score": round(best_score, 4) if best else None,
                "output_pdf": str(output_path) if best else None,
            }
        )

    (out_dir / "model-comparison.json").write_text(json.dumps(report, indent=2))
    print(f"Wrote {out_dir / 'model-comparison.json'}")


if __name__ == "__main__":
    asyncio.run(main())
