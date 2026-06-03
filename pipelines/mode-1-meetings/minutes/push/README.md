# Catalyst Civic: PUSH Layer — Minutes

Loads normalized Minutes PRE_PARSE records into the dedicated `m1_minutes` DB tables.

## Scope

- Input lane: `_Sources/M1-Meetings/Minutes/_output/<minutes_code>/<minutes_code>.preparse.json`
- Supported minutes codes:
  - `M1.AG.MN.<document_number>.<created_yyyymmdd>.<pulled_yyyymmdd>` (agenda-mined lane)
  - `M1.MN.<document_number>.<created_yyyymmdd>.<pulled_yyyymmdd>` (approved-PDF OCR lane)
- Target DB schema: `m1_minutes`
  - `m1_minutes.meetings`
  - `m1_minutes.excerpts`

Strict invariant:

- PUSH only
- No pull logic
- No parsing/enrichment logic
- No glossary writes

## Script

- `push_minutes_output_to_db.py`

## Prerequisite

Apply DB schema first:

- `_Infra/DATABASE/init/013_minutes_schema.sql`

## Tracking Artifacts

- Global manifest:
  `_Sources/M1-Meetings/Minutes/M1_MINUTES_PUSH_MANIFEST.jsonl`
- Global state:
  `_Sources/M1-Meetings/Minutes/minutes_push_state.json`
- Per-run artifacts:
  `_Sources/M1-Meetings/Minutes/_output/_runs/<RUN_ID>/`
  - `minutes_push_manifest.jsonl`
  - `run_summary.json`
  - `minutes_push_failures.jsonl` (if any)

## DB Connection

Environment variables:

- `PG_HOST` (default `localhost`)
- `PG_PORT` (default `5432`)
- `PG_DB` (default `catalyst_civic`)
- `PG_USER` (default `postgres`)
- `PG_PASS` (default `postgres`)

## Usage

```bash
# Scan only (no DB writes)
python push_minutes_output_to_db.py --dry-run

# Push all changed minutes records
python push_minutes_output_to_db.py

# Validation batch (first N pushable records)
python push_minutes_output_to_db.py --limit 25

# Re-push even if unchanged by state
python push_minutes_output_to_db.py --force
```

If your machine has multiple Python installs, run with the interpreter that has `psycopg2`:

```bash
py -3.12 push_minutes_output_to_db.py
```

## Idempotency

- State is keyed by `meeting_id`.
- Re-runs skip records when source `.preparse.json` SHA is unchanged and prior status is `pushed`.
- `--force` bypasses skip behavior and re-upserts meetings/excerpts.
