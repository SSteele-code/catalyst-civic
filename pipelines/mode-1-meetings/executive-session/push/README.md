# Executive Session PUSH (Mode 1, VA)

Loads normalized Executive Session parse/preparse records from:

- `C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Executive_Session\_output\`

Into DB tables:

- `m1_executive_session.documents`
- `m1_executive_session.sections`
- `m1_executive_session.figures`
- `cco.registry`
- `cco.identities`
- `cco.observations`

Required DB migrations:

- `C:\Users\simon\CatalystCivic\_Infra\DATABASE\init\019_executive_session_schema.sql`
- `C:\Users\simon\CatalystCivic\_Infra\DATABASE\init\020_executive_session_sections_figures.sql`
- `C:\Users\simon\CatalystCivic\_Infra\DATABASE\init\012_industrial_glossary.sql`

## Run

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\EXECUTIVE_SESSION\PUSH\push_executive_session_output_to_db.py --dry-run
```

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\EXECUTIVE_SESSION\PUSH\push_executive_session_output_to_db.py --source-run-id RUN_20260522T224356
```
