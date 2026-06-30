#!/usr/bin/env python3
"""Real-data correctness gate for the NR-996825 re-run double-count (Nick, June-23).

Parses every NR-996825 PRJ17/PRJ18 variant Justin tested (in ~/Downloads) and
asserts the recomputed Comp-9 total equals the known-correct field value -
including the `telcyte-summary` outputs whose stamped box reads the DOUBLED
2976 and must de-double to 1488 on re-parse.

This pins the fix against real geometry (rotated, multi-page, page-totals boxes)
without relying on the harness's synthetic flatten, which is unfaithful on
rotated pages. Files are customer PDFs and live only in ~/Downloads, so this is
a local gate: it SKIPS cleanly when they are absent (no CI dependency).

Usage:  python scripts/regression/validate_gold.py
Exit:   0 = all pass or skipped (absent); 1 = a mismatch (regression).
"""
from __future__ import annotations

import glob
import os
import re
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)
from app.config import get_settings  # noqa: E402
from app.rate_cards import load_code_catalog  # noqa: E402
from app.pdf_parser import extract_text_blocks, derive_code_totals  # noqa: E402

# Known-correct Comp-9 field total per project (the box's own value once any
# previously stamped Job/Page Totals boxes are excluded as re-run evidence).
EXPECTED_COMP9 = {"PRJ17": "734", "PRJ18": "1488"}


def comp9(totals: list[str]) -> str | None:
    # Rows look like "Comp-9 - 1488"; take the quantity after the LAST " - ".
    for t in totals:
        if t.replace(" ", "").upper().startswith("COMP-9"):
            m = re.search(r"-\s*([\d.,]+)\s*$", t.strip())
            return m.group(1) if m else None
    return None


def main() -> int:
    files = sorted(glob.glob(os.path.expanduser("~/Downloads/*NR-996825*.pdf")))
    if not files:
        print("validate_gold: no NR-996825 files in ~/Downloads — skipped.")
        return 0

    settings = get_settings()
    catalog = load_code_catalog(settings.rate_card_codes, settings.rate_card_paths)
    failures = []
    for p in files:
        name = os.path.basename(p)
        project = "PRJ17" if "PRJ17" in name else ("PRJ18" if "PRJ18" in name else None)
        if project is None:
            continue
        expected = EXPECTED_COMP9[project]
        notes: list[str] = []
        with open(p, "rb") as fh:
            totals = derive_code_totals(extract_text_blocks(fh.read()), code_catalog=catalog, notes=notes)
        got = comp9(totals)
        ok = got == expected
        flag = "ok " if ok else "FAIL"
        print(f"  [{flag}] {project} Comp-9 expected {expected:>5} got {str(got):>5}  {name}")
        if not ok:
            failures.append((name, expected, got))

    print(f"\nvalidate_gold: {len(files)} files, {len(failures)} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
