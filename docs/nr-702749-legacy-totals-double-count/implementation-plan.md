# NR-702749 Legacy Totals Double-Count - Implementation and Go-Live Plan

**Status:** Planning only. No application code is changed by this document.
**Production incident:** Run `761d99bcf66e4c1eb4a7e2940c566be6`, July 22, 2026 at 10:38 AM local time.
**Objective:** Prevent legacy Job/Page tally boxes from entering billing calculations, replace verified superseded Job/Page variants with one canonical tool box, retain protected customer summaries and genuine field callouts, and fail safely when a future tally uses an unfamiliar title.

## 1. Validated root cause

The production input contains four movable FreeText annotations authored by the subcontractor crew:

| PDF page | Heading | Contents relevant to billing |
|---|---|---|
| 1 | `MKR Job Total` | `UG-37 - 1852`, `UG-6 - 85` |
| 2 | `Pg 1 Total` | `UG-6 - 10` |
| 3 | `Pg 2 Total` | `UG-37 - 1192`, `UG-6 - 40` |
| 4 | `Pg 3 Total` | `UG-37 - 660`, `UG-6 - 35` |

The shared title recognizer currently accepts only headings beginning with `MKR Job Totals`, `MKR Page Totals`, or `MKR New Totals`. The singular `MKR Job Total` and numbered `Pg N Total` variants therefore reach the generic billing aggregator as ordinary field evidence.

The arithmetic proves the failure:

- `UG-37`: existing Job tally `1852` + numbered Page tallies `1192 + 660` = incorrect `3704`.
- `UG-06`: existing Job tally `85` + numbered Page tallies `10 + 40 + 35` + actual recognized field callouts `18` = incorrect `188`.
- Removing only those four annotations in a controlled local experiment reproduces the later clean parser result exactly: `UG-06 - 18`, `UG-85 - 14`, `Comp-9 - 1852`, `Comp-6 - 1852`, `UG-38 - 3704`, with no field-backed `UG-37`.

This is the same defect class as the earlier re-run problem, but not the same PDF representation. These boxes are live comments, not flattened remnants. The previous fix correctly handles canonical titles and flattened canonical boxes; it does not recognize this crew vocabulary.

### Local evidence - do not commit the customer PDFs

```text
/Users/javiervillaguardado/Documents/Claude/Telcyte_asbuilt-summation_fix_10 Jun/telcyte-asbuilt-summation/output/pdf/NR-702749-double-count-analysis/NR-702749-input.pdf

/Users/javiervillaguardado/Documents/Claude/Telcyte_asbuilt-summation_fix_10 Jun/telcyte-asbuilt-summation/output/pdf/NR-702749-double-count-analysis/NR-702749-output.pdf

/Users/javiervillaguardado/Documents/Claude/Telcyte_asbuilt-summation_fix_10 Jun/telcyte-asbuilt-summation/output/pdf/NR-702749-double-count-analysis/NR-702749-evidence.json
```

## 2. Finishing criteria

The change is complete only when all of the following are true:

1. The verified crew headings are excluded from Job and Page calculations.
2. The verified legacy Job/Page tally annotations are replaced in place by one canonical, editable tool box rather than remaining as visible duplicates.
3. Canonical tool boxes continue to be replaced in place on re-runs.
4. Genuine field callouts outside a tally box remain counted, even when their text matches a tally row.
5. A future unclassified total-like annotation cannot produce a green result with silently inflated totals.
6. The NR-702749 input produces the field-only parser result above and is marked Review because the ignored crew summaries contain values not independently supported by field evidence.
7. Existing canonical, split-title, rotated, and flattened-box tests remain green.
8. Full tests, gold validation, and the real regression harness pass with only the explicitly approved NR-702749 correction.
9. Production is verified by deployed commit, Railway health/logs, exact upload result, evidence endpoint, and rendered output.

## 3. Design principles

### 3.1 Make calculation and replacement decisions explicit

Two questions must be represented explicitly by the shared classifier:

- **Should this box feed calculations?** Recognized Job/Page/New summaries: no.
- **May the tool delete and replace this box?** Canonical and verified legacy Job/Page totals: yes. `Previously Billed`, New Totals, Materials, and unknown formats: no.

