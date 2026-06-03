# PDF Parser — Optical Engine for Civic Documents

A production-grade, resident-service PDF processing engine built for municipal document packets. The machine boots once, accepts folder-drop jobs via HTTP handshake, and runs each packet through a deterministic 10-state machine that classifies, extracts, and packages every page into machine-readable JSON.

## Architecture

The engine is a resident service, not a batch script. One process stays alive and handles sequential job submissions.

**Service states:** `booting` → `ready` → `processing` → `fatal`

**Run states (per job):**
1. `handshake_received`
2. `drop_verified`
3. `prepared`
4. `split`
5. `features_computed`
6. `typed`
7. `extracted`
8. `packaged`
9. `handoff_ready`
10. `completed`

The run manifest under `manifests/runs/` is the truth source for state, timings, and delivery paths. Terminal failure state: `failed`.

## Entry Point

```
scripts/run_state_machine_service.py   — primary service boot
scripts/run_review_engine_service.py   — review/audit service
cleave_and_scout.py                    — direct standalone entry (bypasses service circularities)
```

## Processing Flow

For each submitted job:
1. Validate handshake and source PDF
2. Copy source to `work/runs/<run_id>/input`
3. Split into single-page PDFs
4. Compute per-page features in-process:
   - render page to image
   - detect native text presence and quality
   - detect and correct skew
   - detect handwriting regions
   - detect text and table regions
   - select extraction route (native text vs OCR)
5. Type pages (classify function, layout, support role)
6. Run type-specific extraction
7. Package and emit machine-readable handoff payload

Core implementation:
- `src/state_machine/page_feature_pipeline.py`
- `src/state_machine/page_typer.py`
- `src/state_machine/extractors.py`
- `src/state_machine/packager.py`

## Page Classification

The classifier tracks three orthogonal axes:
- `function_type` — what the page does (agenda item, minutes, report, etc.)
- `layout_type` — how the page is structured (prose, table, mixed, etc.)
- `support_role` — role within the packet (main body, separator, cover, etc.)

The legacy `page_type` label is still exported for downstream compatibility.

Enabled page types: `blank_separator`, `agenda`, `minutes`, `reference_or_procedure`, `legislative_prose`, `contract_or_agreement`, `financial_report`, `government_form`, `invoice`, `table_or_mixed_layout`, `generic_prose`, `powerpoint`.

The classifier is heuristic and manifest-driven. It uses OCR text, layout features, neighbor context, and contradiction handling — not a trained model.

## Per-Page Output Schema

Each typed page exports:
- `page_type`, `function_type`, `layout_type`, `support_role`
- `routing_tags` — formal handshake for downstream modules (e.g., `["route_agenda"]`)
- `page_family`, `page_layout`, `page_support_subtype`
- `page_type_confidence`, `suspicion_score`, `suspicion_reasons`
- `decision_reason`, `page_type_candidates`

## Performance

Baseline `RUN_2026_04_14_7BFA` against a production municipal agenda packet:

| Metric | Value |
|---|---|
| Throughput | 67.56 pages/minute |
| Latency | 0.89 seconds/page |
| Peak RSS | 518 MB |
| CPU | ~400% (parallel extraction) |

## Sample Output

`sample-output/` contains one real run against `ORIGINAL_SOURCE.pdf`:
- `example.factsheet.json` — full run manifest: page IDs, segment map, status, completed stages
- `example.txt` — full extracted text output from the packet

## HTTP Handshake

The service listens on `http://127.0.0.1:8091` by default.

Job folder shape:
```
inbox/
  JOB_123/
    source.pdf
    intake.json
```

Minimal `intake.json`:
```json
{
  "job_id": "JOB_123",
  "source_file": "source.pdf",
  "profile": "default"
}
```

Submit:
```
POST /handshake/start   { "job_folder": "<absolute path to job folder>" }
GET  /health
GET  /runs/<run_id>
```

## Configuration

Primary settings: `config/thresholds.json`

Key sections: `service`, `state_machine`, `page_types`, `layout`, `extraction`, `classification`, `segmentation`, `escalation`

## Setup

- Python 3.12+
- Tesseract OCR installed and on PATH (Windows default: `C:\Program Files\Tesseract-OCR\tesseract.exe`)
- FFmpeg on PATH

Core dependencies:
```
pip install -r requirements.txt
```

Requires: `fitz` (PyMuPDF), `opencv-python`, `pytesseract`, `Pillow`, `psutil`

## Path Configuration

This engine uses absolute paths in several places. Before running locally, update the following in `src/state_machine/config.py` and `scripts/run_state_machine_service.py`:
- Service working directories (`inbox/`, `outbox/`, `work/`, `manifests/`, `logs/`)
- Tesseract executable path

## Mode 1 Specialization Note

This is the Mode 1 Meetings instance of the engine — tuned and battle-tested for municipal agenda packets: Roman numeral outlines, nested agenda items, civic phrasal signatures (Roll Call, Call to Order, Show Cause). See `MUNICIPALITY_READER_SPEC.md` for the design specification.
