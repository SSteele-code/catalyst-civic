# Municipality Reader Spec

Status: Draft
Date: 2026-04-10
Project Root: `C:\Users\simon\CatalystCivic\_Modules\PDF Parser`

## Purpose

This machine is not a parser for one PDF. It is a generalized municipality reader for civic packets.

The winning unit is not "this packet." The winning unit is:

- civic document function
- page layout
- packet role
- evidence traceability

The system must prefer explicit uncertainty over false certainty. A quarantined unknown page is acceptable. A high-confidence wrong page is poison.

## Current Baseline

As of `RUN_2026_04_10_43EB`:

- page count: `212`
- suspicious page count: `21`
- wall-clock runtime: `169.83s`
- throughput: `74.9 pages/minute`
- seconds/page: `0.8`
- source-page coverage: `212 / 212`
- source-page linkage: complete
- document identity: `DOC_054599B02BE2CA51`

Live machine strengths:

- resident service model is stable
- run manifest is the truth source for run state and delivery
- per-page artifacts are source anchored
- the machine now exports `function_type`, `layout_type`, and `support_role`
- extractor routing already prefers `function x layout`
- context promotion now has evidence floors

Live machine weaknesses:

- upstream OCR normalization is still too narrow
- short structured municipal pages remain fragile
- table/form/finance cover pages still need a specialist lane
- some context carry still behaves like rescue rather than weak prior
- performance is dominated by OCR, so expensive retries must stay targeted

## Problem Statement

The remaining misses are not primarily "one more threshold" problems.

They come from four structural gaps:

1. Bad OCR normalization can poison downstream typing.
2. Structured civic pages are still forced through too few OCR/layout routes.
3. Context can still over-influence weak pages.
4. The final hard tail of tables/forms is being treated as generic OCR text instead of structured evidence.

## Success Definition

"97% passing" means:

- `100%` source page coverage
- `100%` source-page linkage
- `97%` of pages auto-pass with source-faithful text, type, and structure
- `0` high-confidence gibberish pages
- all remaining low-trust pages are explicitly provisional, quarantined, or review-required

The wrong target is "make every page classify somehow."

## Non-Goals

- Replacing the resident Windows service model
- Replacing the local stack with cloud OCR for every page
- Optimizing one packet at the cost of cross-packet generalization
- Treating packaging polish as a substitute for extraction truth

## Live Code Paths

Current implementation anchors:

- service: `src/state_machine/service.py`
- page feature pipeline: `src/state_machine/page_feature_pipeline.py`
- typer: `src/state_machine/page_typer.py`
- extractor routing: `src/state_machine/page_types.py`
- extractors: `src/state_machine/extractors.py`
- packaging: `src/state_machine/packager.py`

Current live facts:

- native text is checked in `page_feature_pipeline.py`
- OCR retry currently uses a small candidate set:
  - base
  - `--psm 6`
  - `rot90 --psm 6`
  - `rot270 --psm 6`
  - optional `--psm 11`
- internal axes are already exported:
  - `function_type`
  - `layout_type`
  - `support_role`
- extractor selection already prefers `function x layout`
- context evidence floors already exist, but still need tightening and broader use

## Target Architecture

The machine shall resolve pages in this order:

### Layer 1: Provenance

- source hash
- run manifest
- run-scoped page id
- document machine code
- page machine code
- source page anchor

### Layer 2: Physical Analysis

- native-text presence and quality
- fixed render DPI band
- cardinal orientation
- fine deskew
- scan/noise profile
- handwriting/noise estimate
- text and table region counts

### Layer 3: Layout Classification

- `blank`
- `outline`
- `prose`
- `table`
- `form`
- `mixed`
- `slide`

### Layer 4: Civic Function Classification

- `agenda`
- `minutes`
- `reference`
- `legislative`
- `contract`
- `finance`
- `admin`
- `separator`
- `unknown`

### Layer 5: Packet Role Classification

- `cover`
- `continuation`
- `attachment`
- `exhibit`
- `appendix`
- `signature`
- `standalone`

### Layer 6: Family-Specific Extraction

Extractor choice must be based on `layout_type x function_type`, not on one flat label alone.

### Layer 7: Context Smoothing

Context may rank candidates. Context may not rescue a page past hard evidence floors.

### Layer 8: Confidence and Abstention

The machine must be able to say:

- auto-pass
- provisional
- review-required
- quarantined

### Layer 9: Packaging

The package must export evidence, claims, provenance, and review state without hiding uncertainty.

## Required Changes

### 1. Normalization and Retry Routing

This is the highest-value next move.

Requirements:

- normalize OCR pages into a fixed DPI band before OCR
- perform cardinal orientation resolution before fine deskew
- keep fine deskew after orientation, not instead of orientation
- do not let one bad OCR pass be the only witness on suspicious pages
- route suspicious pages through a small retry grid

Default retry grid:

- rotations: `0`, `90`, `180`, `270`
- `--psm 6` for outline/prose candidates
- `--psm 11` or `--psm 12` for sparse, odd, or highly fragmented layouts

Retry routing rules:

- retry must be conditional, not global
- retry should trigger from low OCR yield, high skew, unstable route choice, or high suspicion
- retry winner must be selected from explicit evidence, not only raw alnum count
- retry output should capture which candidate won and why

Native-text routing rules:

- if native text is real, use native text first
- use reading-order aware extraction where it improves civic page coherence
- only enter OCR when native text is absent or weak
- if OCR is weak, enter retry lane
- if retry lane is weak, escalate to provisional or quarantine

### 2. Orthogonal Truth Axes

This machine must stop treating one flat page label as the only truth.

