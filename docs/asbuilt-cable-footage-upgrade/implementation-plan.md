# Cable Footage Counting — Implementation & Deployment Plan

**Companion to:** [`upgrade-brief.md`](./upgrade-brief.md) · snapshots in [`./snapshots/`](./snapshots) ([contact sheet](./snapshots/_contact_sheet.png))
**Scope:** add per-type cable (fiber + coax) footage counting to the as-built summation app, with an auditable breakdown in the web UI and a separate PDF box labeled `Materials`.
**Status:** Phase 0-1 implementation exists in the working tree behind feature flags. Solo dev, push-to-`main` = Railway auto-deploy after local verification and an intentional commit.

This plan extends the brief in five places the brief under-specifies: (1) path footage should be derived first from a **single configured cable-bearing composite code** such as `Comp-15`, not from vision/geometry or a summed code set, (2) the cable callouts **currently force manual review** and must be re-scoped, (3) the part map has **alternate part numbers** needing canonical selection, (4) the **storage bucket** must be defined exactly, and (5) Nick confirmed the cable material output belongs in a separate `Materials` box, bottom-left preferred. Phasing is built around a robust role-classification layer over the existing de-duplicated billing callouts, with geometry/vision only as a later fallback.

---

## 1. Objective & success criteria

Produce, per cable type on an as-built, a material line (e.g. `605-3277 (48Ct) - 2800'`) plus a step-by-step calculation visible in the web app, without regressing MKR billing totals.

**Done =**
- Fiber: `ceil_100((Σ path + Σ storage) × 1.10)` per type.
- Coax: `ceil_by_coax_rule(Σ pulled path × 1.10)` per type, **no storage**. Nick confirmed this rule; keep the rounding increment/config explicit because the current sample text evidence still does not fully explain the `140'` output.
- One line **per cable type**; multiple types and multiple pages handled as the norm, not edge cases.
- Per-type breakdown (path list, storage list, subtotal, buffer, rounding, total, source pages, confidence) shown in the result card and persisted in run history.
- Rollout mode derives and displays cable footage first; stamping cable material lines is gated by a **global** `AUTO_STAMP_CABLE_FOOTAGE` flag until a validation batch supports hands-off stamping.
- PDF output uses a separate `Materials` FreeText annotation. Preferred placement is bottom-left, using the same obstacle-avoidance / replacement behavior as the MKR totals box, but with Material-box styling: light green fill, blue outline, black text.
- Never emits a silently-low total: if any required input is missing/ambiguous, it flags for review instead of guessing.
- Billing-code totals, placement, and re-run idempotency are byte-for-byte unchanged when the feature flag is off.

**June 18, 2026 field-feedback artifacts now required for validation:**
- `BI-913037` production input/output pair: validates Nick's newest Materials-box merge request. The input already has a Materials box with manual rows (`Lg Ped`, `VP`, `EMT`, PVC, Mule, Tape, etc.); the current production output incorrectly replaces it with only `605-3277 (48Ct) - 1200'`.
- `BI-883032` latest production input/output pair: validates re-run behavior where the input already has a cable row plus manual material rows; the output must keep the manual rows and avoid duplicating the cable row.
- `BI-942102` production input/output pair: stays as the known cable baseline and must continue producing `605-3277 (48Ct) - 1700'`.

Local artifact folder:

`/Users/javiervillaguardado/Documents/Claude/Telcyte_asbuilt-summation_fix_10 Jun/telcyte-asbuilt-summation/downloaded/latest-nick-materials-feedback`

---

## 2. Non-negotiable invariants (carried from `CLAUDE.md`)

- **Parser/deterministic logic is source of truth.** The LLM only reviews; it never invents cable totals (mirror `ALLOW_LLM_INFERRED_TOTALS=false`).
- **Exclusions are never silent** — every skipped/not-counted/pull-through segment surfaces as a note or warning.
- **`log_run` must never raise**; cable persistence failures cannot block PDF delivery.
- **Re-run safety** — a stamped Material box must not be re-counted on re-upload (extend the existing `MKR Job Totals` box-exclusion to material lines).
- **Flag-gated rollout**, default off, until Nick validates.
- **Cable totals are rate-card-whitelist independent**; loading a billing rate card must not remove or change cable-footage evidence.
- The totals box **never drops lines**; cable lines obey the same fit-or-shrink rule.

---

## 3. Architecture & where it fits

Add a **deterministic cable aggregator parallel to `derive_code_totals`**, feeding the existing (currently dormant) materials channel plus a new structured breakdown.

