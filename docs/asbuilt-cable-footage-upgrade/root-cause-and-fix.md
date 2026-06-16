# Cable Footage — Validated Root Cause & Robust Fix

*Companion to [`upgrade-brief.md`](./upgrade-brief.md) and [`implementation-plan.md`](./implementation-plan.md). This doc corrects a load-bearing assumption shared by both plans, with evidence.*

## TL;DR

Both the competing plan and my own implementation plan assumed the hexagon/path footage needs geometry or LLM-vision and routed it to manual entry / Review. **I tested that against the two sample PDFs and it is false for fiber.** Nick's exact numbers reconstruct from the *text layer alone*. The real bottleneck is not reading a number off the drawing — it is knowing each number's **role**. That changes the fix: build a read-only role/reconciliation layer over the callouts the billing parser already extracts, and never touch billing.

## 1. Validation — what the evidence actually shows

Method: `pdftotext -layout` token reconstruction + `pdftocairo -svg` vector probe on the two sample as-builts.

| Quantity | Nick's sheet | Reconstructed from the TEXT layer | Result |
|---|---|---|---|
| Fiber storage | `730'` | 6×`Storage-48Ct-100'` + `Tie Point-48Ct-100'` + `EOL-48Ct-30'` | **730 — exact** |
| Fiber path ("hexagons") | `1772'` | Σ `Comp-15 - N'` = 290+270+336+124+552+200 | **1772 — exact** |
| Fiber total | `2800'` | (1772+730) × 1.10 = 2752 → round-up-100 | **2800 — exact** |
| Coax material (`.625`) | `140'` final line | visible Σ path-codes in text (`Comp-15` 34+84) = 118 before buffer/rounding | **source path under-determined** |
| Hexagons as vectors | — | SVG = 4745 `<path>`, 0 `<polygon>`, 0 clean 6–8-node shapes | **not detectable as shapes** |

Two independent confirmations that path footage lives in **path-code callouts**, not only in hexagon glyphs:

1. Fiber `Σ Comp-15 = 1772'` exactly — and the app's existing `derive_code_totals` already aggregates `Comp-15` (it is a billed composite code today).
2. The coax sheet's own legend literally names **`Comp-15` / `UG-56` / `UG-57`** as *"callout codes to look for to determine if cable was pulled through a path."*

The coax exception is the tell: its visible in-text path-codes sum to `118'` before buffer/rounding, which does not explain the stated `140'` material output. Nick later confirmed the coax formula, so the remaining coax problem is **not the math rule**; it is that the visible text path evidence does not yet fully explain the source footage that leads to `140'`. Fiber and coax are asymmetric — they were treated symmetrically by both plans.

## 2. Candid assessment

**Competing plan**
- Its load-bearing assumption — *"geometry/vector detection for bare hexagon numbers is not required for safe v1; ambiguous bare path numbers must trigger Review"* — means **fiber as-builts mostly land in Review**, so it saves Nick little on the primary case. The premise is wrong: fiber path footage is not an unattributable bare number, it is the `Comp-15` path-code total.
- It explicitly downgrades `Comp-15/UG-56/UG-57` to *"evidence signals, not standalone footage sources,"* discarding the exact signal that reconstructs `1772'`.
- It never addresses that `Comp-15` is simultaneously a **billed** line and the **cable length** — the actual integration hazard.
- Credit where due: structured per-type results, flag-gated rollout, no-silent-guessing, run-history migrations, and keeping coax configurable are all sound and match best practice.

**My earlier implementation plan (same author as this doc) — what I missed**
- I asserted "hexagon = vision problem" **without validating it**. Wrong for fiber. My Phase-2 vector pass is both unnecessary (fiber) and not feasible as implied (the SVG has no clean hexagon polygons among 4745 paths).
- I missed that path footage **equals the existing `Comp-15` billing callouts** — the reuse that makes fiber nearly free.
- I missed the billing/cable **double-encoding** as the true "don't-regress-other-projects" risk.
- I under-weighted the fiber/coax asymmetry.
- I was directionally right on structure (flag, parser-first, never-silent, breakdown-in-UI) but wrong on the central technical claim — which is the one that drives effort and reliability.

## 3. Validated root cause

