# Minutes Pipeline

Two independent pull flows feed a shared pre-parse, OCR, and push chain for council meeting minutes.

## Stages

```
pull/       — two sources: agenda text mining + town website PDF fetch
pre-parse/  — stage and normalize minutes sections
ocr/        — OCR vault of approved minutes PDFs
hover/      — generate glossary hover candidates from minutes output
push/       — load to m1_minutes tables in PostgreSQL
```

## Flow A: Output Mining

Mines the agenda parser's text output for minutes sections (approval motions, corrections, references). No PDF download required — content is extracted from the existing agenda pipeline output.

```powershell
python pull/pull_minutes_from_agenda_output.py
python pull/pull_minutes_from_agenda_output.py --dry-run
python pull/pull_minutes_from_agenda_output.py --force --limit 25
```

## Flow B: Approved Minutes PDF Fetch

Pulls approved minutes PDFs directly from the town website. Validates each download (size check, PDF header signature) before vaulting.

```powershell
python pull/pull_approved_minutes_from_town_site.py
python pull/pull_approved_minutes_from_town_site.py --since 2024
python pull/pull_approved_minutes_from_town_site.py --dry-run
```

## Artifacts

Per staged source packet:
- `<machine_code>.minutes.json`
- `<machine_code>.minutes.txt`

Per run:
- `minutes_output_pull_manifest.jsonl`
- `run_summary.json`

## Path Configuration

Scripts reference the local source store at `_Sources/M1-Meetings/Minutes/`. Update path constants for your local environment.
