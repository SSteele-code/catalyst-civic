# Catalyst Civic: HOVER Layer — Minutes (Glossary Candidate Lane)

This stage reads normalized Minutes output records and produces glossary-hover
candidate artifacts for extraction QA.

## Scope

- Input lane: `_Sources/M1-Meetings/Minutes/_output/<minutes_code>/<minutes_code>.preparse.json`
  - Supports both:
    - `M1.AG.MN...` (agenda-mined excerpts)
    - `M1.MN...` (approved PDF OCR records)
- Output lane: `_Sources/M1-Meetings/Minutes/_output/<minutes_code>/`
  - `<minutes_code>.glossary_hover.json`
  - `<minutes_code>.glossary_hover.txt`
- Strict invariant:
  - HOVER/extract only
  - No DB writes
  - No glossary writes

## Script

- `hover_minutes_glossary_from_output.py`

## CCO Seed Inputs

The hover extraction references CCO files in main Catalyst root:

- `_CCO/CORE/CCO_ONTOLOGY_EXTRACT.json`
- `_CCO/Mode_1_MEETINGS/.../Richlands/Roster/Richlands_Roster.json`

These are used as known-name seeds for candidate matching/canonicalization.

## Output Schema

- Schema version: `m1.minutes.glossary_hover.v1`
- Record type: `minutes_glossary_candidates_record`

Primary fields in `.glossary_hover.json`:

- `minutes_code` / `artifact_machine_code`
- `linked_source_pdf_code`
- `meeting_context.*`
- `lineage.*` (source preparse + CCO seed references)
- `glossary_hover_summary.*`
- `glossary_entities[]` (candidate entities with evidence)
- `qa.*` (explicit extraction-only marker)

## Tracking Artifacts

- Global manifest:
  `_Sources/M1-Meetings/Minutes/M1_MINUTES_GLOSSARY_HOVER_MANIFEST.jsonl`
- Global state:
  `_Sources/M1-Meetings/Minutes/minutes_glossary_hover_state.json`
- Global flat candidate snapshot (one row per entity):
  `_Sources/M1-Meetings/Minutes/M1_MINUTES_GLOSSARY_HOVER_CANDIDATES_SNAPSHOT.jsonl`
- Per-run artifacts:
  `_Sources/M1-Meetings/Minutes/_output/_runs/<RUN_ID>/`
  - `minutes_glossary_hover_manifest.jsonl`
  - `minutes_glossary_hover_candidates.jsonl`
  - `run_summary.json`
  - `minutes_glossary_hover_quality.json`
  - `minutes_glossary_hover_failures.jsonl` (if any)

Flat candidate JSONL fields include:

- `minutes_code`, `source_lane`, `source_preparse_lane`, `entity_id`
- `category`, `canonical_name`, `fact_key`, `confidence`, `match_type`
- `evidence_text`, `evidence_sha256`
- `qa_flags[]`
- `source_span_char_start`, `source_span_char_end`
- `source_span_excerpt_id`, `source_span_page_number`

## Usage

```bash
# Scan only
python hover_minutes_glossary_from_output.py --dry-run

# Build glossary-hover outputs for all minutes records
python hover_minutes_glossary_from_output.py

# Validation batch
python hover_minutes_glossary_from_output.py --limit 20

# Rebuild even if unchanged by state
python hover_minutes_glossary_from_output.py --force
```

## Idempotency

- State is keyed by `minutes_code`.
- Re-runs skip unchanged source preparse hashes.
- `--force` bypasses skip behavior and rewrites hover outputs.