Not *"the number is unreadable."* The number is in the text. The root cause is **role ambiguity from redundant encoding**: each path footage can appear in up to three roles at once —

1. a **hexagon glyph** (visual path distance),
2. a **billed composite** callout (`Comp-15 - N'`), and
3. the **cable length** for a given type,

— and may additionally be flagged *"not pulled through this path"* (exclude). The parser already has the numbers; what it lacks is the **rule-set that assigns roles**: which codes count as cable-path, which segments are excluded, and which cable type owns them. Fiber encodes those roles cleanly in text; coax does not fully. Vision was a misdiagnosis of a reconciliation problem.

## 4. The fix — robust, efficient, non-regressive, future-proof

A **read-only cable-derivation layer** that sits on top of the callouts the billing parser already produces.

1. **Reuse extraction, not billing policy.** Consume the existing `TextBlocks` and the same de-dup/stamped-box-exclusion mechanics as billing, but compute cable composite totals from a whitelist-independent view. No second PDF pass, no vision dependency, no new extraction failure surface, and no dependency on `RATE_CARD_PATHS`. *(efficiency + future-proofing)*
2. **Role rules, config-driven.** storage = `Storage|Tie Point|EOL - <type> - <ft>`; path = the de-duplicated footage on a single configured **cable-bearing composite code** (default fiber `Comp-15`); `UG-56`/`UG-57` are pulled-through inspection cues, not addends; excluded = segments marked *"not pulled through this path."* New drawing conventions become config, not code. *(future-proof)*
3. **Never mutate billing.** `Comp-15` and friends stay in MKR Job Totals byte-for-byte; the cable layer only reads their de-duplicated footage to derive cable length. Regression tests assert MKR totals are identical with the cable flag **on and off**, and cable totals are invariant to billing rate-card filters. *(no regression to other projects)*
4. **Confidence gating + safe degrade.** Single cable type + path-codes present + buffer/rounding can prefill fiber (`2800'`), but rollout keeps PDF stamping behind the global `AUTO_STAMP_CABLE_FOOTAGE` flag until a validation batch and independent guardrail justify hands-off stamping. Under-determined path evidence (coax short, multi-type attribution ambiguity, pull-through ambiguity) → emit a breakdown and a Review badge — **never a silently-low number.**
5. **Isolated failure domain.** Wrap the whole derivation in a guard that returns *"no cable result + note"* on any exception; the billing pipeline continues untouched (mirrors the existing invariant that a reviewer failure never sinks a run). A novel as-built six months out that breaks cable logic degrades to **billing-only + Review — it cannot crash the main path.** *(no future crashes)*
6. **Structured + versioned output** persisted to run history (`cable_footage_json`); old rows default to empty.

## 5. Why this is durable at the 6-month horizon

- **Other projects:** cable logic is additive and read-only; billing extraction, placement, rate-card filtering, and re-run idempotency are untouched; flags default off; tests lock MKR output and cable invariance to rate-card filters.
- **Future as-builts:** role rules and the cable-bearing composite code are config; per-family formulas are explicit (fiber: path+storage+10%+ceil100; coax: path-only+10%+coax rounding); anything ambiguous becomes Review, not a guess; any parsing failure is caught, logged, and non-fatal.
- **Efficiency:** the fiber case is pure arithmetic on already-extracted callouts — no vision, no second PDF parse. Coax and rollout safety add a review-safe breakdown instead of an unreliable computer-vision pipeline.

## 6. What this changes in the implementation plan

- **Drop** "Phase 2 = vector / LLM-vision for hexagons" as the primary path. **Replace** with "path footage = the configured cable-bearing composite code, reusing the billing extraction."
- Keep vision only as an **optional later cross-check** for coax / under-determined sheets.
- **Phase 1 now pre-fills fiber totals** for single-type sheets (not manual math); PDF stamping is globally gated during rollout; coax stays Review when path evidence is incomplete. This is what makes v1 save Nick time on the primary (fiber) case while staying safe.
- The one genuinely hard, still-open item is **coax source-path reconciliation** (visible text gives `118'` before buffer/rounding, while the final material output is `140'`) and **multi-type attribution** — both correctly routed to human-confirm, not guessed.

---