This distinction prevents summary arithmetic from being counted without turning every total-like phrase into something the tool may delete. The verified `MKR Job Total` and `Pg N Total` boxes are superseded Job/Page summaries: preserving them while adding new canonical boxes recreates the visible duplicate-box symptom. Existing protected categories remain untouched.

### 3.2 One source of truth

All title normalization and classification stays in `app/box_titles.py`. The parser and annotator must import it; neither may maintain a second regex list.

Keep the existing public functions for compatibility, but make them views over one typed classifier:

- `starts_with_job_totals_title`: canonical or verified legacy Job summary; replaceable unless protected as `Previously Billed`.
- `starts_with_page_totals_title`: canonical or verified numbered Page summary; replaceable unless protected as `Previously Billed`.
- `starts_with_new_totals_title`: existing New Totals behavior.
- `starts_with_totals_title`: broad calculation-exclusion umbrella.

Add one explicit helper for diagnostics and tests:

- `is_legacy_totals_alias`: true only for the newly verified singular/numbered forms.

### 3.3 Exact aliases, not loose keyword matching

Match a normalized heading prefix with precompiled, anchored regular expressions:

```python
_LEGACY_JOB_TOTAL_RE = re.compile(r"^mkr\s+job\s+totals?\b", re.I)
_LEGACY_NUMBERED_PAGE_TOTAL_RE = re.compile(
    r"^(?:pg|page)\s+\d+\s+totals?\b",
    re.I,
)
```

The verified aliases are:

- `MKR Job Total` / `MKR Job Totals`
- `Pg <number> Total` / `Pg <number> Totals`
- `Page <number> Total` / `Page <number> Totals`

Do not add an unanchored `contains("total")` rule. Do not automatically classify generic headings such as `Job Cost Total`, `Total Bore Length`, or `Construction Total`.

The heading normalizer must continue supporting split canonical titles such as:

```text
MKR Job
Totals
```

Use the first two nonblank lines for title-prefix normalization. Do not normalize the entire annotation body into the title classifier.

### 3.4 Unknown formats fail visibly

Add a conservative diagnostic for an annotation that:

- is not a recognized Materials or totals box;
- has a first nonblank line containing the standalone word `Total` or `Totals`; and
- has at least one subsequent billing-looking `CODE - QUANTITY` row.

Such a box must be excluded from aggregation, preserved on the PDF, and produce a deterministic Review issue including page and heading. It must never remain `Done` or `Done - Notes` merely because the model reviewer missed it. The positive criteria above and negative title tests below keep this fail-safe narrow.

For the first release, do not add a sum-echo heuristic that silently excludes untitled boxes. Equality with field totals is not proof that a block is a summary; it may be a legitimate grouped callout. Unknown formats should be Review until real examples justify another exact alias.

## 4. Exact code changes

### 4.1 `app/box_titles.py`

Add:

```python
def normalized_heading(text: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return normalized_title(" ".join(lines[:2]))


def is_legacy_totals_alias(text: str) -> bool:
    heading = normalized_heading(text)
    return bool(
        _LEGACY_JOB_TOTAL_RE.match(heading)
        or _LEGACY_NUMBERED_PAGE_TOTAL_RE.match(heading)
    ) and not bool(
        _CANONICAL_JOB_TOTAL_RE.match(heading)
        or _CANONICAL_PAGE_TOTAL_RE.match(heading)
    )
```

Classify canonical and verified legacy Job/Page headings centrally:

```python
class TotalsTitleKind(str, Enum):
    JOB = "job"
    PAGE = "page"
    NEW = "new"


def totals_title_kind(text: str) -> TotalsTitleKind | None:
    heading = normalized_heading(text)
    if _CANONICAL_JOB_TOTAL_RE.match(heading) or _LEGACY_JOB_TOTAL_RE.match(heading):
        return TotalsTitleKind.JOB
    if _CANONICAL_PAGE_TOTAL_RE.match(heading) or _LEGACY_NUMBERED_PAGE_TOTAL_RE.match(heading):
        return TotalsTitleKind.PAGE
    if _CANONICAL_NEW_TOTAL_RE.match(heading):
        return TotalsTitleKind.NEW
    return None
```

