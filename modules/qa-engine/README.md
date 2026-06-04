# QA State Machine

Resident QA engine for Catalyst Civic parser output. This module receives a handshake, walks every page back to source, stamps page-level QA machine codes, emits QA artifacts, and can run pass-only janitorial cleanup on the parser runtime.

## Machine Root

Code and config:

- `config/`
- `scripts/`
- `src/`
- `README.md`

Disposable runtime:

- `inbox/`
- `outbox/`
- `work/`
- `manifests/`
- `logs/`
- `reports/`

## Service API

Default address: `http://127.0.0.1:8093`

Endpoints:

- `POST /handshake/start`
- `GET /health`
- `GET /runs/<qa_run_id>`

Start service:

```powershell
python modules/qa-engine/scripts/run_qa_service.py
```

## Handshake Input

The handshake uses the same `job_folder` contract shape as the parser.

Minimum payload when `job_folder` is already the parser outbox run folder:

```json
{
  "job_folder": "<absolute path to parser outbox run folder>"
}
```

Optional `qa_job.json` inside the job folder:

```json
{
  "parser_output_folder": "<path to parser outbox run folder>",
  "parser_root": "<path to modules/pdf-parser>",
  "source_pdf_path": "<path to original source PDF>"
}
```

Submit handshake:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri 'http://127.0.0.1:8093/handshake/start' `
  -ContentType 'application/json' `
  -Body '{"job_folder":"<absolute path to parser outbox run folder>"}'
```

## QA Behavior

For each page, QA computes source walkback metrics and writes:

- `qa_page_machine_code`
- `qa_status`
- `accuracy_score`
- `char_similarity`
- `token_recall`

When native source text is thin, QA runs adaptive OCR first (`source_ocr_psm_candidates`, `source_ocr_rotation_candidates`) and only compares against source text that passes minimum reference quality (`source_reference_min_quality`). Candidate selection can apply a light parser-alignment tie-break (`source_ocr_parser_alignment_weight`) when OCR variants are close.

OCR throughput is controlled with:

- `source_ocr_workers` (current default: `5`)
- `source_ocr_timeout_seconds` (current default: `25.0`)

Run summaries now include:

- `qa_stage_timings`
- `source_extraction_stats`

Quality-safe defaults keep:

- `source_ocr_worker_backend = "thread"`
- `source_ocr_candidate_cascade_enabled = false`
- `source_ocr_adaptive_dpi_enabled = false`

Experimental speed toggles remain available for controlled testing:

- `source_ocr_worker_backend = "process"`
- `source_ocr_candidate_cascade_enabled = true`
- `source_ocr_adaptive_dpi_enabled = true`

## Throughput Snapshot

Measured on the current workstation (`11th Gen Intel(R) Core(TM) i5-1145G7`, 8 logical cores) with the tuned QA setting `source_ocr_workers=5`:

- Parser: `1.39 sec/page` (RUN_2026_04_11_1542, 148 pages in 206.46s)
- QA: `2.36 sec/page` (QA_2026_04_12_A8A4, 148 pages in 349.10s)
- Combined parse + QA: `3.75 sec/page`

Projected for one 200-page PDF of similar complexity:

- Parse: ~`278s` (~`4m 38s`)
- QA: ~`472s` (~`7m 52s`)
- End-to-end parse + QA: ~`750s` (~`12m 30s`)

Operational pass gate targets 95% accepted quality with 5% fail margin on comparable pages:

- `run_accept_ratio`
- `run_max_fail_ratio`
- `run_min_comparable_pages`

The QA stamp format is:

- `<document_machine_code>.QA.<qa_run_id>.P####`

Example:

- `DOC_28A0E49506EFCE95.QA.QA_2026_04_11_1A2B.P0007`

QA writes results to:

- `QA\outbox\<qa_run_id>_<parser_run_folder>\machine_readable\`

And copies QA artifacts into the parser run:

- `PDF Parser\outbox\<run_folder>\qa\<qa_run_id>\`

## Janitor Ownership

Parser-side janitor is disabled. QA is now the janitor authority.

When QA run status is `pass`, QA janitor can purge parser runtime while keeping exactly one validated parser outbox folder.

Janitor settings live in:

- `config/thresholds.json` -> `janitor`
