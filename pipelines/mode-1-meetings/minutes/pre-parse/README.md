# Catalyst Civic: PRE_PARSE Layer — Minutes (Agenda-Staging Lane)

This stage normalizes agenda-mined minutes excerpts into a stable, pusher-ready Minutes schema.

## Scope

- Input lane: `_Sources/M1-Meetings/Minutes/_staging/**/.minutes.json`
- Output lane: `_Sources/M1-Meetings/Minutes/_output/<minutes_code>/`
- Strict invariant:
  - PRE_PARSE only
  - No DB writes
  - No glossary writes

## Script

- `pre_parse_minutes_from_agenda_staging.py`

## ID Contract (Linked to Agenda PDF Source)

- Source PDF code: `M1.AG.<document_number>.<created_yyyymmdd>.<pulled_yyyymmdd>`
- Minutes code: `M1.AG.MN.<document_number>.<created_yyyymmdd>.<pulled_yyyymmdd>`

`M1.AG.MN...` is derived from the agenda output factsheet field:

- `source_pdf_original_name` in
  `Agendas/_output/<source_pulse_id>/<source_pulse_id>.factsheet.json`

## Output Schema

- Schema version: `m1.minutes.preparse.v1`
- Record file:
  `_Sources/M1-Meetings/Minutes/_output/<minutes_code>/<minutes_code>.preparse.json`
- Summary file:
  `_Sources/M1-Meetings/Minutes/_output/<minutes_code>/<minutes_code>.preparse.txt`

Primary fields in `.preparse.json`:

- `minutes_code` / `artifact_machine_code`
- `linked_source_pdf_code`
- `source_lane`
- `meeting_context.anchor_meeting_date` (derived from source PDF created date)
- `lineage.*` (stage/factsheet/source hashes + file paths)
- `minutes_excerpt_summary.*` (counts, approval signal, date mentions)
- `minutes_excerpts[]` (excerpt rows + per-row signals)
- `pusher_ready.*` contract for downstream DB loader

## Tracking Artifacts

- Global manifest:
  `_Sources/M1-Meetings/Minutes/M1_MINUTES_PREPARSE_MANIFEST.jsonl`
- Global state:
  `_Sources/M1-Meetings/Minutes/minutes_preparse_state.json`
- Per-run artifacts:
  `_Sources/M1-Meetings/Minutes/_output/_runs/<RUN_ID>/`
  - `minutes_preparse_manifest.jsonl`
  - `run_summary.json`
  - `minutes_preparse_failures.jsonl` (if any)

## Usage

```bash
# Scan/map only
python pre_parse_minutes_from_agenda_staging.py --dry-run

# Build normalized outputs
python pre_parse_minutes_from_agenda_staging.py

# Process first N minutes codes
python pre_parse_minutes_from_agenda_staging.py --limit 25

# Rebuild even if unchanged by state
python pre_parse_minutes_from_agenda_staging.py --force
```

## Idempotency

- State is keyed by `minutes_code`.
- Re-runs skip unchanged source stage + source text hashes.
- `--force` bypasses skip behavior and rewrites outputs.