All existing predicates become one-line views over `totals_title_kind()`. The annotator's existing `is_previously_billed_totals_box()` carve-out remains authoritative, so broadening Job/Page recognition does not delete protected prior-billing boxes. `MKR New Totals`, tool-owned `Additions`, and Materials handling remain unchanged. This structure avoids regex duplication and recursive predicate bugs.

### 4.2 `app/pdf_parser.py`

Import `is_legacy_totals_alias` or `totals_title_kind` from `app.box_titles`.

The existing `extract_text_blocks()` and `field_evidence_blocks()` already call `starts_with_totals_title`. Once the counting umbrella recognizes crew aliases:

- the annotation remains available as one evidence block;
- its rows do not enter annotation-line deduplication;
- the summary block is excluded before billing aggregation;
- a matching genuine field callout elsewhere remains available;
- flattened versions reuse the existing tight spatial-remnant exclusion.

Do not add a second filtering pass.

Before calling `field_evidence_blocks()` in `derive_code_totals()`, collect the verified legacy aliases once for deterministic reporting:

```python
legacy_boxes = [block for block in blocks if is_legacy_totals_alias(block.text)]
```

After aggregation:

- emit `SummaryIssue(severity="notice", code="legacy_totals_ignored", ...)` identifying how many verified legacy tally boxes were excluded;
- compare excluded summary claims with field-backed totals and emit `SummaryIssue(severity="blocker", code="summary_conflict", ...)` only when a code is absent from, or conflicts with, the applicable field evidence;
- emit `SummaryIssue(severity="blocker", code="unclassified_totals_summary", ...)` for an unknown total-like annotation caught by the fail-safe in section 3.4;
- include a short page/title preview, capped to avoid oversized headers/history payloads.

Suggested wording:

```text
Ignored 4 legacy Job/Page tally boxes as calculation evidence to prevent double-counting.
```

Do not ask the model reviewer to infer this. The deterministic parser owns the issues and their severity. Do not also append equivalent free-text warnings that would be reconciled as duplicate `unclassified_warning` issues.

Add a small helper in `pdf_parser.py` for the unknown-summary diagnostic described in section 3.4. Reuse the existing billing-code patterns; do not introduce another parser or a broad free-text heuristic. Include matching unknown summary blocks in the existing `field_evidence_blocks()` anchor/exclusion pass, but never in the annotator replacement predicates.

### 4.3 `app/pdf_annotator.py`

Do not add a second title list or a special NR-702749 branch. The existing functions `_existing_job_totals_boxes()` and `_existing_page_totals_boxes()` continue importing the shared Job/Page predicates. Once those predicates use `totals_title_kind()`:

- `MKR Job Total` is selected and replaced in place by one `MKR Job Totals` box;
- `Pg N Total` is selected and replaced in place by one `MKR Page Totals` box on that PDF page;
- replacement is positional: the physical PDF page containing the annotation controls which page-total box is replaced; the number written in `Pg N Total` is never parsed or used as a page index;
- multiple matching boxes are consolidated using the existing `_select_primary_output_box()` and delete-by-xref path;
- `Previously Billed`, New Totals/Additions, Materials, and unclassified total-like boxes remain protected;
- a re-run replaces the canonical output rather than creating duplicates.

Add no author-name allowlist. Author metadata proved these were third-party boxes, but names are not stable enough to become business logic.

### 4.4 `app/models.py` / status derivation

No schema change is required. Add the explicit structured issues in section 4.2 to `SummaryResult.issues`; do not rely on the generic warning fallback. Confirm with tests that:

- `legacy_totals_ignored` alone yields `Done - Notes`;
- `summary_conflict` yields yellow Review;
- `unclassified_totals_summary` yields yellow Review.

Do not add a new status, database column, feature flag, or API field for this fix.

### 4.5 Documentation

Update the relevant project context to state:

- canonical and exact verified legacy Job/Page summaries are replaced;
- protected `Previously Billed`, New Totals/Additions, Materials, and unknown summaries are preserved;
- unknown total-like annotations cause Review;
- summaries never become field evidence merely because their heading is unfamiliar.

## 5. Testing plan

### 5.1 Unit title classification

Add table-driven tests for:

**Excluded from counting and replaceable as superseded Job/Page summaries**

