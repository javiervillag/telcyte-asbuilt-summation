#!/usr/bin/env python3
"""Golden-master regression harness for the As-Built Summation pipeline.

Captures the DETERMINISTIC results of the parser + annotator layer for a corpus of
real PDFs (run-history blobs + on-disk examples), freezes them as a baseline, and
diffs future runs against that baseline with success metrics.

It deliberately does NOT call the LLM: the parser is the source of truth (its totals
always win, and with ALLOW_LLM_INFERRED_TOTALS=false model-only totals are dropped),
so the deterministic layer is the right thing to characterise and is reproducible
with no network.

Usage:
    python scripts/regression/harness.py capture [--smoke] [--limit N]
    python scripts/regression/harness.py check   [--smoke] [--limit N]

Artifacts live under tmp/regression/ (gitignored) so customer PDFs and their
extracted totals never reach the public GitHub repo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field

import fitz  # PyMuPDF

# --- make the app importable regardless of CWD ---
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from app.config import get_settings  # noqa: E402
from app.rate_cards import load_code_catalog  # noqa: E402
from app.pdf_parser import (  # noqa: E402
    extract_text_blocks,
    derive_code_totals,
    derive_code_totals_by_page,
    diagnose_extraction,
)
from app.pdf_annotator import annotate_pdf, PlacementReviewRequired  # noqa: E402
from app.models import SummaryResult  # noqa: E402

try:
    from app.cable_footage import derive_cable_footage, CableFootageResult  # noqa: E402
except Exception:  # pragma: no cover - cable module optional
    derive_cable_footage = None
    CableFootageResult = None

# Same normalized box-title rule production uses, so the harness can't drift from
# the parser/annotator and correctly tolerates whitespace/wrapped titles.
from app.box_titles import starts_with_materials_title, starts_with_totals_title  # noqa: E402

OUT_DIR = os.path.join(REPO, "tmp", "regression")
BASELINE = os.path.join(OUT_DIR, "baseline.json")
CORPUS = os.path.join(OUT_DIR, "corpus")  # frozen curated snapshot (see build_corpus.py)


def _is_box_title(content: str) -> bool:
    return starts_with_totals_title(content) or starts_with_materials_title(content)


@dataclass
class Record:
    key: str
    source: str
    sha: str
    pages: int
    rotations: list
    ok: bool
    error: str = ""
    parser_totals: list = field(default_factory=list)
    warnings: int = 0
    notes: int = 0
    review_required: bool = False
    materials: list = field(default_factory=list)
    boxes: list = field(default_factory=list)  # [{page,title,lines}]
    idem_contiguous: str = "n/a"  # ok | DRIFT | error
    idem_flattened: str = "n/a"   # ok | DOUBLED | error

    def digest(self) -> str:
        payload = json.dumps(
            {
                "parser_totals": sorted(self.parser_totals),
                "warnings": self.warnings,
                "review_required": self.review_required,
                "materials": sorted(self.materials),
                "boxes": [
                    {"page": b["page"], "lines": sorted(b["lines"])} for b in self.boxes
                ],
                # Include idempotency status so a change that re-introduces the
                # flattened double-count (without altering first-pass totals,
                # materials, or boxes) still changes the digest and fails `check`.
                "idem_contiguous": self.idem_contiguous,
                "idem_flattened": self.idem_flattened,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _settings():
    return get_settings()


def _parse(pdf_bytes: bytes, settings, catalog):
    """Deterministic parser layer -> (parser_totals, warnings, notes, review_required, materials)."""
    blocks = extract_text_blocks(pdf_bytes)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total_pages = doc.page_count
    finally:
        doc.close()
    excluded, notes, warnings = [], [], []
    totals = derive_code_totals(
        blocks, code_catalog=catalog, excluded_lines=excluded, notes=notes, warnings=warnings
    )
    # Per-page totals drive the "MKR Page Totals" boxes on multi-page sheets - the
    # exact path that caused the June-23 NR-996825 double-count. Characterize it too.
    page_totals = derive_code_totals_by_page(blocks, code_catalog=catalog)
    materials = []
    handled = []
    if settings.include_cable_footage and derive_cable_footage is not None:
        cable = derive_cable_footage(
            blocks,
            auto_stamp=settings.auto_stamp_cable_footage,
            path_codes=settings.cable_path_code,
            coax_rounding_increment=settings.coax_rounding_increment,
        )
        handled = cable.handled_callout_lines
        for ln in cable.lines:
            if ln.eligible_for_stamp and ln.material_line:
                materials.append(ln.material_line)
    diag = diagnose_extraction(
        blocks,
        totals,
        excluded_context_lines=excluded,
        parser_notes=notes,
        parser_warnings=warnings,
        resolved_callout_lines=handled,
        total_pages=total_pages,
    )
    return {
        "totals": list(totals),
        "warnings": list(diag.warnings),
        "notes": list(notes),
        "review_required": bool(diag.review_required),
        "materials": materials,
        "page_totals": {int(k): list(v) for k, v in (page_totals or {}).items()},
    }


def _extract_boxes(pdf_bytes: bytes) -> list:
    """Read stamped totals/materials boxes (FreeText annots) -> [{page,title,lines}]."""
    boxes = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for i in range(doc.page_count):
            page = doc[i]
            if not page.annots():
                continue
            for a in page.annots():
                if a.type[1] != "FreeText":
                    continue
                content = (a.info.get("content") or "").strip()
                if _is_box_title(content):
                    lines = [l.strip() for l in content.replace("\r", "\n").split("\n") if l.strip()]
                    boxes.append({"page": i, "title": lines[0] if lines else "", "lines": lines})
    finally:
        doc.close()
    return boxes


def _flatten_boxes(pdf_bytes: bytes) -> bytes:
    """Reproduce an editor 'flatten': re-render each totals/materials FreeText box as
    separate page-text blocks (title + each line), then delete the annotation.
    This is the June-23 failure mode (title block excluded, code lines double-counted)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for i in range(doc.page_count):
            page = doc[i]
            if not page.annots():
                continue
            to_delete = []
            for a in page.annots():
                if a.type[1] != "FreeText":
                    continue
                content = (a.info.get("content") or "").strip()
                if not _is_box_title(content):
                    continue
                rect = a.rect
                lines = [l for l in content.replace("\r", "\n").split("\n")]
                # Faithful adversarial flatten: each line becomes its OWN text block.
                # PyMuPDF groups lines spaced < ~14pt into a single block (which the
                # existing title-prefix guard catches, hiding the bug); a >=16pt gap
                # splits them, reproducing the June-23 separated-block double-count.
                y = rect.y0 + 12
                for ln in lines:
                    if ln.strip():
                        page.insert_text((rect.x0 + 2, y), ln, fontsize=9, color=(1, 0, 0))
                    y += 16
                to_delete.append(a)
            for a in to_delete:
                page.delete_annot(a)
        return doc.tobytes()
    finally:
        doc.close()


