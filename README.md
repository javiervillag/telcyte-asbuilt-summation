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

Uploaded and generated files are processed in temporary memory/files only and are not persisted by the app.
