# Department Reports PUSH (Mode 1, VA)

Loads DepartmentReports parse/preparse output into:

- `m1_department_reports.documents`
- `m1_department_reports.excerpts`
- `m1_department_reports.figures`

Required DB migrations: `017_department_reports_schema.sql`, `018_department_reports_excerpts_figures.sql`

## Run

```powershell
python pipelines/mode-1-meetings/department-reports/push/push_department_reports_output_to_db.py --dry-run
```

```powershell
python pipelines/mode-1-meetings/department-reports/push/push_department_reports_output_to_db.py --source-run-id RUN_20260522T111353
```

```powershell
python pipelines/mode-1-meetings/department-reports/push/push_department_reports_output_to_db.py --force
```