def build_record(key: str, source: str, pdf_bytes: bytes, settings, catalog, history_totals=None) -> Record:
    sha = hashlib.sha256(pdf_bytes).hexdigest()[:16]
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = doc.page_count
        rotations = [doc[i].rotation for i in range(pages)]
        doc.close()
    except Exception as e:
        return Record(key=key, source=source, sha=sha, pages=0, rotations=[], ok=False, error=f"open:{e}")

    rec = Record(key=key, source=source, sha=sha, pages=pages, rotations=rotations, ok=True)
    try:
        base = _parse(pdf_bytes, settings, catalog)
        rec.parser_totals = base["totals"]
        rec.warnings = len(base["warnings"])
        rec.notes = len(base["notes"])
        rec.review_required = base["review_required"]
        rec.materials = base["materials"]
    except Exception as e:
        rec.ok = False
        rec.error = f"parse:{e}"
        return rec

    # Annotate (parser-only summary) to capture stamped boxes + run idempotency.
    try:
        summary = SummaryResult(
            title="MKR Job Totals",
            job_totals=rec.parser_totals,
            materials=rec.materials,
            page_totals=base.get("page_totals", {}),
            model="parser-only",
        ).with_eligible_cable_materials()
        try:
            output = annotate_pdf(pdf_bytes, summary, source_name=key)
            rec.boxes = _extract_boxes(output)
        except PlacementReviewRequired:
            rec.boxes = []
            output = None

        if output is not None:
            # (a) contiguous re-run: re-parse the freshly stamped output
            re_a = _parse(output, settings, catalog)
            rec.idem_contiguous = "ok" if sorted(re_a["totals"]) == sorted(rec.parser_totals) else "DRIFT"
            # (b) flattened re-run: the actual bug surface
            flat = _flatten_boxes(output)
            re_b = _parse(flat, settings, catalog)
            rec.idem_flattened = "ok" if sorted(re_b["totals"]) == sorted(rec.parser_totals) else "DOUBLED"
    except Exception as e:
        rec.idem_contiguous = rec.idem_flattened = f"error:{type(e).__name__}"
    return rec


