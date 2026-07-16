# Telcyte As-Built Summation

Internal Telcyte tool for adding an AI-generated MKR Job Totals box to an as-built PDF.

## Local Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
OPENROUTER_API_KEY=... uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`, upload one PDF, and download the annotated result.

## Optional Extra Billing Codes

The app extracts and sums visible billing-code labels from the PDF by default. It does not automatically guess implied billing codes.

Use **Optional Extra Billing Codes** when Telcyte has confirmed extra billing items that are not shown as direct PDF labels. Search for a code, select it, enter the quantity, and optionally add a short note. The generated PDF separates those lines under **User-selected extra totals** so they stay distinct from the direct extracted totals.

The optional-code catalog uses the unmatched-code review and MCA rate-card descriptions, grouped as Preconstruction, Coax/HFC, Fiber, Performance/testing, Time/labor, and Composite.

## Environment

- `OPENROUTER_API_KEY`: required for AI extraction.
- `OPENROUTER_MODEL`: default model used by the app.
- `OPENROUTER_MODEL_CANDIDATES`: comma-separated list used by the sample evaluation script.
- `OPENROUTER_HTTP_REFERER`: optional OpenRouter attribution URL.
- `OPENROUTER_APP_TITLE`: optional OpenRouter attribution title.
- `INCLUDE_PAGE_IMAGES`: optional `true`/`false`; defaults to parser-first text context without page images.
- `INCLUDE_MATERIALS`: optional `true`/`false`; defaults to `false` so phase one focuses on billing-code totals.
- `ALLOW_LLM_INFERRED_TOTALS`: optional `true`/`false`; defaults to `false` so the box is driven by parsed/summed codes.
- `STRICT_REVIEW_BADGES`: optional `true`/`false`; defaults to `false`. When true, any warning or note shows as Review.
- `CABLE_PATH_CODE`: optional comma-separated composite codes whose callouts carry pulled cable footage; defaults to `Comp-15,Comp-10`.
- `RATE_CARD_CODES`: optional comma/newline-separated Cox billing codes.
- `RATE_CARD_PATHS`: optional comma-separated local paths to code-only `.txt`, `.csv`, `.tsv`, or `.xlsx` rate-card files. For `.xlsx` files, highlighted cells are preferred first, highlighted tabs are used next, and the full workbook is used only when no highlights are found.

Run history stores uploaded input PDFs and generated output PDFs so prior runs can be searched, audited, replayed, and downloaded. PDF retention is still an open product decision; do not add automatic deletion without Telcyte approval.

Run-history savings use Nick's 2026-06-08 estimate of about 8 minutes saved per completed as-built. Dollar savings stay hidden until the hourly rate is confirmed.

## Output Behavior

- Production behavior is generic. The app does not use job IDs, filenames, or known sample outputs to choose totals, materials, or placement.
- PDFs use parser-first extraction from both the flat PDF text layer and readable FreeText annotation boxes. LLM-only totals are not added unless `ALLOW_LLM_INFERRED_TOTALS=true`.
- Billing quantities can be read from direct totals like `UG-06 - 13` and quantity-first notes like `13 x UG-6`. Quantity-first notes are ignored when an equivalent direct total is already present, so the same evidence is not counted twice.
- 2024/2025 and 2026 single-digit billing-code variants are treated as equivalent for supported as-built codes, so `UG-7` and `UG-07` total together. Composite codes keep their original number shape, so `Comp-9` is not normalized to `Comp-09`. ELI codes are ignored for as-built totals.
- If the PDF has no usable text layer, no readable text-box annotations, too little readable quantity text, incomplete billing-code text, unresolved construction callouts, or no supported billing-code totals, the app returns a manual-review message instead of creating unsupported totals. For unresolved construction callouts, the manual-review response includes the specific lines that still need rate-card/composite interpretation plus any supported totals the parser did find.
- Confirmed cable sheets can treat 48Ct, 144Ct, 288Ct, `.625`, `.875`, Drop F, RG6, and RG11 as Tie Point-to-EOL sequences: the difference between the terminal T (tail) jacket numbers is the raw footage, with storage, risers, and end slack already inside the span. Jacket markers accept `D`/`T` plus pole-top `Top`/`Pole`/`P` prefixes, with or without a dash before the digits; a terminal callout may carry either one marker or a marker pair. Each cable family keeps its established buffer and rounding rule. When a splice or multiple terminal markers suggest multiple cables through the same route, the established path method is used instead. When sequence and path-code methods are both available, their rounded quantities must agree exactly or the Materials line is marked `VERIFY` and the run requires review.
- Box placement evaluates top-left and top-right visible areas first, then other fallback positions. It chooses the least busy area based on rendered ink density, existing text overlap, and existing PDF annotation/colored-markup overlap. If the best spot still risks covering existing annotations, the app returns a manual-review message.
- The app uses configured or discovered PDF font files when available, while preserving searchable text. It prefers Arial/Arial Narrow locally, can use Liberation Sans/Liberation Sans Narrow if present, and falls back to portable PDF fonts otherwise. Exact Arial matching requires those exact font files in the runtime. Generic PDFs scale the box text to the drawing size. The `/health` endpoint reports which font files are actually being used.