## 7. Round 2 — the coded-box approach has its own validated failure mode

After the plan was updated to *"coded text boxes (`Comp-15`, `UG-56`, `UG-57`) are the primary path-footage source,"* I stress-tested that too. It overfits one sample.

**Validation (both sheets, `pdftotext` token sums):**

| Approach | Fiber path (target 1772) | Coax visible path subtotal (final output 140) |
|---|---|---|
| `Comp-15` only | **1772 ✓** | 118 (does not explain final 140) |
| Default **set** `{Comp-15, UG-56, UG-57}` | **2130 ✗** (+358 from UG-56) | 118 |
| Naive sum of all `Comp-15` cells (no box-exclusion) | 1772 | **236 ✗** (roll-up 118 + 34 + 84) |

Three distinct, validated failure modes:

1. **The default path-code SET overcounts.** `UG-56` contributes `358'` on the fiber sheet → 2130, not 1772 (and a final `3200'` instead of `2800'`). Nick's legend names `Comp-15/UG-56/UG-57` as *"codes to look for to determine **if** cable was pulled through a path"* — a per-segment yes/no **inspection cue**, not a set to sum. Summing the set is a category error.
2. **Roll-up / stamped-box double-count.** Coax `Comp-15` appears as a `118'` summary **and** its `34'+84'` components; a naive re-parse sums 236. The existing `derive_code_totals` already avoids this (it excludes stamped boxes — the re-run-safety logic), which is exactly why the cable layer must **reuse that de-duplicated aggregate, never re-parse raw cells.**
3. **n = 1 overfit.** Even done right (`Comp-15` only, de-duped) it matches **one** of two samples: fiber `1772 ✓`, while coax `118'` still does not explain the `140'` material output. "Coded boxes = path" is a fiber coincidence, not a law.

**Deeper validated root cause:** the plan is fitting a deterministic *"sum these codes"* rule to a single example. The real invariant is narrower and per-segment: cable path = the footage of the **one cable-bearing composite code** (here `Comp-15`), **de-duplicated against roll-ups, restricted to segments the target cable was actually pulled through.** Which code and which segments is drawing- and cable-specific (fiber: Comp-15 only; coax: partial + footage missing from text), so no fixed code-set sum can be correct. Promoting the coincidence to "primary source of truth" is the error.

**Corrected fix (small deltas to the plan):**

1. **Path = a single configured cable-bearing composite code per family** (default fiber `Comp-15`), **not** the legend set. Demote `UG-56/UG-57` to *pulled-through inspection flags that can EXCLUDE a segment, never ADD one.* → fixes the +358 fiber overcount.
2. **Reuse the de-duplicated `derive_code_totals` aggregate** for that code (it already drops stamped boxes/roll-ups) instead of re-parsing raw cells. → fixes the 236 coax double-count; free; no second parse.
3. **Cross-check, then degrade.** Auto-emit only when there is a single cable type and the composite total is unambiguous (and reconciles with any on-sheet total, if present). Coax / multi-type / mismatch → itemized evidence + Review/manual follow-up. Never sum a code set hoping it lands.
4. **Pin the coupling.** Cable path now reads a *billing* aggregate, so add a contract test that fails if a future billing-extraction change shifts the cable total — plus the existing "MKR byte-identical, flag on/off" test. → 6-month protection against silent drift.
5. **The worked-example box is an oracle, not an input.** It exists only on these marked samples; production sheets won't have it. Use it as a test fixture, never runtime logic.

Net: fiber still pre-fills the correct `2800'`, PDF stamping stays globally gated during rollout, the overcount/double-count are gone, a future billing change can't silently move cable totals, and coax stays safely review-gated when source path evidence is incomplete — with no vision and no second parse.

### Plan edits this implies (`implementation-plan.md`)
- **§5 Phase 1 / D3:** change "path-code **set** `{Comp-15, UG-56, UG-57}` … primary path-footage source" → "**single composite code** (default `Comp-15`); `UG-56/UG-57` are pulled-through **exclusion** cues, not addends."
- **§3 / §5:** specify path footage is read from the **existing de-duplicated `derive_code_totals` aggregate**, not a fresh raw-cell sum.
- **§7 Testing:** add the fiber overcount guard (set-sum ≠ 1772), the coax roll-up de-dup test, and the billing-coupling contract test.