| Layer | File | Change |
|---|---|---|
| Cable logic (new) | `app/cable_footage.py` | `derive_cable_footage(blocks, …) -> CableFootageResult`: role classification, type detection, bucketing, formula, mapping, confidence/stamp-eligibility state, review flags. |
| Type catalog + part map | `app/rate_cards.py` (or `app/cable_catalog.py`) | callout→{primary part, alternates, family, formula-class}; canonical via pick-ticket highlight (reuse `_has_highlight_fill`). |
| Callout extraction | `app/pdf_parser.py` | factor the existing de-dup/stamped-box exclusion into a shared helper; recognize `(<Storage\|Tie Point\|EOL\|Splice>) - <type> - <ft>`, bare type designators, and the configured cable-bearing composite code (`Comp-15` by default); **re-scope `UNRESOLVED_CALLOUT_PATTERN`** so resolved cable callouts no longer force 422. |
| Data model | `app/models.py` | add `cable_footage: list[CableFootageLine]`; derive separate `materials` lines from it; keep MKR totals and Materials output renderable independently. |
| Config | `app/config.py` | `INCLUDE_CABLE_FOOTAGE` (new, default false), `AUTO_STAMP_CABLE_FOOTAGE` (default false), and explicit per-family rounding rules. |
| Orchestration | `app/openrouter_client.py` | run aggregator; treat cable like materials in `_merge_parser_and_model` (parser-backed only); optional vision via existing `INCLUDE_PAGE_IMAGES` only as a fallback/cross-check. |
| API payload | `app/main.py` | add `cable_footage` to `_result_summary_payload`; flows through `X-Telcyte-Result-Summary`. |
| Web UI | `static/app.js` | new `appendCableBreakdown()` after `appendResultSummary()` (line ~336); the `materials` count row (line ~351) already exists. |
| Persistence | `app/run_history.py` | add `cable_footage_json` column via `_PG_MIGRATIONS`/`_SQLITE_MIGRATIONS` lists (same pattern as `result_lines_json`). |
| Annotator | `app/pdf_annotator.py` | add a separate `Materials` FreeText annotation path. It should replace prior `Materials` boxes on re-run, prefer bottom-left while avoiding callouts, stay movable/editable like MKR totals, and support rotated pages with the same known-good annotation structure. Style it like the sample Material box: light green fill, blue border, black text. Do **not** simply append cable material lines into the MKR Job Totals box or reuse the red MKR text style. |

---

## 4. Decisions and Nick-confirmed rules

Nick confirmed by email on June 16, 2026: the fiber and coax formulas below are correct, and the final coax material line should include the part number (`220-9236 (.625) - 140'`). He also clarified that the material output belongs in a box labeled `Materials`, with bottom-left preferred as long as it does not block callouts.

| # | Question | Build default (robust, reversible) |
|---|---|---|
| D1 | Does coax get buffer/rounding? | **Confirmed:** path footage only, no storage, add 10%, then round by the normal coax rule. Implement as a per-family rule object with explicit rounding config; do not infer missing path footage from the formula. |
| D2 | Which labeled callouts feed **storage/slack**? (`730'` = 600 storage + 100 tie point + 30 EOL in the fiber sample) | Storage/slack bucket = `Storage-` + `EOL-` + `Tie Point-` footage when type and footage are explicit. Each item is shown in the breakdown. |
| D3 | Which single composite code carries cable path footage for a given family/design? | Default fiber path source = the de-duplicated `Comp-15` aggregate. `UG-56`/`UG-57` are pulled-through inspection cues, not addends; unknown or competing path codes → Review/manual follow-up. |
| D4 | Coax line format: `220-9236 (.625) - 140'` or raw `.625 - 140'`? | **Confirmed:** use part number + type + footage, e.g. `220-9236 (.625) - 140'`. |
| D5 | Does the Tie Point-to-EOL jacket sequence apply beyond 48Ct? | **Confirmed by Nick on 2026-07-16:** apply it to 48Ct, 144Ct, 288Ct, `.625`, `.875`, Drop F, RG6 (`240-2079`), and RG11 (`240-2083`). Keep each family's existing buffer/storage/rounding formula and retain Review on incomplete, conflicting, or multi-cable evidence. |
| D6 | When does the app stamp derived cable lines? | Current pipeline is one-shot, so use a **global** gate: `AUTO_STAMP_CABLE_FOOTAGE=false` means derive/show/persist only; `true` allows high-confidence lines to stamp. A per-run click-to-stamp flow is a separate product feature. |
| D7 | Where does the PDF output go? | Separate box titled `Materials`, bottom-left preferred, same placement safety/replacement/movable annotation rules as MKR totals. Visual style should match Nick's sample Material box: light green fill, blue outline, black text. |

