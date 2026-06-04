# Executive Session PARSE (Mode 1, VA)

This stage uses the canonical parser implementation in:

- `..\PRE_PARSE\pre_parse_executive_session_from_agenda_staging.py`

The PARSE entrypoint exists for lane parity with other M1 pipelines.
The canonical parser emits `executive_session_sections` and a `glossary` section in the same record.

## Run

```powershell
py -3.12 $CC_DATA_ROOT\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\EXECUTIVE_SESSION\PARSE\parse_executive_session_from_agenda_staging.py --dry-run
```

```powershell
py -3.12 $CC_DATA_ROOT\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\EXECUTIVE_SESSION\PARSE\parse_executive_session_from_agenda_staging.py --force
```
