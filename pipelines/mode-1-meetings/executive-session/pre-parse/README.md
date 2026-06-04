# Catalyst Civic: PRE_PARSE Layer - Executive Session (Agenda-Staging Lane)

This folder contains two PRE_PARSE-adjacent scripts:

- Discovery pass (classification seed):
  - `discover_executive_session_from_agenda_output.py`
- PRE_PARSE normalizer (Stage 2/3 parser implementation):
  - `pre_parse_executive_session_from_agenda_staging.py`

## Discovery Pass

Identifies Executive Session sections from agenda output text.

Input:
- `$CC_DATA_ROOT\_Sources\M1-Meetings\Agendas\_output\<M1.AG...>\<M1.AG...>.txt`

Writes:
- `$CC_DATA_ROOT\_Sources\M1-Meetings\Executive_Session\M1_EXECUTIVE_SESSION_DISCOVERY_MANIFEST.jsonl`
- `$CC_DATA_ROOT\_Sources\M1-Meetings\Executive_Session\executive_session_discovery_summary.json`
- `$CC_DATA_ROOT\_Sources\M1-Meetings\Executive_Session\_output\_runs\RUN-ES-DISCOVERY-<timestamp>\executive_session_discovery_manifest.jsonl`
- `$CC_DATA_ROOT\_Sources\M1-Meetings\Executive_Session\_output\_runs\RUN-ES-DISCOVERY-<timestamp>\executive_session_discovery_summary.json`

Run:

```powershell
py -3.12 $CC_DATA_ROOT\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\EXECUTIVE_SESSION\PRE_PARSE\discover_executive_session_from_agenda_output.py
```

## PRE_PARSE Normalizer

Normalizes staged pull outputs into stable, pusher-ready Executive Session schema.

Scope:
- Input lane: `_Sources/M1-Meetings/Executive_Session/_staging/**/.executive_session.json`
- Output lane: `_Sources/M1-Meetings/Executive_Session/_output/<executive_session_code>/`
- Strict invariant:
  - PRE_PARSE only
  - No DB writes
  - Includes glossary section in output payload (for downstream DB + CCO push stages)

ID contract:
- Source PDF code: `M1.AG.<document_number>.<created_yyyymmdd>.<pulled_yyyymmdd>`
- Executive Session code: `M1.AG.ES.<document_number>.<created_yyyymmdd>.<pulled_yyyymmdd>`

Schema + tracking:
- Output schema version: `m1.executive_session.preparse.v1`
- Global manifest:
  - `_Sources/M1-Meetings/Executive_Session/M1_EXECUTIVE_SESSION_PREPARSE_MANIFEST.jsonl`
- Global state:
  - `_Sources/M1-Meetings/Executive_Session/executive_session_preparse_state.json`
- Run artifacts:
  - `_Sources/M1-Meetings/Executive_Session/_output/_runs/<RUN_ID>/executive_session_preparse_manifest.jsonl`
  - `_Sources/M1-Meetings/Executive_Session/_output/_runs/<RUN_ID>/run_summary.json`
  - `_Sources/M1-Meetings/Executive_Session/_output/_runs/<RUN_ID>/executive_session_preparse_failures.jsonl` (if any)

Usage:

```powershell
py -3.12 $CC_DATA_ROOT\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\EXECUTIVE_SESSION\PRE_PARSE\pre_parse_executive_session_from_agenda_staging.py
```

```powershell
py -3.12 $CC_DATA_ROOT\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\EXECUTIVE_SESSION\PRE_PARSE\pre_parse_executive_session_from_agenda_staging.py --source-run-id RUN_20260522T222706
```

```powershell
py -3.12 $CC_DATA_ROOT\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\EXECUTIVE_SESSION\PRE_PARSE\pre_parse_executive_session_from_agenda_staging.py --dry-run
```