The remaining uncertainty is not the formula; it is whether the parser can fully reconstruct the source path evidence on every coax sheet. The sample still shows visible text path subtotal `118'` while Nick's material output is `140'`, so coax should stay review-safe until the path source is reconciled.

---

## 5. Phased delivery

Phasing uses the validated source of truth first: the existing de-duplicated billing aggregate for the selected composite code. In the fiber sample, `Comp-15 - 290' + 270' + 336' + 124' + 200' + 552' = 1772'`, exactly matching Nick's path footage. Do **not** sum `{Comp-15, UG-56, UG-57}` as a set: `UG-56` adds `358'` on the fiber sample and would overcount to `2130'`. Geometry/vision is deferred to fallback cases where the configured composite code is missing or under-determined.

### Phase 0 — Scaffolding (dark, zero behavior change)
**Goal:** pure, unit-tested building blocks behind the off flag.
- Add `INCLUDE_CABLE_FOOTAGE` (default false), `AUTO_STAMP_CABLE_FOOTAGE` (default false), and the per-family rule object (buffer, rounding, storage-applicable). Fiber = storage yes, 10%, `ceil_100`; coax = storage no, 10%, explicit coax rounding.
- Build the type catalog + part map (data-driven, including alternates; canonical = highlighted pick-ticket row; note the `220-6999 (825)`→`.875` source typo — key on the **callout string**, never the parenthetical).
- Define the `CableFootageLine`/`CableFootageResult` data model (§6).
- Pure functions: `normalize_cable_type` (`48Ct`/`048ct`→`48ct`; `.625`/`625`→`.625` **only with the dot or cable context**, never `PWR-625`), `apply_formula`, `map_part_number`.
- Factor shared callout aggregation so cable can use the same de-dup/stamped-box exclusion as billing **without** inheriting the rate-card whitelist.
- **Acceptance:** `pytest tests/test_cable_footage.py` green; nothing in the request path changes; flag off = identical output.

### Phase 1 — Automatic composite-code cable derivation
**Goal:** auto-derive fiber material math from text evidence while keeping PDF stamping globally gated and ambiguous coax/multi-type cases review-safe.
- Parse labeled storage/slack callouts (`Storage/Tie Point/EOL - <type> - <ft>`) and bare type designators → per-type storage subtotal, types present, source pages.
- Read path footage from the whitelist-independent de-duplicated aggregate for the configured cable-bearing composite code (`Comp-15` by default). Do not re-parse raw cells, do not sum roll-up boxes with their component boxes, and do not depend on `RATE_CARD_CODES` / `RATE_CARD_PATHS`.
- Treat `UG-56`/`UG-57` as pulled-through inspection cues that can support or exclude a segment, not as footage addends.
- Attribute the composite-code footage to a cable type only when safe: single cable type on the parsed scope, or clear nearby same-type evidence. Multi-type or unclear ownership → Review, not a guessed material line.
- Filter out prior output/instructional content: existing `MKR Job Totals`, old `Material` boxes, yellow formula/mapping examples, and any text inside their rectangles must never feed cable totals.
- Re-scope `UNRESOLVED_CALLOUT_PATTERN`: `Storage/Tie Point/EOL/Pull through` stop forcing 422 once recognized as cable inputs; they force review only when type/footage is ambiguous.
- UI shows the derived cable line and the audit breakdown. If `AUTO_STAMP_CABLE_FOOTAGE=false`, do not stamp cable lines into the PDF at all; if true, stamp high-confidence lines automatically into the separate `Materials` box.
- Acceptance: on `FIBER-…-888071`, derive path `1772'`, storage/slack `730'`, and material `605-3277 (48Ct) - 2800'`; assert that adding `UG-56` would incorrectly overcount and is not part of the path total; assert the same cable result with and without a restrictive rate-card catalog; billing totals unchanged with the flag off/on.

### Phase 2 — Under-determined cases and fallback evidence
**Goal:** handle coax and future sheets that do not fully reconcile from coded text boxes.
- Coax: detect `.625`/`.875`, read the de-duplicated configured composite-code aggregate, apply explicit pull-through exclusions, then apply Nick-confirmed no-storage + 10% + coax-rounding formula only when the source path is complete. Show Review/manual follow-up when the text subtotal does not reconcile to the expected material line (sample: visible `Comp-15` path text gives `118'`, while Nick-confirmed output is `220-9236 (.625) - 140'`).
- Multi-type sheets: use position/proximity only to disambiguate composite-code ownership when confidence is high; otherwise emit per-type evidence and require review.
- Geometry/vision fallback: only after coded text evidence is insufficient. Use `page.get_drawings()` or `INCLUDE_PAGE_IMAGES` as cross-checks, not the primary path, and cap work so it cannot slow normal projects.
- Acceptance: ambiguous coax and multi-type cases degrade to Review with a useful breakdown; they do not crash, do not emit silently-low totals, and do not alter MKR billing totals.

