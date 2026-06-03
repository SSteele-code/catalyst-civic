# Mode 1: Meetings Pipeline

End-to-end data pipeline for acquiring, parsing, structuring, and indexing municipal council meeting records from the Town of Richlands, Virginia.

## What It Does

Pulls every public record the town produces around a council meeting — agenda packets, approved minutes, audio recordings, transcripts, ordinances, resolutions, public hearing notices, executive session notices, and department reports — runs each through a purpose-built processing chain, and writes structured output to PostgreSQL.

## Pipeline Architecture

Each document type has its own lane. All lanes share the same stage pattern:

```
PULL  →  PRE_PARSE  →  PARSE  →  PUSH
```

| Stage | Responsibility |
|---|---|
| PULL | Fetch source documents from the town website or YouTube. Write-only to a local source store. No DB writes. |
| PRE_PARSE | Discover, classify, and stage relevant sections from parser output. Integrity-gated. |
| PARSE | Normalize staged sections into the canonical output schema. |
| PUSH | Load structured output to PostgreSQL. |

## Document Lanes

| Lane | Source | Entry Point |
|---|---|---|
| `agenda/` | Town agenda PDFs | `agenda/conductor.py` |
| `transcript/` | YouTube VTT / local audio | `transcript/production_line.py` |
| `minutes/` | Town minutes PDFs + agenda text | `minutes/pull/` |
| `department-reports/` | Extracted from agenda text | `department-reports/pull/` |
| `executive-session/` | Extracted from agenda text | `executive-session/pull/` |
| `ordinance-resolution/` | Extracted from agenda text | `ordinance-resolution/pull/` |
| `public-hearings/` | Extracted from agenda text | `public-hearings/pull/` |

## Key Design Decisions

**Agenda as the master source.** The PDF Parser engine turns every agenda packet into a structured text output. Five downstream lanes (department reports, executive session, ordinances, minutes, public hearings) mine that text output rather than fetching independently. The agenda packet is the single authoritative input.

**Integrity gates.** Every PULL stage computes an integrity score (anchor match ratio, excerpt coverage, boundary cleanliness) and enforces a threshold before staging. A staged record only advances to PARSE if it passed.

**Idempotency.** Every script is safe to re-run. State files and manifests track what has been processed so incremental runs skip unchanged sources.

**No data in this repo.** All source PDFs, VTT files, and database records live outside the repo in `_Sources/`. The pipeline code operates against that external store.

## Shared Modules

The two computationally heavy operations are extracted as reusable modules:

- `modules/pdf-parser/` — optical engine for document packets (OCR + layout + page classification)
- `modules/auditory-engine/` — Whisper transcription for audio recordings

## Database

Target: PostgreSQL `catalyst_civic` database.

Schema groups:
- `m1_agenda.*` — meetings, agenda items, pipeline ledger
- `m1_transcripts.*` — speaker-attributed transcript records
- `m1_minutes.*`, `m1_department_reports.*`, `m1_executive_session.*`, etc.
- `cco.*` — CCO entity registry (people, organizations, locations)

Connection is configurable via environment variables: `PG_HOST`, `PG_PORT`, `PG_DB`, `PG_USER`, `PG_PASS`.

## Running the Agenda Pipeline

```powershell
# 1. Pull agenda PDFs from the town website
python agenda/pull/orchestrator.py --since 2013

# 2. Run the full processing pipeline
python agenda/conductor.py

# 3. Audit quality
python agenda/tools/audit_pulse.py <PULSE_ID>
python agenda/tools/audit_registry.py
```

## Running the Transcript Pipeline

```powershell
# Pull new transcripts from YouTube
python transcript/pull/orchestrator.py

# Run the full PRATTLE processing loop
python transcript/production_line.py
```
