# Executive Session PUSH (Mode 1, VA)

Loads normalized Executive Session parse/preparse records from:

- `$CC_DATA_ROOT\_Sources\M1-Meetings\Executive_Session\_output\`

Into DB tables:

- `m1_executive_session.documents`
- `m1_executive_session.sections`
- `m1_executive_session.figures`
- `cco.registry`
- `cco.identities`
- `cco.observations`

Required DB migrations: `019_executive_session_schema.sql`, `020_executive_session_sections_figures.sql`, `012_industrial_glossary.sql`

## Run

```powershell
python pipelines/mode-1-meetings/executive-session/push/push_executive_session_output_to_db.py --dry-run
```

```powershell
python pipelines/mode-1-meetings/executive-session/push/push_executive_session_output_to_db.py --source-run-id RUN_20260522T224356
```
