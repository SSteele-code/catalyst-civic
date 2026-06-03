# Public Hearings Pipeline

Extracts public hearing sections from agenda parser output, normalizes them, and loads records to PostgreSQL.

## Stages

```
pull/       — extract public hearing sections from agenda text output
pre-parse/  — discover and stage candidate sections
parse/      — normalize to output schema
push/       — load to database + push glossary entries to authority
```

## Running

```powershell
python pull/pull_public_hearing_from_agenda_output.py
python pre-parse/pre_parse_public_hearing_from_agenda_staging.py
python parse/parse_public_hearing_from_agenda_staging.py
python push/push_public_hearing_output_to_db.py
python push/push_public_hearing_glossary_to_authority.py
```

## Path Configuration

Scripts reference `_Sources/M1-Meetings/Agendas/_output/` as the input root. Update path constants for your local environment.