- `MKR Job Total`
- `Pg 1 Total`
- `Pg 2 Totals`
- `Page 3 Total`

**Excluded and replaceable canonical tool boxes**

- `MKR Job Totals`
- `MKR Job\nTotals`
- `MKR Page Totals`

**Not classified as totals boxes**

- `Construction Total`
- `Total Bore Length`
- `Job Cost Total`
- `Subtotal`
- `MKR Job Totalizer`
- a normal field callout containing the word `total` in its body

**Excluded from counting and protected from replacement**

- `MKR Job Totals - Previously Billed`
- `MKR Page Totals - Previously Billed`
- `MKR New Totals - Add resto`
- `MKR New Totals / Additions` according to its existing ownership rule
- `Materials` and manual material rows

Assert both dimensions explicitly: `excluded_from_counting` and `replaceable_by_tool`.

### 5.2 Sanitized NR-702749 regression fixture

Create a synthetic five-page PDF in the test itself. Do not commit the customer PDF.

Fixture contents:

- Page 1 crew box: `MKR Job Total / UG-37 - 1852 / UG-6 - 85`.
- Page 2 crew box: `Pg 1 Total / UG-6 - 10` plus a genuine `Dirt - UG-6 - 1` field callout.
- Page 3 crew box: `Pg 2 Total / UG-37 - 1192 / UG-6 - 40` plus field evidence totaling the real case.
- Page 4 crew box: `Pg 3 Total / UG-37 - 660 / UG-6 - 35` plus field evidence totaling the real case.
- Page 5 no billing evidence.

Assert:

```text
UG-06 - 18
UG-85 - 14
Comp-9 - 1852
Comp-6 - 1852
UG-38 - 3704
```

Assert no `UG-37` is emitted from crew tallies, four crew boxes are reported, and status is Review.

### 5.3 Preservation and replacement behavior

After annotation:

- the four legacy headings and stale contents are gone;
- one canonical `MKR Job Totals` output exists;
- each applicable later page has at most one canonical `MKR Page Totals` output;
- the canonical replacement reuses the prior box location where safe;
- the canonical boxes are movable FreeText annotations;
- protected `Previously Billed`, New Totals/Additions, and Materials annotations remain unchanged;
- rendering shows no incoherent overlap.

Re-upload the output and assert:

- legacy headings do not reappear;
- canonical boxes remain one each;
- Job/Page totals do not grow;
- the Review warning does not duplicate.

### 5.4 Flattened and spatial edge cases

Add synthetic tests for:

- flattened `MKR Job Total` with title and rows in separate blocks;
- flattened `Pg 2 Total` with separate rows;
- a genuine matching field callout in another column;
- a field callout directly below but outside the existing tight adjacency window;
- rotated pages at 90 and 270 degrees;
- multiple crew boxes on one page;
- a canonical `MKR Page Totals` box and a legacy `Pg N Total` box on the same page: both are excluded from counting, consolidated to one canonical replacement, and use the existing primary-anchor selection;
- split canonical titles still working;
- an unknown total-like annotation producing Review rather than green.

The exclusion must remain linear in the number of text blocks. Do not add OCR, image rendering, geometry scanning, or an LLM request.

### 5.5 Real local acceptance

Run the exact downloaded NR-702749 input locally, without committing it:

1. Confirm the field-only totals listed above.
2. Confirm four crew tally boxes are excluded and reported.
3. Confirm the run is Review because legacy-summary-only totals conflict with field evidence.
4. Confirm the four legacy boxes are replaced by canonical movable FreeText boxes in their established locations.
5. Confirm protected summary/material boxes remain unchanged.
6. Confirm canonical tool boxes do not duplicate after a second pass.
7. Render pages 1-4 and confirm there is one coherent summary box per intended location.

### 5.6 Full regression gates

Run on the production-pinned dependency versions:

```bash
python3 -m pytest -q
python3 scripts/regression/validate_gold.py
python3 scripts/regression/harness.py check
node --check static/app.js
```

Run `check` against the trusted pre-change baseline first. It is expected to expose the deliberate NR-702749 correction rather than pass vacuously. Review every changed record and confirm there is no unrelated drift. Only after that review is approved may the baseline be refreshed:

```bash
python3 scripts/regression/harness.py capture
python3 scripts/regression/harness.py check
```

