# Catalyst Civic: PULL Layer — Minutes

The Minutes pull layer has two strict pull-only flows:

- No DB writes
- No parser mutation
- No enrichment pipeline logic

## Flow A: Output-Mining Pull (Agenda Output Text)

Script:

- `pull_minutes_from_agenda_output.py`

Scope:

- Input root: `_Sources/M1-Meetings/Agendas/_output/`
- Canonical source files only: `Agendas/_output/<machine_code>/<machine_code>.txt`
- Output: `_Sources/M1-Meetings/Minutes/_staging/<RUN_ID>/`

What it captures:

- Explicit minutes section markers, including OCR drift:
  - `V. Approval of Minutes`
  - `VI. Minutes`
  - `b. Minutes`
  - `IN RE: Minutes`
  - `Minutes Distributed Prior to Meeting`
  - `Minutes- ...`
- Minutes approval/action lines:
  - motions to approve/adopt minutes
  - council voted to approve minutes
  - corrections/deletions to minutes

What it avoids:

- Time-duration chatter (for example, `three minute max`, `ten-minute recess`, `few minutes ago`) unless clearly minutes-governance context.

Usage:

```bash
# Dry run (scan only)
python pull_minutes_from_agenda_output.py --dry-run

# Pull/stage all changed source outputs
python pull_minutes_from_agenda_output.py

# Reprocess everything regardless of state
python pull_minutes_from_agenda_output.py --force

# Stage first N hit files
python pull_minutes_from_agenda_output.py --limit 25
```

Artifacts:

- Per matched source packet:
  - `<machine_code>.minutes.json`
  - `<machine_code>.minutes.txt`
- Per run:
  - `minutes_output_pull_manifest.jsonl`
  - `run_summary.json`
- Global tracking:
  - `_Sources/M1-Meetings/Minutes/M1_MINUTES_OUTPUT_PULL_MANIFEST.jsonl`
  - `_Sources/M1-Meetings/Minutes/minutes_output_pull_state.json`

## Flow B: Approved-Minutes Website Pull (Town Source PDFs)

Script:

- `pull_approved_minutes_from_town_site.py`

Scope:

- Discovery source: `https://www.town.richlands.va.us/minutes/minutes.html`
- Year-page source pattern: `https://www.town.richlands.va.us/minutes/<YYYY>MINUTES.html`
- Pulled source files: approved minute PDFs linked under `/minutes/`
- Output root: `_Sources/M1-Meetings/Minutes/_vaulted/`

Naming and tracking:

- Machine code pattern: `M1.MN.<document_number>.<created_yyyymmdd>.<pulled_yyyymmdd>.pdf`
- Global manifest: `_Sources/M1-Meetings/Minutes/M1_MINUTES_APPROVED_PULL_MANIFEST.jsonl`
- Global state: `_Sources/M1-Meetings/Minutes/minutes_approved_pull_state.json`
- Per-run artifacts: `_Sources/M1-Meetings/Minutes/_vaulted/_runs/<RUN_ID>/`
  - `minutes_approved_pull_manifest.jsonl`
  - `minutes_approved_pull_failures.jsonl` (if any)
  - `run_summary.json`

Download integrity checks:

- Rejects empty downloads
- Rejects very small payloads
- Requires PDF file signature (`%PDF-`) before vaulting

Usage:

```bash
# Dry run discovery only
python pull_approved_minutes_from_town_site.py --dry-run

# Full pull
python pull_approved_minutes_from_town_site.py

# Pull with limit (validation batch)
python pull_approved_minutes_from_town_site.py --limit 10

# Restrict to recent years
python pull_approved_minutes_from_town_site.py --since 2024

# Restrict to one year
python pull_approved_minutes_from_town_site.py --year 2025

# Reprocess even tracked URLs
python pull_approved_minutes_from_town_site.py --force
```

Idempotency behavior:

- URLs already present in manifest are skipped.
- URLs already marked `ok` in state are skipped.
- Failed URLs are retried on rerun.
