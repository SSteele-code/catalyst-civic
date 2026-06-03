# Transcript PUSH

Loads processed transcript records and glossary hover candidates from PRATTLE output to PostgreSQL.

## Scripts

- `push_transcripts_to_db.py` — bulk load structured transcript JSON to `m1_transcripts` tables
- `push_transcript_glossary_to_authority.py` — push glossary hover candidates to `cco` authority tables

## Running

```powershell
python push/push_transcripts_to_db.py
python push/push_transcript_glossary_to_authority.py
```

## Input

Reads from `_Sources/M1-Meetings/Transcripts/_output/` — the PRATTLE output directory.
