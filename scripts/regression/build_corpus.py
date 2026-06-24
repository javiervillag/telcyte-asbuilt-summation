#!/usr/bin/env python3
"""One-time builder for the curated regression corpus.

Scans the on-disk example library, signatures each PDF by behavioral shape
(pages / rotation / box / materials / review / code families), keeps ONE
representative per distinct shape (plus a force-included set of known edge
cases), and copies the winners into tmp/regression/corpus/ with a manifest.

Run-history DB is intentionally excluded: 160 of its 180 rows are the same
sample.pdf; its one unique shape is already covered by the disk BI-829050 files.

After this runs, harness.py reads the frozen snapshot from tmp/regression/corpus/,
so the baseline is reproducible even if the Downloads library is reorganised.
"""
from __future__ import annotations

import json
import os
import re
import glob
import shutil
import sys

import fitz

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)
from app.config import get_settings  # noqa: E402
from app.rate_cards import load_code_catalog  # noqa: E402
from app.pdf_parser import extract_text_blocks, derive_code_totals, diagnose_extraction  # noqa: E402

WR = os.path.abspath(os.path.join(REPO, ".."))
DL = os.path.expanduser("~/Downloads/Asbuilt Examples for AI Summation")
CORPUS = os.path.join(REPO, "tmp", "regression", "corpus")

LOCATIONS = [WR, DL, f"{DL}/Results", f"{DL}/Extra context from Nick",
             f"{REPO}/downloaded", f"{REPO}/output/pdf"]

# Always include these (match by basename substring) regardless of clustering,
# because they pin specific behaviors we must never regress.
FORCE = [
    "NR-1138768-DEROTATED", "NR-1138768-BAKED",        # multipage rotated + Page Totals box
    "VERIFIED-NR-702749-multipage",                     # multipage upright Job+Materials
    "RL-248790-Totals Removed", "RL-248790 (1)",        # rotated single page (clean + stamped)
    "BI-872022-NEW-OUTPUT",                             # spaced callouts (UG- 6 - 3)
    "BI-304069-telcyte-summary",                        # materials, 14 codes
    "SMOKE-TEST-BI-829050",                             # stamped output, re-run case
    "materials-visual-check",                           # materials-only / zero codes edge
]
# Real customer gold fixtures - always included verbatim by absolute path. Kept
# LOCAL only (tmp/regression is gitignored). Nick's June-23 NR-996825 PRJ18
# Segment 7 double-count: rotated (rot=270), with page-totals boxes on p3/p4.
GOLD_FILES = [
    os.path.expanduser("~/Downloads/FIBER-ASBUILT-(TelCyte)-NR-996825 PRJ18 - Segment 7 (2).pdf"),
    os.path.expanduser("~/Downloads/FIBER-ASBUILT-(TelCyte)-NR-996825 PRJ18 - Segment 7-telcyte-summary (2).pdf"),
]
CAP = 22


def signature(pdf_bytes, settings, catalog):
    d = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = d.page_count
    rots = tuple(sorted(set(d[i].rotation for i in range(pages))))
    alltxt = "".join(d[i].get_text("text") for i in range(min(pages, 12)))
    d.close()
    box = "PAGE" if "MKR Page Totals" in alltxt else ("JOB" if "MKR Job Totals" in alltxt else "none")
    hasmat = any(m in alltxt for m in ("Materials", "Material\r", "Material\n"))
    blocks = extract_text_blocks(pdf_bytes)
    totals = derive_code_totals(blocks, code_catalog=catalog, excluded_lines=[], notes=[], warnings=[])
    pref = tuple(sorted({re.match(r'([A-Za-z]+)', t.strip()).group(1).upper()
                         for t in totals if re.match(r'([A-Za-z]+)', t.strip())}))
    diag = diagnose_extraction(blocks, totals, excluded_context_lines=[], parser_notes=[],
                               parser_warnings=[], resolved_callout_lines=[], total_pages=pages)
    pb = "1p" if pages == 1 else ("2-4p" if pages <= 4 else "5plus")
    sig = (pb, "ROT" if any(rots) else "up", box, "mat" if hasmat else "x",
           "rev" if diag.review_required else "ok", pref)
    return sig, len(totals), pages


