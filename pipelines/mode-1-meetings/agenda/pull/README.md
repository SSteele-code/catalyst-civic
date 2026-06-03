# Catalyst Civic: PULL Layer — Agenda (Source Acquisition)

The PULL layer is responsible for automated ingestion of raw meeting agenda PDFs from the Town of Richlands, VA public website into the flat M1 source store.

## 🌐 Data Source

**URL**: [https://town.richlands.va.us/agenda/agenda.html](https://town.richlands.va.us/agenda/agenda.html)

The town publishes council meeting agendas as PDFs, organized by year (2013–2026). Each yearly index page lists downloadable PDF links for council packets, special meetings, public hearings, and workshops.

## 🛠️ The Ingestion Logic

### 1. Agenda Link Parser (`parse_links.py`)
*   **Mission**: Scrape a single yearly agenda HTML page and extract all PDF download links.
*   **Target**: `https://town.richlands.va.us/agenda/{YYYY}AGENDA.html`
*   **Output**: Structured JSON array with `url`, `title`, `year`, `date_str` for each PDF.
*   **Usage**: `python parse_links.py --year 2025 --json`

### 2. Agenda Fetcher (`fetch_agenda.py`)
*   **Mission**: Download a single agenda PDF by URL with retry logic.
*   **Strict Invariant**: Stage 01 ONLY. No parsing or transformation.
*   **Usage**: `python fetch_agenda.py --url "<pdf_url>" --outdir ./output`

### 3. PULL Orchestrator (`orchestrator.py`)
*   **Mission**: Manage the batch lifecycle of agenda PDF discovery and download.
*   **Pipeline**: Discover (parse_links) → Fetch (fetch_agenda) → Rename (machine code) → Track (state + manifest)
*   **Output Root**: `_Sources/M1-Meetings/Agendas/`
*   **File Naming**: `M1.AG.<document_number>.<created_yyyymmdd>.<pulled_yyyymmdd>.pdf`
*   **Manifest**: `M1_AGENDAS_MANIFEST.jsonl`
*   **State**: `agenda_state.json`

## 🚀 Quick Start

```bash
# Dry run — discover all agendas without downloading
python orchestrator.py --dry-run

# Download everything from 2024 onward
python orchestrator.py --since 2024

# Download only the next 5 unprocessed agendas
python orchestrator.py --limit 5

# Scan a single year
python parse_links.py --year 2025
```

## 📁 Output Structure

```
_Sources/M1-Meetings/Agendas/
├── agenda_state.json
├── M1_AGENDAS_MANIFEST.jsonl
├── M1.AG.000001.20130514.20260331.pdf
├── M1.AG.000002.20130709.20260331.pdf
└── ...
```

## 🧹 Git Policy

- `_Sources/M1-Meetings/Agendas/` is local data storage and is gitignored.
- Downloaded PDFs, `agenda_state.json`, `M1_AGENDAS_MANIFEST.jsonl`, and local upload-cadence reports are not versioned.

## 📈 Upload Cadence (Observed)

Snapshot collected on **2026-04-11** from Richlands agenda PDF HTTP metadata (`Last-Modified`):

- Source URLs analyzed: `264`
- With `Last-Modified`: `264` (100%)
- First observed upload: `2016-07-05T19:42:19Z`
- Most recent observed upload: `2026-04-10T20:52:45Z`
- Average upload rate: `2.22` uploads / 30 days
- Median gap between uploads: `10.11` days
- Gap spread: p25 `0.00` days, p75 `25.07` days, max `91.09` days
- Weekday pattern: Friday-heavy (`104`), then Monday (`56`), Tuesday (`49`)

Year-level upload counts (by `Last-Modified`):

- `2016: 25`
- `2017: 36`
- `2018: 16`
- `2019: 17`
- `2020: 26`
- `2021: 37`
- `2022: 22`
- `2023: 26`
- `2024: 16`
- `2025: 37`
- `2026: 6` (through 2026-04-10)

Detailed report:
`_Sources/M1-Meetings/Agendas/RICHlands_upload_metadata_report.json`

## 📦 Dependencies

**None.** Uses only Python standard library (`urllib`, `html.parser`, `json`, `pathlib`).

## 🤖 Bot/Agent Instructions
*   **Flat Storage**: Do not create year folders under `Agendas/`.
*   **State File**: `agenda_state.json` tracks processed URL IDs.
*   **Manifest File**: `M1_AGENDAS_MANIFEST.jsonl` is the canonical ingest ledger and machine-code registry.
*   **Idempotent**: Re-running the orchestrator skips already-ingested URLs.
*   **Polite Crawling**: Built-in 2–5 second random jitter between downloads.
