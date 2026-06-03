# Department Reports PARSE (Mode 1, VA)

This stage uses the canonical parser implementation in:

- `..\PRE_PARSE\pre_parse_department_reports_from_agenda_staging.py`

The PARSE entrypoint exists for lane parity with other M1 pipelines.

## Run

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PARSE\parse_department_reports_from_agenda_staging.py --dry-run
```

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PARSE\parse_department_reports_from_agenda_staging.py --force
```