### Phase 3 — Harden & enable
- Confidence scoring + review badges; re-run safety for stamped Material lines; multi-page/multi-type stress; performance caps on vector/vision; update `README`/`CLAUDE.md`.
- Flip `INCLUDE_CABLE_FOOTAGE=true` first for derive/show mode. Flip `AUTO_STAMP_CABLE_FOOTAGE=true` only after Nick signs off on a validation batch and at least one independent guardrail exists (for example segment-count agreement, validated-run history, or later geometry/vision cross-check). When stamping is enabled, material lines go into the separate bottom-left-preferred `Materials` box.

---

## 6. Data model & contract

```text
CableFootageLine:
  callout: "48ct" | ".625" | ...
  part_number: "605-3277" | "" (flagged if unmapped)
  family: "fiber" | "coax"
  path_segments:   [{composite_code, source, page, ft, attribution}], path_subtotal
  storage_items:   [{label, page, ft}], storage_subtotal   # empty for coax
  buffer:   1.10
  rounding: "ceil_100" | "coax_rule"
  total_ft: int | null      # null => not emitted (review)
  eligible_for_stamp: bool  # true only when confidence is high and AUTO_STAMP_CABLE_FOOTAGE allows it
  source_pages: [int]
  confidence: float
  review_flags: [str]       # e.g. "ambiguous path attribution", "unmapped part", "coax path evidence incomplete"

SummaryResult (+):
  cable_footage: list[CableFootageLine]
  # materials box lines derived from high-confidence lines whose total_ft is not null
```

`_result_summary_payload` adds `cable_footage`; `app.js` renders one expandable block per line (type/part, path subtotal, storage subtotal, buffer, rounding, total, source pages, flags). This block is the audit path Nick and Javier asked for. The PDF stamp is separate and only appears when `AUTO_STAMP_CABLE_FOOTAGE=true`.

---

## 7. Testing & verification

- **Unit (CI-safe, synthetic):** `tests/test_cable_footage.py` — type normalization, storage/slack bucketing, configured composite-code bucketing, fiber formula incl. **round-up boundaries** (`2752→2800`, `2801→2900`, never down), coax no-storage + 10% + configured-rounding formula, coax review behavior when path evidence is incomplete, global stamp gating, part mapping incl. **alternates** (288ct→605-1503 not 605-0035) and unmapped→flag.
- **Parser:** extend `tests/test_pdf_parser.py` — labeled callout extraction; configured composite code (`Comp-15`) surfaced as path evidence; `UG-56`/`UG-57` treated as inspection/exclusion cues, not addends; `UNRESOLVED_CALLOUT_PATTERN` no longer trips on resolved cable callouts; pull-through exclusion noted.
- **Config/regression:** `tests/test_config.py` flag default off; `tests/test_generic_workflow.py` proves billing totals + placement unchanged with flag **off and on** but no cable present.
- **Golden (local, real PDFs):** validate against the two sample PDFs in `/Users/javiervillaguardado/Downloads/As built uprades`; assert fiber path `1772'`, storage/slack `730'`, and final `2800'`; assert coax is detected and mapped to `220-9236 (.625)`, but marked Review/manual follow-up until its `118'` text subtotal vs `220-9236 (.625) - 140'` material output is resolved. Keep these separate from CI-safe units unless sanitized fixtures are approved for the repo.
- **Overcount guards:** assert `{Comp-15 + UG-56 + UG-57}` is **not** the fiber path rule; it would produce `2130'` on the sample. Assert coax raw `Comp-15` cells de-dupe to `118'`, not `236'` from roll-up plus components.
- **Billing-coupling contract:** assert cable path uses the same de-dup/box-exclusion mechanics as billing but is invariant to `RATE_CARD_CODES` / `RATE_CARD_PATHS`; MKR Job Totals output remains identical with the cable flag off/on.
- **Stamping gate:** assert derived cable lines appear in the result payload/run history but are not stamped into the PDF while `AUTO_STAMP_CABLE_FOOTAGE=false`; when true, high-confidence lines stamp in a separate `Materials` FreeText annotation.
- **Materials placement/style/re-run:** assert the `Materials` box is bottom-left preferred, avoids callouts, uses light green fill + blue border + black text, remains movable/editable, replaces an old `Materials` box on re-run, and does not get counted as input evidence on the next upload.
- **Nick edge cases:** append to `tests/test_nick_jun9_feedback.py` — multi-type one sheet; same type across pages; fiber storage vs path separation; coax no-storage; ignored pull-through; missing mapping → review; UI payload contains the breakdown; run history round-trips it.
- **Independent check:** a verification subagent re-derives every golden total from the formula and diffs against parser output; visually confirm crops via the contact sheet.
- **Gate:** full suite green locally before every push (no CI).

