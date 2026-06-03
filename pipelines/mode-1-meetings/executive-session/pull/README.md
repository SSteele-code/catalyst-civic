# Catalyst Civic: PULL Layer — Executive Session

Stages executive-session material from Agenda output packets into Executive Session staging.

## Scope

- Input lane:
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Agendas\_output\<M1.AG...>\<M1.AG...>.txt`
- Candidate selector:
  - Discovery run manifest from:
    - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Executive_Session\_output\_runs\RUN-ES-DISCOVERY-<timestamp>\executive_session_discovery_manifest.jsonl`
- Output lane:
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Executive_Session\_staging\RUN_<timestamp>\*.executive_session.json`
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Executive_Session\_staging\RUN_<timestamp>\*.executive_session.txt`

Strict invariant:

- Pull only
- No DB writes
- No downstream parse/enrichment

## Script

- `pull_executive_session_from_agenda_output.py`

## Tracking Artifacts

- Global manifest:
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Executive_Session\M1_EXECUTIVE_SESSION_OUTPUT_PULL_MANIFEST.jsonl`
- Global state:
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Executive_Session\executive_session_output_pull_state.json`
- Per-run artifacts:
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Executive_Session\_staging\RUN_<timestamp>\executive_session_output_pull_manifest.jsonl`
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Executive_Session\_staging\RUN_<timestamp>\run_summary.json`

## Source Bundle Naming

For each staged packet:

- Source bundle folder:
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Executive_Session\_sources\<M1.AG...>\`
- Source bundle text:
  - `<M1.AG...>.executive_session.txt`
- Source factsheet (when present):
  - `<M1.AG...>.factsheet.json`

## Integrity Gate

- Run computes per-packet traceability checks and a run-level document integrity rate.
- Default target and gate:
  - `>= 0.95`
- Script exits non-zero on gate fail unless `--no-enforce-integrity` is set.

## Usage

```powershell
# Dry run
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\EXECUTIVE_SESSION\PULL\pull_executive_session_from_agenda_output.py --dry-run

# Stage candidate rows from latest discovery run (enforce >=95%)
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\EXECUTIVE_SESSION\PULL\pull_executive_session_from_agenda_output.py

# Include adjacent rows
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\EXECUTIVE_SESSION\PULL\pull_executive_session_from_agenda_output.py --include-adjacent

# Pin to specific discovery run
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\EXECUTIVE_SESSION\PULL\pull_executive_session_from_agenda_output.py --discovery-run-id RUN-ES-DISCOVERY-20260522T220741
```

