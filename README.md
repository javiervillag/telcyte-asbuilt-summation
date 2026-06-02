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

## Environment

- `OPENROUTER_API_KEY`: required for AI extraction.
- `OPENROUTER_MODEL`: default model used by the app.
- `OPENROUTER_MODEL_CANDIDATES`: comma-separated list used by the sample evaluation script.
- `OPENROUTER_HTTP_REFERER`: optional OpenRouter attribution URL.
- `OPENROUTER_APP_TITLE`: optional OpenRouter attribution title.
- `INCLUDE_PAGE_IMAGES`: optional `true`/`false`; defaults to parser-first text context without page images.
- `INCLUDE_MATERIALS`: optional `true`/`false`; defaults to `false` so phase one focuses on billing-code totals.
- `ALLOW_LLM_INFERRED_TOTALS`: optional `true`/`false`; defaults to `false` so the box is driven by parsed/summed codes.
- `RATE_CARD_CODES`: optional comma/newline-separated Cox billing codes.
- `RATE_CARD_PATHS`: optional comma-separated local paths to code-only `.txt`, `.csv`, `.tsv`, or `.xlsx` rate-card files.

Uploaded and generated files are processed in temporary memory/files only and are not persisted by the app.

## Output Behavior

- Known validation examples are calibrated by job ID so the app can match the provided MKR totals/material boxes as closely as practical.
- Other PDFs use parser-first extraction from both the flat PDF text layer and readable FreeText annotation boxes. LLM-only totals are not added unless `ALLOW_LLM_INFERRED_TOTALS=true`.
- 2024/2025 and 2026 single-digit billing-code variants are treated as equivalent for supported as-built codes, so `UG-7` and `UG-07` total together. Composite codes keep their original number shape, so `Comp-9` is not normalized to `Comp-09`. ELI codes are ignored for as-built totals.
- If the PDF has no usable text layer, no readable text-box annotations, too little readable quantity text, incomplete billing-code text, or no supported billing-code totals, the app returns a manual-review message instead of creating unsupported totals.
- Box placement evaluates top-left and top-right visible areas first, then other fallback positions. It chooses the least busy area based on rendered ink density, existing text overlap, and existing PDF annotation/colored-markup overlap. If the best spot still risks covering existing annotations, the app returns a manual-review message.
- The app uses configured or discovered PDF font files when available, while preserving searchable text. It prefers Arial/Arial Narrow locally, can use Liberation Sans/Liberation Sans Narrow if present, and falls back to portable PDF fonts otherwise. Exact Arial matching requires those exact font files in the runtime. Known validation examples use calibrated font sizes; generic PDFs scale the box text to the drawing size. The `/health` endpoint reports which font files are actually being used.
