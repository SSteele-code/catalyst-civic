# Catalyst Civic: OCR Layer — Minutes (Approved PDF Lane)

This stage reads approved minutes PDFs from `_vaulted` and emits normalized
minutes schema records into `_output`.

## Scope

- Input lane: `_Sources/M1-Meetings/Minutes/_vaulted/M1.MN.*.pdf`
- Output lane: `_Sources/M1-Meetings/Minutes/_output/<minutes_code>/`
- Strict invariant:
  - OCR/normalize only
  - No DB writes
  - No glossary writes

## Script

- `ocr_minutes_vault_to_schema.py`

## Schema Contract

- Output schema version: `m1.minutes.preparse.v1`
- Output record type: `minutes_preparse_record`
- Minutes code: `M1.MN.<document_number>.<created_yyyymmdd>.<pulled_yyyymmdd>`
- Source lane value: `approved_minutes_pdf_ocr`

The OCR lane emits the same normalized contract family used by PRE_PARSE so one
pusher can consume both lanes.

## OCR Regime

1. Native extraction pass via `pdftotext -layout`
2. Per-page fallback OCR only when native text is below threshold:
   - `pdftoppm` (gray PNG render)
   - `tesseract` (`--oem 1`) with adaptive modes:
     - `--psm 6`
     - rescue `--psm 4`
     - rescue `--psm 11`
     - orientation-aware rescue `--psm 1` (sideways/rotated scans)

Optional full OCR mode:

- `--ocr-all-pages`

## Tooling Requirements

- `pdftotext`
- `pdftoppm`
- `pdfinfo`
- `tesseract` (default path: `C:\Program Files\Tesseract-OCR\tesseract.exe`)

Optional env var overrides:

- `M1_MINUTES_PDFTOTEXT_EXE`
- `M1_MINUTES_PDFTOPPM_EXE`
- `M1_MINUTES_PDFINFO_EXE`
- `M1_MINUTES_TESSERACT_EXE`

## Tracking Artifacts

- Global manifest:
  `_Sources/M1-Meetings/Minutes/M1_MINUTES_OCR_MANIFEST.jsonl`
- Global state:
  `_Sources/M1-Meetings/Minutes/minutes_ocr_state.json`
- Per-run artifacts:
  `_Sources/M1-Meetings/Minutes/_output/_runs/<RUN_ID>/`
  - `minutes_ocr_manifest.jsonl`
  - `run_summary.json`
  - `minutes_ocr_failures.jsonl` (if any)

## Usage

```bash
# Dry run
python ocr_minutes_vault_to_schema.py --dry-run

# Process all vaulted approved PDFs
python ocr_minutes_vault_to_schema.py

# Validation batch
python ocr_minutes_vault_to_schema.py --limit 10

# Force full rebuild
python ocr_minutes_vault_to_schema.py --force

# Tune fallback threshold and DPI
python ocr_minutes_vault_to_schema.py --native-page-min-chars 120 --ocr-dpi 350

# Force OCR on every page
python ocr_minutes_vault_to_schema.py --ocr-all-pages

# Repair OCR state/manifest from existing M1.MN outputs (no OCR pass)
python ocr_minutes_vault_to_schema.py --reconcile-existing

# Run all vaulted PDFs in resumable batches until complete
python ocr_minutes_vault_to_schema.py --until-complete --batch-size 20

# Low-text audit only (no writes)
python ocr_minutes_vault_to_schema.py --rerun-low-text --dry-run

# Small targeted low-text rerun (example: first 15 low-text records)
python ocr_minutes_vault_to_schema.py --rerun-low-text --low-text-limit 15
```

Low-text rerun notes:

- Targets only `M1.MN.*` OCR records already in `_output`.
- Never touches mined `M1.AG.MN.*` outputs.
- Rerun mode forces full-page OCR for selected records using at least 450 DPI.
- Default low-text heuristic:
  - average chars/page `< 500`, or
  - total chars `< 1000`
- Writes per-run summary:
  `_Sources/M1-Meetings/Minutes/_output/_runs/<RUN_ID>/minutes_ocr_low_text_rerun_summary.json`