def gather():
    """Read the frozen curated corpus snapshot (built by build_corpus.py)."""
    import glob as _glob
    out = []
    for p in sorted(_glob.glob(os.path.join(CORPUS, "*.pdf"))):
        with open(p, "rb") as fh:
            out.append((os.path.basename(p), "corpus", fh.read(), None))
    return out


def run():
    settings = _settings()
    catalog = load_code_catalog(settings.rate_card_codes, settings.rate_card_paths)
    items = gather()
    t0 = time.time()
    records = []
    for key, source, blob, hist in items:
        records.append(build_record(key, source, blob, settings, catalog, hist))
    dur = time.time() - t0
    return records, dur


def scoreboard(records, dur, baseline=None):
    n = len(records)
    ok = [r for r in records if r.ok]
    failed = [r for r in records if not r.ok]
    review = [r for r in ok if r.review_required]
    total_codes = sum(len(r.parser_totals) for r in ok)
    idem_c_ok = sum(1 for r in ok if r.idem_contiguous == "ok")
    idem_c_n = sum(1 for r in ok if r.idem_contiguous in ("ok", "DRIFT"))
    idem_f_ok = sum(1 for r in ok if r.idem_flattened == "ok")
    idem_f_n = sum(1 for r in ok if r.idem_flattened in ("ok", "DOUBLED"))
    doubled = [r for r in ok if r.idem_flattened == "DOUBLED"]
    print("=" * 64)
    print(f"REGRESSION SCOREBOARD   corpus={n}  ok={len(ok)}  failed={len(failed)}  ({dur:.1f}s)")
    print("-" * 64)
    print(f"  parser-readable totals (codes)   : {total_codes}")
    print(f"  manual-review-required PDFs      : {len(review)}")
    print(f"  idempotency CONTIGUOUS (re-stamp): {idem_c_ok}/{idem_c_n} ok")
    print(f"  idempotency FLATTENED  (the bug) : {idem_f_ok}/{idem_f_n} ok   <-- R2 target 100%")
    if doubled:
        print(f"  >> {len(doubled)} PDF(s) DOUBLE-COUNT when flattened (current bug):")
        for r in doubled[:8]:
            print(f"       - {r.key}  pages={r.pages}")
    if baseline is not None:
        changed = diff_against(records, baseline)
        print("-" * 64)
        print(f"  vs baseline: {len(changed)} changed / {n}")
        for c in changed[:12]:
            print(f"       ~ {c}")
    print("=" * 64)


def diff_against(records, baseline):
    base = {b["key"]: b for b in baseline["records"]}
    changed = []
    for r in records:
        b = base.get(r.key)
        if b is None:
            changed.append(f"NEW {r.key}")
        elif b.get("digest") != r.digest():
            changed.append(f"{r.key}: {b.get('digest')} -> {r.digest()}")
    for k in base:
        if k not in {r.key for r in records}:
            changed.append(f"MISSING {k}")
    return changed


def serialize(records, dur):
    return {
        "captured_dur_s": round(dur, 1),
        "count": len(records),
        "records": [
            {
                "key": r.key, "source": r.source, "sha": r.sha, "pages": r.pages,
                "rotations": r.rotations, "ok": r.ok, "error": r.error,
                "parser_totals": r.parser_totals, "warnings": r.warnings, "notes": r.notes,
                "review_required": r.review_required, "materials": r.materials,
                "boxes": r.boxes, "idem_contiguous": r.idem_contiguous,
                "idem_flattened": r.idem_flattened, "digest": r.digest(),
            }
            for r in records
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["capture", "check"])
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)

    records, dur = run()

    if args.cmd == "capture":
        with open(BASELINE, "w") as fh:
            json.dump(serialize(records, dur), fh, indent=1)
        print(f"baseline frozen -> {BASELINE}")
        scoreboard(records, dur)
    else:
        if not os.path.exists(BASELINE):
            print("no baseline; run capture first", file=sys.stderr)
            sys.exit(2)
        with open(BASELINE) as fh:
            baseline = json.load(fh)
        scoreboard(records, dur, baseline=baseline)
        changed = diff_against(records, baseline)
        sys.exit(1 if changed else 0)


if __name__ == "__main__":
    main()
