# Catalyst Civic: PULL Layer — Department Reports

Stages department-report material from Agenda output packets into DepartmentReports staging.

## Scope

- Input lane:
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Agendas\_output\<M1.AG...>\<M1.AG...>.txt`
- Candidate selector:
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports\M1_DEPARTMENT_REPORTS_DISCOVERY_MANIFEST.jsonl`
- Output lane:
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports\_staging\RUN_<timestamp>\*.department_reports.json`
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports\_staging\RUN_<timestamp>\*.department_reports.txt`

Strict invariant:

- Pull only
- No DB writes
- No downstream parse/enrichment

## Script

- `pull_department_reports_from_agenda_output.py`
- `audit_department_reports_pull_integrity.py` (independent post-pull audit gate)

## Tracking Artifacts

- Global manifest:
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports\M1_DEPARTMENT_REPORTS_OUTPUT_PULL_MANIFEST.jsonl`
- Global state:
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports\department_reports_output_pull_state.json`
- Per-run artifacts:
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports\_staging\RUN_<timestamp>\department_reports_output_pull_manifest.jsonl`
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports\_staging\RUN_<timestamp>\run_summary.json`
  - `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports\_staging\RUN_<timestamp>\external_integrity_audit.json`

## Integrity Gates

- Run computes per-record integrity scores and a run-level `document_integrity_rate`.
- Run also executes an independent external audit over staged JSON + source TXT:
  - `external_document_integrity_rate`
- Default target and gate:
  - `>= 0.95`
- If integrity gate fails, script exits non-zero unless overridden.

## Usage

```powershell
# Dry-run scoring only
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PULL\pull_department_reports_from_agenda_output.py --dry-run

# Stage candidate records and enforce >=95% integrity
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PULL\pull_department_reports_from_agenda_output.py

# Include adjacent discovery rows
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PULL\pull_department_reports_from_agenda_output.py --include-adjacent

# Change threshold
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PULL\pull_department_reports_from_agenda_output.py --integrity-threshold 0.97

# Do not fail process on threshold miss
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PULL\pull_department_reports_from_agenda_output.py --no-enforce-integrity

# Skip independent external audit gate (not recommended)
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PULL\pull_department_reports_from_agenda_output.py --no-external-audit
```

Run external audit directly for a specific staging run:

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PULL\audit_department_reports_pull_integrity.py --run-dir C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports\_staging\RUN_20260519T212719
```
