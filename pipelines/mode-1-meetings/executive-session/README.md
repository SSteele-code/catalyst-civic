# Executive Session Pipeline (VA M1)

Current status:
- PULL is implemented for agenda-output extraction into Executive Session staging.
- PRE_PARSE includes discovery + normalizer (+ glossary section generation).
- PARSE entrypoint is implemented (wrapper to canonical PRE_PARSE parser with glossary output).
- PUSH is implemented for SQL load (documents + sections + figures).

Stages:
- PULL
- PRE_PARSE
- PARSE
- PUSH
- SCHEMA

Discovery script:
- `PRE_PARSE/discover_executive_session_from_agenda_output.py`
- `PRE_PARSE/pre_parse_executive_session_from_agenda_staging.py`
- `PARSE/parse_executive_session_from_agenda_staging.py`
- `PULL/pull_executive_session_from_agenda_output.py`
- `PUSH/push_executive_session_output_to_db.py`
- See `PULL/README.md`, `PRE_PARSE/README.md`, `PARSE/README.md`, and `PUSH/README.md` for usage and outputs.
