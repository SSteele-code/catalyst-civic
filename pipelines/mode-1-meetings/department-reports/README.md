# Department Reports Pipeline (VA M1)

Current status:
- PULL is implemented for agenda-output extraction into DepartmentReports staging.
- PRE_PARSE includes discovery + Stage 2 normalizer.
- External integrity audit gate is implemented for PULL.
- PARSE entrypoint is implemented (mirrors other lanes; forwards to canonical PRE_PARSE parser).
- PUSH is implemented for SQL load (documents + excerpts + figures).

Stages:
- PULL
- PRE_PARSE
- PARSE
- PUSH
- SCHEMA

Discovery script:
- `PRE_PARSE/discover_department_reports_from_agenda_output.py`
- `PRE_PARSE/pre_parse_department_reports_from_agenda_staging.py`
- `PARSE/parse_department_reports_from_agenda_staging.py`
- `PULL/audit_department_reports_pull_integrity.py`
- `PUSH/push_department_reports_output_to_db.py`
- See `PULL/README.md`, `PRE_PARSE/README.md`, `PARSE/README.md`, and `PUSH/README.md` for stage usage and artifacts.