The final `check` must report zero drift against the newly approved baseline. Never run `capture` before the initial comparison because `capture` overwrites `tmp/regression/baseline.json`.

Acceptance:

- no unit or integration failures;
- gold validation unchanged;
- the initial harness check identifies only explicitly reviewed corrections for sources containing the verified aliases;
- the baseline is refreshed only after those corrections are approved, and the final harness check reports zero drift;
- no new contiguous or flattened idempotency regression;
- no billing drift on PDFs without crew tally aliases;
- no meaningful runtime increase. Target parser overhead: less than 5%, expected near zero.

## 6. Go-live plan

### 6.1 Development

1. Start from current `origin/main`, not a stale local branch.
2. Create one focused branch and one focused PR.
3. Implement title classification first, parser warning second, tests third.
4. Do not mix cable-footage, UI, database, retention, or unrelated refactors into the PR.
5. Keep customer PDFs and generated outputs ignored and out of Git history.

Suggested commit message:

```text
Exclude legacy crew totals boxes from billing evidence
```

### 6.2 Pre-merge evidence

Attach to the PR:

- exact before/after NR-702749 totals;
- title classification matrix;
- full suite result;
- gold/harness result;
- rendered page comparison;
- confirmation that verified legacy summaries are replaced, protected boxes survive, and canonical tool boxes remain idempotent.

### 6.3 Production deployment

1. Merge to `main` only after all local gates pass.
2. Confirm Railway deploys the merged commit successfully.
3. Confirm `/health` and deployment logs show no startup or migration errors.
4. Upload the exact NR-702749 input through production.
5. Verify response status is Review, not Done - Notes.
6. Verify production evidence excludes the four legacy boxes and shows field-only contributions.
7. Download and render the production output.
8. Confirm each legacy box was replaced by one movable canonical box and protected boxes remain untouched.
9. Re-upload the production output once and confirm idempotency.

No environment-variable or database migration is required. Rollback is a normal revert of the focused commit.

### 6.4 Monitoring

For the next seven days, inspect run-history issues for:

- `legacy crew totals ignored`;
- `unclassified total-like annotation`;
- unexpected loss of codes that appear only in crew summaries;
- duplicate canonical output boxes;
- any new parser exception.

If an unknown title appears, collect the exact heading and a sanitized fixture. Add it as an exact alias only after confirming it is a summary. Do not broaden the regex reactively.

## 7. Six-month robustness review

| Future risk | Protection in this design |
|---|---|
| A new crew spelling reopens double counting | Unknown total-like annotations deterministically force Review; no green silent failure. |
| A broad regex hides a legitimate field callout | Only anchored, evidence-backed aliases are automatically excluded. |
| Parser and annotator drift apart | One classifier module; counting and replacement are explicit views of the same classification. |
| Protected customer summaries are accidentally deleted | Only exact verified Job/Page aliases are replaceable; `Previously Billed`, New Totals/Additions, Materials, and unknown formats have explicit negative tests. |
| Flattened PDFs leak summary rows | Existing bounded spatial-remnant logic is reused and tested with the new aliases. |
| Genuine matching callouts are suppressed | Annotation-line dedup and cross-column regression tests preserve independent field evidence. |
| Large PDFs slow down or crash | Precompiled regexes and one bounded block pass; no OCR, rendering, network, or LLM work. |
| A future developer loosens matching | Positive and negative title matrices, exact incident fixture, and idempotency tests pin the contract. |
| Field-only totals conflict with crew summaries | Deterministic Review warning; never claim the cleaned result is billing-correct without human confirmation. |
| Rollout introduces unrelated regressions | One focused PR, no schema/config/UI changes, full corpus and gold gates, simple revert. |

## 8. Recommended decision

Ship the verified alias exclusion, legacy Job/Page replacement, protected-box carve-outs, and deterministic Review safeguard together in one focused PR. Replace only canonical or exact verified legacy Job/Page summaries. Preserve `Previously Billed`, New Totals/Additions, Materials, and unknown formats. Do not ship a generic sum-echo auto-exclusion yet.

This is the smallest change that fixes both the arithmetic and visible duplicate-box symptoms without weakening field-evidence counting or creating a new heuristic that could silently undercount a future project.