---

## 8. Deployment & rollout (Railway)

1. Merge Phase 0–1 with `INCLUDE_CABLE_FOOTAGE` **off** and `AUTO_STAMP_CABLE_FOOTAGE` **off** → push `main` → auto-deploy **dark**. `/health` still green.
2. **DB migration:** run `ALTER TABLE asbuilt_run_history ADD COLUMN ... cable_footage_json` in prod Postgres manually (no migration runner; `CREATE TABLE IF NOT EXISTS` won't add it) and keep the idempotent line in `_PG_MIGRATIONS`/`_SQLITE_MIGRATIONS`.
3. Enable `INCLUDE_CABLE_FOOTAGE` in Railway for a **validation window**; keep `AUTO_STAMP_CABLE_FOOTAGE=false`; run the two samples + a few real as-builts; confirm via run history that billing totals/placement are unchanged and cable derivations look right.
4. Hand to Nick for sign-off on path reconstruction/placement. Formula and output format are already confirmed. **Rollback = flip `INCLUDE_CABLE_FOOTAGE` off** (no redeploy).
5. Enable `AUTO_STAMP_CABLE_FOOTAGE=true` only after validation. Rollback = flip it off.
6. Promote Phase 2 the same way (dark → validate → enable), keeping geometry/vision capped and fallback-only since it adds latency and failure modes.

---

## 9. Risks & mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Composite-code rule misconfigured | Wrong-low or wrong-high cable totals | Use one configured composite code per family/design, itemize every included segment, and add sample guards against set-sum overcount; mismatches degrade to Review. |
| Single-signal fiber auto-stamp is wrong | Confidently wrong material line | Roll out in derive/show mode; require validation batch plus independent guardrail before hands-off auto-stamp. |
| Rate-card whitelist changes | Cable evidence disappears or shifts when billing catalog changes | Cable composite totals use whitelist-independent de-dup/box-exclusion helper; test invariance to `RATE_CARD_CODES` / `RATE_CARD_PATHS`. |
| OCR ambiguity (`.625`/`625`, `(825)` typo, `48Ct`/`048ct`) | Mis-typed/mis-mapped cable | Require the dot/cable context for coax sizes; canonical part via highlight; normalize counts; flag on ambiguity. |
| Multi-type path attribution | Footage assigned to wrong type | Auto-attribute only with single-type or high-confidence local evidence; below-threshold → Review; itemized breakdown for human catch. |
| Polluting MKR billing totals | Core feature regression | Cable is a **separate aggregator + field**; reuse `NON_BILLING_PREFIXES`; regression tests with flag off/on. |
| Geometry/vision fallback latency | Slow or timed-out runs | Coded text path is primary; geometry/vision capped, optional, and never required for normal fiber path. |
| Coax path evidence incomplete | Coax totals off despite confirmed formula | Formula is confirmed, but text subtotal mismatch becomes Review/manual follow-up until the pulled path source is reconciled. |
| Materials box blocks callouts, merges into MKR box, or uses wrong styling | Output is hard to use or visually inconsistent | Separate bottom-left-preferred `Materials` placement with obstacle scoring, sample-matched styling, re-run replacement, and input-exclusion tests. |
| Re-run double counting | Inflated cable totals | Extend stamped-box exclusion to Material lines; idempotency test. |

---

## 10. Sequencing

```
Phase 0 (scaffold+catalog+model+flag)  →  Phase 1 (derive composite-code path + storage/slack + derive/show UI)
   → ship dark, validate, ALTER TABLE  →  Phase 2 (coax/multi-type fallback + optional geometry/vision)
   → validate                          →  Phase 3 (confidence/badges/harden) → enable for Nick
```

Phase 1 is the value inflection point: fiber can be prefilled from the whitelist-independent `Comp-15` aggregate while keeping PDF stamping globally gated and ambiguous cases in Review. Phase 2 expands coverage for under-determined coax and multi-type sheets. Each phase is independently shippable behind the flag and independently reversible.
