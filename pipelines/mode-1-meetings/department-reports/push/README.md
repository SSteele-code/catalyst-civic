# Department Reports PUSH (Mode 1, VA)

Loads DepartmentReports parse/preparse output into SQL:

- `m1_department_reports.documents`
- `m1_department_reports.excerpts`
- `m1_department_reports.figures`

Required DB init migrations:

- `017_department_reports_schema.sql`
- `018_department_reports_excerpts_figures.sql`

## Run

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PUSH\push_department_reports_output_to_db.py --dry-run
```

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PUSH\push_department_reports_output_to_db.py --source-run-id RUN_20260522T111353
```

```powershell
py -3.12 C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\DEPARTMENT_REPORTS\PUSH\push_department_reports_output_to_db.py --force
```