---

## 8. Round 3 — two residual risks the composite-code fix does *not* cover

The plan has converged: single composite code, de-duplicated, billing read-only, degrade-to-review, flag-gated. That core is sound. But two second-order risks remain — one validated from the code, one from this repo's own roadmap.

### Risk A — single-signal auto-emit is unverifiable in production
The plan's safety net is *"auto-emit only when it reconciles, else Review."* For the fiber auto-emit case there is **exactly one** path signal — the `Comp-15` aggregate — and nothing independent to reconcile it against:
- The hexagon glyph numbers are the **same numbers** as the `Comp-15` callouts (validated: the bare path numbers `{290,270,336,124,552,200}` are identical to the `Comp-15` cells) — a redundant copy, not an independent check.
- The on-sheet worked-example `1772` is an oracle that exists **only on the marked samples**, not on production sheets.

So "reconcile-or-review" cannot fire on the one case that matters most: a future sheet where `Comp-15` is used for non-cable composite work would **auto-emit a confidently wrong number** with nothing to flag it. The reconciliation safety is partly illusory for a single signal.

**Fix:** during rollout, fiber path is **derive/show first**, not hands-off stamping — derive `1772`, show the breakdown, and keep cable lines out of the PDF while `AUTO_STAMP_CABLE_FOOTAGE=false`. That keeps essentially all the review-time savings while removing the silent-wrong-number stamp risk. Promote to fully hands-off PDF stamping only after (a) a validated batch and (b) a genuinely independent verifier exists — e.g. a geometry hexagon **count** cross-checked against the `Comp-15` segment count, or accumulated validated-run history.

### Risk B — reusing the billing aggregate couples cable footage to the rate-card whitelist (a pending change)
`derive_code_totals(blocks, code_catalog=catalog)` **drops any code not in the catalog**: `if catalog and normalized_key not in catalog: _record_catalog_miss(line); continue`. The catalog comes from `RATE_CARD_CODES` / `RATE_CARD_PATHS`, both empty today → no filtering → `Comp-15` counts and cable works.

But `CLAUDE.md` open item #4 is to **load `2026 MCA Rate Card-Amendment 3.xlsx` via `RATE_CARD_PATHS`.** The moment that lands, only whitelisted codes count. If `Comp-15` (a composite) isn't on that rate card, the cable path silently becomes empty → the feature **silently stops working for every sheet** (everything degrades to Review). If `Comp-15` is whitelisted but the card is later edited, cable totals **shift** with no cable-side change. An unrelated billing-config change breaks or moves cable footage — exactly the "affects other projects / 6-months-out" failure mode.

**Fix:** the cable layer must compute its composite-code total from a **whitelist-independent** view. Factor the de-dup + stamped-box-exclusion segment extraction into a shared helper that billing calls **with** the rate-card catalog and the cable layer calls **without** it (always counting the configured cable path code). Same blocks, tiny extra aggregation — efficient, and immune to rate-card config. Add a test: **cable footage is invariant to `RATE_CARD_PATHS`/`RATE_CARD_CODES`.**

### Honest convergence note
A and B are the last substantive risks I can find; both are contained (A = rollout staging, B = a decoupling + one test). I do **not** see a hidden showstopper beyond them — the remaining genuinely-open work is the known ones: coax source-path reconciliation (visible text gives `118'`, final output is `140'`) and multi-type attribution, both correctly routed to human-confirm.

### Plan edits this implies (`implementation-plan.md`)
- **§3 / §5:** the cable composite-code total is computed **whitelist-independent** (shared extraction helper; do **not** inherit the rate-card catalog filter).
- **§5 rollout:** fiber path is derive/show before hands-off PDF stamping; full auto only after a validated batch **and** an independent verifier.
- **§7 Testing:** add "cable total invariant to `RATE_CARD_PATHS`" and a global stamp-gate test.

---

## 9. Round 4 — converged; one real gap (the confirm flow), and the dominant risk is now data

The Round-3 updates (whitelist-independent totals, `AUTO_STAMP_CABLE_FOOTAGE=false`, derive/show rollout) are correct. Two honest points remain.

