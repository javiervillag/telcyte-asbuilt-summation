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

The app still extracts and sums visible billing-code labels from the PDF by default. No implied billing codes are added automatically.

Use the **Optional extra billing codes** section when Telcyte has confirmed extra billing items that are not shown as direct PDF labels. Search for a code, select it, enter the quantity, and add a short note when helpful. The generated PDF separates those lines under **User-selected extra totals** so they are clearly distinct from the direct extracted totals.

The optional-code catalog is based on the unmatched-code review and MCA rate-card descriptions. It includes the main families seen in the sample comparisons: Preconstruction, Coax/HFC, Fiber, Performance/testing, Time/labor, and Composite.

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
