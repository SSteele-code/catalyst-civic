# Agenda Pipeline

Pulls municipal council agenda PDFs, runs them through optical parsing, structures the output, and loads meetings, agenda items, and glossary entities into PostgreSQL.

## Architecture

`conductor.py` orchestrates seven states:

1. `INTAKE` — initialize pulse and pipeline ledger row
2. `OPTICAL` — parse PDF pages via the PDF Parser module
3. `SEMANTIC` — build scaffold markdown from per-page JSON
4. `SCULPT` — normalize agenda hierarchy
5. `LOAD` — migrate structured data and load glossary entities
6. `VAULT` — persist packet output bundle to source store
7. `JANITOR` — clean up runtime artifacts for repeatable runs

## Layout

```
conductor.py              — top-level orchestration entry point
requirements.txt
pull/                     — source discovery and fetch
schema-scaffolder/        — page JSON to scaffold markdown
schema-sculptor/          — scaffold to structured agenda hierarchy
migrator/                 — SQL ingestion into m1_agenda tables
registry-loader/          — glossary extraction into cco tables
tools/                    — operational tooling (audit, reconcile, cleanup)
```

## Running

```powershell
# Pull agenda PDFs
python pull/orchestrator.py --since 2013

# Optional: limit fetch batch
python pull/orchestrator.py --since 2013 --limit 5

# Process all staged PDFs
python conductor.py

# Reconcile glossary after parser changes
python tools/reconcile_glossary_runs.py

# Audit
python tools/audit_pulse.py <PULSE_ID>
python tools/check_registry.py <PULSE_ID>
python tools/audit_registry.py

# Reset
python tools/wipe_m1_data.py
python tools/janitor_plus.py
```

## Database Tables

- `m1_agenda.pipeline_ledger` — per-run state tracking
- `m1_agenda.meetings` — structured meeting records
- `m1_agenda.items` — structured agenda items
- `cco.registry`, `cco.identities`, `cco.observations` — entity glossary

## Configuration

Database connection via environment variables (defaults shown):
```
PG_HOST=localhost
PG_PORT=5432
PG_DB=catalyst_civic
PG_USER=postgres
PG_PASS=postgres
```

Glossary tuning:
```
M1_GLOSSARY_HIGH_RECALL=0
M1_GLOSSARY_CONTEXT_WINDOW=40
M1_GLOSSARY_MAX_EVIDENCE=160
M1_GLOSSARY_MAX_FACT_CONTEXT=120
```

## Path Configuration

`conductor.py` references the local source store at `_Sources/M1-Meetings/Agendas/`. Update the `PULL_DIR` constant for your local environment.

## Requirements

- Python 3.12+
- PostgreSQL with `catalyst_civic` database and required schemas
- Tesseract OCR on PATH
- `pip install -r requirements.txt`