**Validated gap — "confirm before stamping" has no home in the current pipeline.** `/api/summarize` is the *only* write endpoint and is **stateless one-shot**: upload → `annotate_pdf` → finished PDF returned in the same response. There is no draft / confirm / re-stamp step anywhere in `main.py` or `app.js`. So "require reviewer confirmation before stamping" is not a flag — as written it conflates two different things:
- a **global** gate (`AUTO_STAMP=false` → never stamp cable lines), which is trivial; and
- a **per-run** "reviewer clicks accept, then we stamp," which needs a **new flow** (a derive endpoint that returns the cable result unstamped + a separate stamp endpoint + run state) — real work and new surface.

**Recommendation:** when `AUTO_STAMP=false`, cable lines never touch the PDF — they live only in the web breakdown + run history, and "confirmation" is the **global flag flip after a validated batch.** Build per-run accept-then-stamp only if Nick explicitly asks. (§5/D6 currently reads like the per-run version; the cheap, safe one is the global gate — say which.)

**The dominant remaining risk is epistemic, not design.** Every rule here — `Comp-15` = path, storage = `Storage/Tie Point/EOL`, coax has no clean text total — is fit to **one fiber + one coax** drawing. No further plan edits shrink that; only validating against a **batch of real as-builts** does. The `AUTO_STAMP=false` / UI-only mode is exactly the instrument to collect that ground truth safely: derive, show, log to run history, compare against Nick's hand calc, flip to hands-off only once a batch agrees.

**Convergence (candid).** Rounds 1–3 found substantive issues (vision misdiagnosis → text; set-overcount; whitelist coupling + single-signal). This round finds one concrete gap (the confirm flow); otherwise the plan is implementation-ready. I'm not going to manufacture a new "root cause" each pass — the honest next steps are (1) gather more sample as-builts and validate, (2) build Phase 0 behind the flags. The docs are sufficient to start.

---

## 10. Round 5 — Nick confirmed the formula and exposed one output-placement miss

Nick replied on June 16, 2026 and confirmed three product rules:

1. **Fiber formula:** path footage + storage footage, add 10%, round up to nearest 100.
2. **Coax formula:** path footage only, no storage, add 10%, then round by the normal coax rule.
3. **Final material line + placement/style:** show the part number and cable type, e.g. `220-9236 (.625) - 140'`, inside a separate PDF box labeled `Materials`; bottom-left is preferred as long as it does not block callouts; style should match the sample Material box: light green fill, blue outline, black text.

**What this corrects:** the plan should no longer treat coax buffer/rounding or line format as open questions. The correct remaining coax risk is **source-path reconciliation**: the sample's visible de-duplicated path text still explains `118'`, while Nick's expected material output is `140'`. The fix is not to hard-code `140'` or loosen the parser; it is to keep coax Review-gated until the parser can explain the pulled path source or Nick supplies the missing evidence/rule detail.

**What I missed:** I previously said `pdf_annotator.py` likely needed no change because `materials` already flow through `display_lines()`. That is now wrong. Today the app would put material lines inside the MKR Job Totals stamp. Nick wants a separate `Materials` box with bottom-left preference and the same movable/replacement behavior as the MKR totals box. That is a real implementation requirement and a new re-run/idempotency surface.

**Updated robust fix:**

1. Keep the read-only cable derivation layer and whitelist-independent path aggregation from Rounds 1–4.
2. Lock formulas by family: fiber = path + storage + 10% + ceil100; coax = path only + 10% + configured coax rounding.
3. Lock output format: `part_number (type) - footage'` for both fiber and coax.
4. Add a separate `Materials` annotation path: replace old Materials boxes on re-run, prefer bottom-left, avoid callouts, stay movable/editable, support rotated pages, match the sample styling (light green fill, blue outline, black text), and exclude old Materials boxes from future parsing.
5. Keep `AUTO_STAMP_CABLE_FOOTAGE=false` for rollout: show cable derivations in the web UI/run history first; stamp the separate Materials box only after validation.

This keeps performance stable (no default vision pass, no second PDF parse), protects other projects (feature flag off = identical MKR behavior), and protects the six-month future failure mode (rate-card changes and old material stamps cannot silently change cable totals).