Requirements:

- `function_type`, `layout_type`, and `support_role` are first-class internal truth
- external `page_type` remains a compatibility label derived from internal truth
- internal decisions must not require `page_type` to be resolved first
- extractor dispatch must remain based on `function_type x layout_type`
- downstream packages must export both stable axes and compatibility labels

Current state:

- partially implemented

Remaining work:

- reduce legacy `page_type` influence inside classification passes
- reduce neighbor carry that backfills semantics too aggressively
- keep `generic_prose` and `table_or_mixed_layout` as layout-facing fallbacks, not semantic truth

### 3. Hard Evidence Gates Before Context

Context is a prior, not a pardon.

Requirements:

- finance cannot win without enough numeric density, finance lexical evidence, or table strength
- agenda cannot win without outline evidence, section markers, or strong continuation linkage
- admin/form cannot win without compact field structure, key-value evidence, or signature cues
- contract cannot win from one stray keyword
- neighbor agreement may raise confidence, but cannot replace evidence floors

Current state:

- evidence floors exist for several context paths

Remaining work:

- apply the same discipline across all context-carry branches
- use OCR quality and route stability as preconditions for context promotion
- treat low-quality OCR as weak evidence even when neighbors are clean

### 4. Specialist Fallback Lane for Tables and Forms

The tail pages are structured documents, not just bad OCR pages.

Requirements:

- keep the current local stack as the default lane
- add a specialist fallback lane only for the hardest pages
- specialist lane is triggered only when all of the following are materially true:
  - low OCR yield or unstable OCR retry result
  - `layout_type` is `table`, `form`, or `mixed`
  - suspicion is high or confidence is low
  - page sits in finance/form ambiguity or known structured-page families

Allowed specialist scope:

- table structure extraction
- form key-value extraction
- signatures or selection marks if available
- layout-aware recovery for civic cover sheets and finance attachments

Not allowed:

- replacing the whole pipeline with cloud OCR
- sending clean prose pages into the expensive lane by default

### 5. Confidence, Review, and Abstention

Confidence must become stricter, not louder.

A page may auto-pass only when:

- OCR or native-text quality clears a minimum floor
- route choice is stable enough
- `function_type`, `layout_type`, and `support_role` are not in serious contradiction
- context supports the page rather than rescuing it

Otherwise the page must be marked:

- `provisional`
- `review_required`
- `quarantined`

Packaging requirements:

- preserve suspicion reasons
- preserve route and OCR witness data
- preserve whether a page passed through retry or specialist fallback

### 6. Cross-Packet Evaluation Harness

This is required before claiming generalization.

Build a gold set with:

- `20` to `30` packets
- at least `5` municipalities
- `150` to `300` hand-graded pages
- explicit coverage for:
  - agenda continuations
  - staff summaries
  - finance attachments
  - rotated pages
  - blank separators
  - mixed exhibits
  - forms and signatures

Track:

- source-link integrity
- function accuracy
- layout accuracy
- role accuracy
- extractor success by family
- high-confidence wrong-label rate
- abstention rate
- runtime and throughput by packet

Without this harness, threshold tuning is not trusted.

## Performance Constraints

Quality work must not blindly explode runtime.

Observed baseline on `RUN_2026_04_10_43EB`:

- total runtime: `169.83s`
- OCR dominates cumulative feature-stage time
- OCR is the main bottleneck

Performance requirements:

- do not globalize expensive retry lanes across all pages
- keep retry routing focused on suspicious or low-yield pages
- keep specialist fallback to the hard tail, ideally the worst `5%` to `10%`
- preserve or improve throughput after normalization cleanup and artifact-write cleanup

Speed work that is compatible with this spec:

- increase page-level concurrency carefully
- reduce unnecessary artifact writes in non-debug runs
- avoid duplicate OCR calls when route evidence is already decisive

## Implementation Order

### Phase 1: Normalization

- add cardinal orientation resolution
- keep fine deskew after orientation
- expand suspicious-page retry grid
- improve retry winner selection
- preserve retry witness metadata

Acceptance:

- page 2 / continuation-style failures improve without packet-specific hacks
- no increase in high-confidence gibberish
- no regression in source linkage or package integrity

### Phase 2: Context Discipline

- tighten evidence floors across all carry paths
- require OCR quality and route stability for context promotion
- reduce semantic bleed from legacy flat labels

Acceptance:

- noisy finance-like pages stop being over-promoted by neighbors
- continuation pages may stay provisional rather than being forced into a wrong class

### Phase 3: Structured Tail

- add specialist fallback lane for tables/forms/mixed pages
- keep fallback targeted and explicit
- export fallback usage and confidence

Acceptance:

- table/form ambiguity drops
- structured support pages improve without harming prose pages

### Phase 4: Calibration and Gold Set

- build cross-packet evaluation harness
- calibrate abstention thresholds
- establish a stable pass/fail report

Acceptance:

- generalization claims are based on cross-packet evidence, not one packet

## Review Standard

Every future tightening pass should be reviewed against this order:

1. Did normalization improve witness quality?
2. Did function/layout/role become more coherent?
3. Did context stay within evidence floors?
4. Did the system abstain where evidence was weak?
5. Did the change hold across packets?

If the answer to `5` is unknown, the change is not yet trusted.

## Atomic Conclusion

The next shortest path from the current machine to a real municipality reader is:

1. normalization plus retry routing
2. stricter `function + layout + role` resolution with harder context gates
3. a specialist fallback lane for table/form pages only
4. cross-packet evaluation before any `97%` claim

This is the path from "good civic OCR sorter" to "truth-grade municipality reader."