def main():
    settings = get_settings()
    catalog = load_code_catalog(settings.rate_card_codes, settings.rate_card_paths)
    files = []
    for L in LOCATIONS:
        files += glob.glob(f"{L}/*.pdf") + glob.glob(f"{L}/**/*.pdf", recursive=True)
    # Exclude our own snapshot dir: the recursive scan from the parent working
    # folder would otherwise re-ingest the corpus we are about to rebuild, making
    # selection non-deterministic across runs.
    files = sorted(f for f in set(files) if "/tmp/regression/" not in f)

    scanned = []
    for p in files:
        try:
            with open(p, "rb") as fh:
                b = fh.read()
            sig, nc, pg = signature(b, settings, catalog)
            scanned.append({"path": p, "sig": sig, "codes": nc, "pages": pg})
        except Exception:
            continue

    chosen, seen_sigs = {}, set()
    # 1) force-included edge cases
    for rec in scanned:
        base = os.path.basename(rec["path"])
        if any(f in base for f in FORCE) and rec["path"] not in chosen:
            chosen[rec["path"]] = rec
            seen_sigs.add(rec["sig"])
    # 2) one representative (most codes) per remaining distinct signature
    for rec in sorted(scanned, key=lambda r: -r["codes"]):
        if rec["sig"] in seen_sigs or rec["path"] in chosen:
            continue
        chosen[rec["path"]] = rec
        seen_sigs.add(rec["sig"])
        if len(chosen) >= CAP:
            break

    if os.path.isdir(CORPUS):
        shutil.rmtree(CORPUS)
    os.makedirs(CORPUS, exist_ok=True)

    manifest = []
    for i, rec in enumerate(sorted(chosen.values(), key=lambda r: (r["sig"][0], -r["codes"]))):
        sig = rec["sig"]
        shape = "_".join(sig[:5])
        stem = re.sub(r'[^A-Za-z0-9.-]+', '-', os.path.splitext(os.path.basename(rec["path"]))[0])[:40]
        dest = f"{i:02d}_{shape}__{stem}.pdf"
        dst = os.path.join(CORPUS, dest)
        try:
            with open(rec["path"], "rb") as fsrc, open(dst, "wb") as fdst:
                fdst.write(fsrc.read())
        except Exception as e:
            print(f"  skip (copy failed): {rec['path']!r} -> {dest}  {e!r}")
            continue
        manifest.append({"file": dest, "origin": rec["path"], "sig": list(sig),
                         "codes": rec["codes"], "pages": rec["pages"]})

    # Gold fixtures: copied verbatim by absolute path, never clustered away.
    for g, src in enumerate(GOLD_FILES):
        if not os.path.exists(src):
            print(f"  gold MISSING: {src}")
            continue
        base = re.sub(r'[^A-Za-z0-9.-]+', '-', os.path.splitext(os.path.basename(src))[0])[:44]
        dest = f"gold{g:02d}__{base}.pdf"
        with open(src, "rb") as fsrc, open(os.path.join(CORPUS, dest), "wb") as fdst:
            fdst.write(fsrc.read())
        manifest.append({"file": dest, "origin": src, "sig": ["gold"], "codes": -1, "pages": -1})

    with open(os.path.join(CORPUS, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)

    print(f"corpus -> {CORPUS}")
    print(f"selected {len(manifest)} PDFs ({len(scanned)} scanned, {len({tuple(m['sig']) for m in manifest})} shapes)\n")
    for m in manifest:
        print(f"  {m['file'][:62]:62}  {m['codes']:>2}c {m['pages']}p")


if __name__ == "__main__":
    main()
