# Catalyst Civic: PRE_PARSE Layer - Department Reports (Agenda-Staging Lane)

This folder contains two PRE_PARSE-adjacent scripts:

- Discovery pass (classification seed):
  - `discover_department_reports_from_agenda_output.py`
- PRE_PARSE normalizer (Stage 2):
  - `pre_parse_department_reports_from_agenda_staging.py`

## Discovery Pass

Defines what counts as a Department Report from agenda output text before/alongside pull tuning.

Input:
- `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Agendas\_output\<M1.AG...>\<M1.AG...>.txt`

Writes:
- `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports\M1_DEPARTMENT_REPORTS_DISCOVERY_MANIFEST.jsonl`
- `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports\department_reports_discovery_summary.json`
- `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports\_output\_runs\RUN-DR-DISCOVERY-<timestamp>\department_reports_discovery_manifest.jsonl`
- `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports\_output\_runs\RUN-DR-DISCOVERY-<timestamp>\department_reports_discovery_summary.json`

Run:

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PRE_PARSE\discover_department_reports_from_agenda_output.py
```

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PRE_PARSE\discover_department_reports_from_agenda_output.py --dry-run
```

## PRE_PARSE Normalizer (Stage 2)

Normalizes staged pull outputs into stable, pusher-ready DepartmentReports schema.

Scope:
- Input lane: `_Sources/M1-Meetings/DepartmentReports/_staging/**/.department_reports.json`
- Output lane: `_Sources/M1-Meetings/DepartmentReports/_output/<department_report_code>/`
- Strict invariant:
  - PRE_PARSE only
  - No DB writes
  - No glossary writes

ID contract:
- Source PDF code: `M1.AG.<document_number>.<created_yyyymmdd>.<pulled_yyyymmdd>`
- Department report code: `M1.AG.DR.<document_number>.<created_yyyymmdd>.<pulled_yyyymmdd>`

Schema + tracking:
- Output schema version: `m1.department_reports.preparse.v1`
- Global manifest:
  - `_Sources/M1-Meetings/DepartmentReports/M1_DEPARTMENT_REPORTS_PREPARSE_MANIFEST.jsonl`
- Global state:
  - `_Sources/M1-Meetings/DepartmentReports/department_reports_preparse_state.json`
- Run artifacts:
  - `_Sources/M1-Meetings/DepartmentReports/_output/_runs/<RUN_ID>/department_reports_preparse_manifest.jsonl`
  - `_Sources/M1-Meetings/DepartmentReports/_output/_runs/<RUN_ID>/run_summary.json`
  - `_Sources/M1-Meetings/DepartmentReports/_output/_runs/<RUN_ID>/department_reports_preparse_failures.jsonl` (if any)

Usage:

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PRE_PARSE\pre_parse_department_reports_from_agenda_staging.py --dry-run
```

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PRE_PARSE\pre_parse_department_reports_from_agenda_staging.py
```

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PRE_PARSE\pre_parse_department_reports_from_agenda_staging.py --source-run-id RUN_20260519T212719
```

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PRE_PARSE\pre_parse_department_reports_from_agenda_staging.py --all-staging
```
